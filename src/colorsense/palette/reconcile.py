"""Reconcile declared-token intent with measured usage via log-linear pooling.

This module fuses two independent signals about a site's palette, in **usage space**
([`UsageCategory`][colorsense.UsageCategory]):

* **usage** — the measured per-category prominence over rendered *colors* produced by
  ``build_usage``; this is "what actually rendered".
* **tokens** — the declared design-token *intent* produced by ``classify_tokens``; each
  token carries a resolved [`Color`][colorsense.Color] and a ``usage_prior`` distribution over
  [`UsageCategory`][colorsense.UsageCategory]; this is "what the author declared".

The two are combined by **log-linear pooling** (a weighted geometric mean) with weight
``alpha`` on intent: ``alpha=0`` -> pure usage, ``alpha=1`` -> pure intent. The pooling
universe is the **measured** usage entries only — declared intent re-weights colors that
actually rendered; a declared color with no measured match never enters the posterior
(it is reported through divergence instead), which is what keeps the public guarantee
that every posterior entry carries measured ``area``/``components`` evidence. A missing
intent signal is uniform-smoothed (``+ 1/K`` over the K candidates), a bounded
scale-aware penalty rather than a veto. Colors are matched across the two sources by
nearest-color under two perceptual ΔE radii — the tight `DELTA_E_MATCH` for
declared-vs-declared and the looser `DELTA_E_MATCH_MEASURED` for measured-vs-declared
(rationale at each constant).

The output is a posterior [`UsagePalette`][colorsense.UsagePalette] plus a divergence report listing
declared-but-unused and used-but-undeclared discrepancies. Declared-but-unused items are
gated to **high-intent** tokens (`HIGH_INTENT_ORIGINS`, where the gate's rationale
lives).

All thresholds are module-level constants, documented and tunable.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from colorsense.color.primitives import delta_e
from colorsense.models import (
    ClassifiedToken,
    Color,
    DivergenceItem,
    TokenOrigin,
    UsageCategory,
    UsageEntry,
    UsagePalette,
)
from colorsense.palette._pruning import prune_distribution
from colorsense.palette.inventory import DELTA_E_MATCH_BG

__all__ = ["reconcile"]

# --- Tunable constants -------------------------------------------------------

#: Nearest-color join threshold in OKLab ΔE for grouping DECLARED token colors with
#: each other. Both sides are exact computed values, so the radius stays tight.
DELTA_E_MATCH: float = 0.08

#: Join threshold for matching a MEASURED usage entry against a declared token color.
#: A measured entry's representative is a screenshot-quantizer bin whenever the cluster
#: matched one, and an element may join a bin up to the bg join radius away
#: (`DELTA_E_MATCH_BG`) — so this radius must be at
#: least that, or a pixel-perfect rendered token can fail its own intent match purely
#: from (platform-dependent) quantizer blending (see docs/how-it-works.md).
DELTA_E_MATCH_MEASURED: float = DELTA_E_MATCH_BG

#: Degenerate-input guard on the USAGE side of the geometric mean only, so a
#: zero-probability entry contributes ``log(EPS)`` rather than ``log(0)`` (undefined).
#: Real ``build_usage`` output never carries zero probabilities, so this never shapes
#: results. The INTENT side deliberately does not use it: a missing intent signal is
#: uniform-smoothed with ``1/K`` instead (see `_pool_category`) — an EPS-floored intent
#: factor made "no token match" a ~``(1/EPS)**alpha`` multiplicative veto that erased
#: dominant undeclared colors from the posterior.
EPS: float = 1e-9

#: Minimum posterior probability a color must retain to survive pruning. Survivors are
#: renormalized after pruning.
MIN_POSTERIOR_PROB: float = 0.02

#: Minimum aggregated token weight for a declared color to be eligible to surface as a
#: declared-but-unused divergence item.
DECLARE_MIN_WEIGHT: float = 0.0

#: Minimum posterior-independent usage probability for a usage entry to surface as a
#: used-but-undeclared divergence item.
UNDECLARED_MIN_PROB: float = 0.15

#: Token classification origins eligible to raise a declared-but-unused divergence.
#: Only direct evidence of author intent qualifies: ``relational`` and ``name_rule``.
#: ``scale`` members (every shade of a palette scale is "declared" but most are never
#: meant to render), ``alias`` followers, and ``fallback`` classifications are excluded
#: (see docs/how-it-works.md for the token-heavy-site failure that motivated the gate).
#: The two qualifying origins reach the report by different paths: ``name_rule`` tokens
#: carry usage priors and report through their intent group's argmax category, while
#: ``relational`` tokens carry no prior (channel-routed) and report through the
#: dedicated `_aggregate_relational` pass, attributed to ``text``.
HIGH_INTENT_ORIGINS: frozenset[TokenOrigin] = frozenset(
    {TokenOrigin.relational, TokenOrigin.name_rule}
)


# --- Internal aggregation structures ----------------------------------------


class _IntentGroup:
    """A color group accumulating declared-token intent for one (approx) color."""

    def __init__(self, color: Color) -> None:
        self.color: Color = color
        self.intent_raw: dict[UsageCategory, float] = {}
        self.token_weight: float = 0.0
        self.rep_name: str = ""
        self._rep_weight: float = -math.inf
        self.high_intent: bool = False

    def add(self, token: ClassifiedToken) -> None:
        for category, prior in token.usage_prior.items():
            self.intent_raw[category] = self.intent_raw.get(category, 0.0) + token.weight * prior
        self.token_weight += token.weight
        if token.origin in HIGH_INTENT_ORIGINS:
            self.high_intent = True
        if token.weight > self._rep_weight:
            self._rep_weight = token.weight
            self.rep_name = token.record.name

    def normalized_intent(self) -> dict[UsageCategory, float]:
        total = sum(self.intent_raw.values())
        if total <= 0.0:
            return {}
        return {category: val / total for category, val in self.intent_raw.items()}


def _group_by_color(eligible: list[ClassifiedToken]) -> list[_IntentGroup]:
    """Fold ``eligible`` tokens into `_IntentGroup`\\ s by nearest color.

    Tokens are processed sorted by ``record.name`` for determinism; a token joins an
    existing group when within `DELTA_E_MATCH` of the group's color, else starts a
    new group anchored on its own resolved color. Callers must pre-filter to tokens
    with a resolved color.
    """
    groups: list[_IntentGroup] = []
    for token in sorted(eligible, key=lambda t: t.record.name):
        color = token.record.resolved
        assert color is not None  # callers filter on resolved
        matched: _IntentGroup | None = None
        for group in groups:
            if delta_e(color, group.color) <= DELTA_E_MATCH:
                matched = group
                break
        if matched is None:
            matched = _IntentGroup(color)
            groups.append(matched)
        matched.add(token)
    return groups


def _aggregate_intent(tokens: list[ClassifiedToken]) -> list[_IntentGroup]:
    """Group declared tokens by color and build per-category intent scores.

    Only tokens with a resolved color and a non-empty ``usage_prior`` are considered —
    these are the tokens that can shape pooling. Relational and (excluded) status tokens
    carry empty priors by construction and are handled separately in divergence
    (`_aggregate_relational`, and the all-resolved-colors membership test).
    """
    return _group_by_color(
        [t for t in tokens if t.record.resolved is not None and len(t.usage_prior) > 0]
    )


def _aggregate_relational(tokens: list[ClassifiedToken]) -> list[_IntentGroup]:
    """Group relational (``--on-primary``-style) tokens for divergence reporting.

    Relational classifications always carry an EMPTY ``usage_prior`` (their config row
    is a channel route, not a category distribution), so they never form intent groups
    and never shape pooling — but they are direct author intent (`HIGH_INTENT_ORIGINS`)
    and must still be able to raise declared-but-unused. The empty-prior filter keeps a
    (hand-constructed) prior-bearing relational token from being reported twice.
    """
    return _group_by_color(
        [
            t
            for t in tokens
            if t.origin is TokenOrigin.relational
            and t.record.resolved is not None
            and not t.usage_prior
        ]
    )


def _clamp_alpha(alpha: float) -> float:
    """Clamp ``alpha`` into ``[0, 1]`` (out-of-range values are silently clamped)."""
    if alpha < 0.0:
        return 0.0
    if alpha > 1.0:
        return 1.0
    return alpha


def reconcile(
    usage: UsagePalette,
    tokens: list[ClassifiedToken],
    alpha: float = 0.4,
    *,
    measured_colors: Sequence[Color] | None = None,
) -> tuple[UsagePalette, list[DivergenceItem]]:
    """Fuse declared intent (``tokens``) with measured ``usage`` by log-linear pooling.

    The pipeline is aggregation (`_aggregate_intent`) → per-category pooling
    (`_pool_category`) → divergence (`_build_divergence`).

    Returns a posterior [`UsagePalette`][colorsense.UsagePalette] and a deterministic list of
    [`DivergenceItem`][colorsense.DivergenceItem]. ``alpha`` weights intent vs. usage and is clamped
    to ``[0, 1]``. The posterior universe is the measured usage entries, so every posterior entry
    carries its measured entry's ``area`` and non-empty ``components``; a declared color with no
    measured match never appears in the posterior and is reported via divergence instead.

    ``measured_colors``, when given, is the FULL measured color inventory (every cluster
    color, pre-prune) used for the declared-but-unused membership test: ``usage`` entries
    are post-prune, so testing against them alone would report a declared color that
    genuinely rendered — just below every category's prune threshold — as "unused in
    render". ``None`` falls back to the usage entries (all colors that survived pruning).
    """
    alpha = _clamp_alpha(alpha)
    groups = _aggregate_intent(tokens)
    intents: list[dict[UsageCategory, float]] = [g.normalized_intent() for g in groups]

    posterior_mapping: dict[UsageCategory, tuple[UsageEntry, ...]] = {}
    for category in UsageCategory:  # iterate in enum order for determinism
        posterior_mapping[category] = tuple(_pool_category(category, usage, groups, intents, alpha))

    posterior = UsagePalette(mapping=posterior_mapping)
    divergence = _build_divergence(usage, tokens, groups, intents, measured_colors)
    return posterior, divergence


@dataclass
class _PoolCandidate:
    """One measured usage entry in a category's pooling universe, with its matched
    declared-intent share (``0.0`` when no declared group is within
    `DELTA_E_MATCH_MEASURED`)."""

    measured: UsageEntry
    p_usage: float
    p_intent: float


def _pool_category(
    category: UsageCategory,
    usage: UsagePalette,
    groups: list[_IntentGroup],
    intents: list[dict[UsageCategory, float]],
    alpha: float,
) -> list[UsageEntry]:
    """Pool the category's measured entries against declared intent, prune, renormalize.

    The pooling universe is the MEASURED entries only: declared intent re-weights colors
    that actually rendered, and a declared color with no measured match never enters the
    posterior (it surfaces through divergence instead). This is what makes the public
    guarantee structural — every posterior entry inherits a measured entry's ``area``
    and non-empty ``components``. (Injecting token-only colors was also a live failure
    mode for unmeasured categories: with zero measurement the posterior collapses to
    ``intent**alpha``, a near-uniform spread where everything survives pruning; see
    docs/how-it-works.md.)
    """
    usage_entries = usage.mapping.get(category, ())
    if not usage_entries:
        return []

    def intent_for(color: Color) -> float:
        best_idx: int | None = None
        best_d = DELTA_E_MATCH_MEASURED
        for gi, group in enumerate(groups):
            if category not in intents[gi]:
                continue
            d = delta_e(color, group.color)
            if d <= best_d:
                best_d = d
                best_idx = gi
        if best_idx is None:
            return 0.0
        return intents[best_idx][category]

    candidates = [
        _PoolCandidate(
            measured=entry,
            p_usage=entry.probability,
            p_intent=intent_for(entry.color),
        )
        for entry in usage_entries
    ]

    # Log-linear pool. The intent factor is uniform-smoothed (+ 1/K over the K
    # candidates): lacking a token match then costs at most a bounded, universe-scaled
    # factor of ``(K + 1) ** alpha`` (~1.6x at K=2, ~2.6x at K=10 for the default
    # alpha) instead of the unbounded near-veto an absolute floor produces — a 95%-
    # dominant undeclared color must stay dominant over a minor declared one.
    smoothing = 1.0 / len(candidates)
    unnorm = [
        (c.p_usage + EPS) ** (1.0 - alpha) * (c.p_intent + smoothing) ** alpha for c in candidates
    ]

    # Normalize, prune, renormalize survivors via the shared step; if pruning empties
    # the category, the deterministic argmax (ties broken by smallest hex) is kept.
    kept = prune_distribution(
        candidates,
        unnorm,
        min_share=MIN_POSTERIOR_PROB,
        tie_key=lambda c: c.measured.color.hex,
    )

    result = [
        UsageEntry(
            color=candidate.measured.color,
            probability=prob,
            area=candidate.measured.area,
            components=dict(candidate.measured.components),
        )
        for candidate, prob in kept
    ]
    result.sort(key=lambda e: (-e.probability, e.color.hex))
    return result


def _build_divergence(
    usage: UsagePalette,
    tokens: list[ClassifiedToken],
    groups: list[_IntentGroup],
    intents: list[dict[UsageCategory, float]],
    measured_colors: Sequence[Color] | None,
) -> list[DivergenceItem]:
    """Report declared-but-unused and used-but-undeclared discrepancies.

    The "unused in render" membership test prefers ``measured_colors`` (the pre-prune
    inventory — see `reconcile`) so a sub-threshold-but-rendered declared color is not
    misreported as unused.
    """
    usage_colors: list[Color] = (
        list(measured_colors)
        if measured_colors is not None
        # Fallback: all measured usage colors that survived per-category pruning.
        else [entry.color for entries in usage.mapping.values() for entry in entries]
    )

    def matches_any_usage(color: Color) -> bool:
        return any(delta_e(color, uc) <= DELTA_E_MATCH_MEASURED for uc in usage_colors)

    items: list[DivergenceItem] = []

    # DECLARED-BUT-UNUSED: token color with high-intent classification (see
    # HIGH_INTENT_ORIGINS), intent mass, and no rendered usage match.
    for gi, group in enumerate(groups):
        intent = intents[gi]
        if not intent:
            continue
        if not group.high_intent:
            continue
        if group.token_weight < DECLARE_MIN_WEIGHT:
            continue
        if matches_any_usage(group.color):
            continue
        argmax_category = max(intent.items(), key=lambda kv: (kv[1], kv[0].value))[0]
        items.append(
            DivergenceItem(
                category=argmax_category,
                color=group.color,
                note=f"declared '{group.rep_name}' unused in render",
            )
        )

    # DECLARED-BUT-UNUSED, relational arm: relational tokens carry no usage prior (so
    # they never join `groups`), but they are direct author intent and an unused
    # ``--on-primary`` deserves a report. They are foreground/text colors by
    # construction (the ``text_on`` channel), so the item is attributed to ``text``.
    for group in _aggregate_relational(tokens):
        if group.token_weight < DECLARE_MIN_WEIGHT:
            continue
        if matches_any_usage(group.color):
            continue
        items.append(
            DivergenceItem(
                category=UsageCategory.text,
                color=group.color,
                note=f"declared '{group.rep_name}' unused in render",
            )
        )

    # USED-BUT-UNDECLARED: prominent usage entry matching no declared token color.
    # Membership is tested against EVERY resolved declared color — including relational,
    # status, scale, and fallback classifications that carry no usage prior — because
    # "undeclared" is a statement about the stylesheet, not about intent mass: a page
    # rendering exactly its declared ``--on-primary`` text color is not undeclared.
    token_colors = [t.record.resolved for t in tokens if t.record.resolved is not None]

    def matches_any_token(color: Color) -> bool:
        return any(delta_e(color, tc) <= DELTA_E_MATCH_MEASURED for tc in token_colors)

    seen: set[tuple[UsageCategory, str]] = set()
    for category, entries in usage.mapping.items():
        for entry in entries:
            if entry.probability < UNDECLARED_MIN_PROB:
                continue
            if matches_any_token(entry.color):
                continue
            key = (category, entry.color.hex)
            if key in seen:
                continue
            seen.add(key)
            items.append(
                DivergenceItem(
                    category=category,
                    color=entry.color,
                    note="used but undeclared",
                )
            )

    items.sort(key=lambda d: (d.note, d.color.hex))
    return items
