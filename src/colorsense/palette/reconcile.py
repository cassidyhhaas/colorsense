"""Reconcile declared-token intent with measured usage via log-linear pooling.

This module fuses two independent signals about a site's palette, in **usage-role space**
([`UsageRole`][colorsense.UsageRole]):

* **usage** — the measured per-role prominence over rendered *colors* produced by
  ``build_usage``; this is "what actually rendered".
* **tokens** — the declared design-token *intent* produced by ``classify_tokens``; each
  token carries a resolved [`Color`][colorsense.Color] and a ``usage_intent`` distribution over
  [`UsageRole`][colorsense.UsageRole]; this is "what the author declared".

The two are combined by **log-linear pooling** (a weighted geometric mean) with weight
``alpha`` on intent: ``alpha=0`` -> pure usage, ``alpha=1`` -> pure intent. The pooling
universe is the **measured** usage entries only — declared intent re-weights colors that
actually rendered; a declared color with no measured match never enters the posterior
(it is reported through divergence instead), which is what keeps the public guarantee
that every posterior entry carries measured ``area``/``components`` evidence. A missing
intent signal is uniform-smoothed (``+ 1/K`` over the K candidates), a bounded
scale-aware penalty rather than a veto. Colors are matched across the two sources by
nearest-color under two maximum OKLab ΔE distances — the tight `MAX_TOKEN_MERGE_DELTA_E`
for collapsing declared tokens with each other and the looser `MAX_MEASURED_MATCH_DELTA_E`
for matching a measured color against a declared token (rationale at each constant).

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

from colorsense.color.match import any_within, first_within, nearest_within
from colorsense.models import (
    ClassifiedToken,
    Color,
    DivergenceItem,
    TokenOrigin,
    UsageEntry,
    UsagePalette,
    UsageRole,
)
from colorsense.palette._pruning import prune_distribution
from colorsense.palette.inventory import MAX_BG_MATCH_DELTA_E

__all__ = ["reconcile"]

# --- Tunable constants -------------------------------------------------------

#: Maximum OKLab ΔE distance at which two DECLARED token colors are treated as the same
#: color and folded into one intent group (`_group_by_color`). Both sides are exact computed
#: values, so the ceiling stays tight.
MAX_TOKEN_MERGE_DELTA_E: float = 0.08

#: Maximum OKLab ΔE distance at which a MEASURED usage color is treated as the same color
#: as a declared token. A measured entry's representative is a screenshot-quantizer bin
#: whenever the cluster matched one, and an element may join a bin up to the bg join radius
#: away (`MAX_BG_MATCH_DELTA_E`) — so this ceiling must be at least that, or a pixel-perfect
#: rendered token can fail its own intent match purely from (platform-dependent) quantizer
#: blending (see docs/how-it-works.md).
MAX_MEASURED_MATCH_DELTA_E: float = MAX_BG_MATCH_DELTA_E

#: Degenerate-input guard on the USAGE side of the geometric mean only, so a
#: zero-probability entry contributes ``log(EPS)`` rather than ``log(0)`` (undefined).
#: Real ``build_usage`` output never carries zero probabilities, so this never shapes
#: results. The INTENT side deliberately does not use it: a missing intent signal is
#: uniform-smoothed with ``1/K`` instead (see `_pool_role`) — an EPS-floored intent
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
#: carry usage intent and report through their intent group's argmax role, while
#: ``relational`` tokens carry no usage intent (channel-routed) and report through the
#: dedicated `_aggregate_relational` pass, attributed to ``text``.
HIGH_INTENT_ORIGINS: frozenset[TokenOrigin] = frozenset(
    {TokenOrigin.relational, TokenOrigin.name_rule}
)


# --- Internal aggregation structures ----------------------------------------


class _IntentGroup:
    """A color group accumulating declared-token intent for one (approx) color."""

    def __init__(self, color: Color) -> None:
        self.color: Color = color
        self.intent_raw: dict[UsageRole, float] = {}
        self.token_weight: float = 0.0
        self.representative_name: str = ""
        self._representative_weight: float = -math.inf
        self.high_intent: bool = False

    def add(self, token: ClassifiedToken) -> None:
        # The token's usage intent is the prior (declared belief about where the
        # color is used); it is later log-linearly pooled against measured usage
        # (the evidence) to form the reconciled posterior.
        for role, weight in token.usage_intent.items():
            self.intent_raw[role] = self.intent_raw.get(role, 0.0) + token.weight * weight
        self.token_weight += token.weight
        if token.origin in HIGH_INTENT_ORIGINS:
            self.high_intent = True
        if token.weight > self._representative_weight:
            self._representative_weight = token.weight
            self.representative_name = token.record.name

    def normalized_intent(self) -> dict[UsageRole, float]:
        total = sum(self.intent_raw.values())
        if total <= 0.0:
            return {}
        return {role: val / total for role, val in self.intent_raw.items()}


def _group_by_color(eligible: list[ClassifiedToken]) -> list[_IntentGroup]:
    """Fold ``eligible`` tokens into `_IntentGroup`\\ s by nearest color.

    Tokens are processed sorted by ``record.name`` for determinism; a token joins an
    existing group when within `MAX_TOKEN_MERGE_DELTA_E` of the group's color, else starts a
    new group anchored on its own resolved color. Callers must pre-filter to tokens
    with a resolved color.
    """
    groups: list[_IntentGroup] = []
    for token in sorted(eligible, key=lambda t: t.record.name):
        color = token.record.resolved
        assert color is not None  # callers filter on resolved
        match_idx = first_within(color, groups, MAX_TOKEN_MERGE_DELTA_E, key=lambda g: g.color)
        matched = groups[match_idx] if match_idx is not None else None
        if matched is None:
            matched = _IntentGroup(color)
            groups.append(matched)
        matched.add(token)
    return groups


def _aggregate_intent(tokens: list[ClassifiedToken]) -> list[_IntentGroup]:
    """Group declared tokens by color and build per-role intent scores.

    Only tokens with a resolved color and a non-empty ``usage_intent`` are considered —
    these are the tokens that can shape pooling. Relational and (excluded) status tokens
    carry empty usage intent by construction and are handled separately in divergence
    (`_aggregate_relational`, and the all-resolved-colors membership test).
    """
    return _group_by_color(
        [t for t in tokens if t.record.resolved is not None and len(t.usage_intent) > 0]
    )


def _aggregate_relational(tokens: list[ClassifiedToken]) -> list[_IntentGroup]:
    """Group relational (``--on-primary``-style) tokens for divergence reporting.

    Relational classifications always carry an EMPTY ``usage_intent`` (their config row
    is a channel route, not a usage-intent distribution), so they never form intent groups
    and never shape pooling — but they are direct author intent (`HIGH_INTENT_ORIGINS`)
    and must still be able to raise declared-but-unused. The empty-usage-intent filter keeps a
    (hand-constructed) intent-bearing relational token from being reported twice.
    """
    return _group_by_color(
        [
            t
            for t in tokens
            if t.origin is TokenOrigin.relational
            and t.record.resolved is not None
            and not t.usage_intent
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

    The pipeline is aggregation (`_aggregate_intent`) → per-role pooling
    (`_pool_role`) → divergence (`_build_divergence`).

    Returns a posterior [`UsagePalette`][colorsense.UsagePalette] and a deterministic list of
    [`DivergenceItem`][colorsense.DivergenceItem]. ``alpha`` weights intent vs. usage and is clamped
    to ``[0, 1]``. The posterior universe is the measured usage entries, so every posterior entry
    carries its measured entry's ``area`` and non-empty ``components``; a declared color with no
    measured match never appears in the posterior and is reported via divergence instead.

    ``measured_colors``, when given, is the FULL measured color inventory (every cluster
    color, pre-prune) used for the declared-but-unused membership test: ``usage`` entries
    are post-prune, so testing against them alone would report a declared color that
    genuinely rendered — just below every role's prune threshold — as "unused in
    render". ``None`` falls back to the usage entries (all colors that survived pruning).
    """
    alpha = _clamp_alpha(alpha)
    groups = _aggregate_intent(tokens)
    intents: list[dict[UsageRole, float]] = [g.normalized_intent() for g in groups]

    posterior_mapping: dict[UsageRole, tuple[UsageEntry, ...]] = {}
    for role in UsageRole:  # iterate in enum order for determinism
        posterior_mapping[role] = tuple(_pool_role(role, usage, groups, intents, alpha))

    posterior = UsagePalette(mapping=posterior_mapping)
    divergence = _build_divergence(usage, tokens, groups, intents, measured_colors)
    return posterior, divergence


@dataclass
class _PoolCandidate:
    """One measured usage entry in a role's pooling universe, with its matched
    declared-intent share (``0.0`` when no declared group is within
    `MAX_MEASURED_MATCH_DELTA_E`)."""

    measured: UsageEntry
    p_usage: float
    p_intent: float


def _pool_role(
    role: UsageRole,
    usage: UsagePalette,
    groups: list[_IntentGroup],
    intents: list[dict[UsageRole, float]],
    alpha: float,
) -> list[UsageEntry]:
    """Pool the role's measured entries against declared intent, prune, renormalize.

    The pooling universe is the MEASURED entries only: declared intent re-weights colors
    that actually rendered, and a declared color with no measured match never enters the
    posterior (it surfaces through divergence instead). This is what makes the public
    guarantee structural — every posterior entry inherits a measured entry's ``area``
    and non-empty ``components``. (Injecting token-only colors was also a live failure
    mode for unmeasured roles: with zero measurement the posterior collapses to
    ``intent**alpha``, a near-uniform spread where everything survives pruning; see
    docs/how-it-works.md.)
    """
    usage_entries = usage.mapping.get(role, ())
    if not usage_entries:
        return []

    def intent_for(color: Color) -> float:
        # Pre-filter to groups carrying this role, keeping the original index so the
        # nearest match remaps back to `intents[group_index]`. Same <=/tie-to-last semantics.
        groups_with_role = [
            (group_index, group)
            for group_index, group in enumerate(groups)
            if role in intents[group_index]
        ]
        match_idx = nearest_within(
            color, groups_with_role, MAX_MEASURED_MATCH_DELTA_E, key=lambda pair: pair[1].color
        )
        if match_idx is None:
            return 0.0
        group_index = groups_with_role[match_idx][0]
        return intents[group_index][role]

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
    uniform_smoothing = 1.0 / len(candidates)
    unnormalized_scores = [
        (c.p_usage + EPS) ** (1.0 - alpha) * (c.p_intent + uniform_smoothing) ** alpha
        for c in candidates
    ]

    # Normalize, prune, renormalize survivors via the shared step; if pruning empties
    # the role, the deterministic argmax (ties broken by smallest hex) is kept.
    kept = prune_distribution(
        candidates,
        unnormalized_scores,
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
    intents: list[dict[UsageRole, float]],
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
        # Fallback: all measured usage colors that survived per-role pruning.
        else [entry.color for entries in usage.mapping.values() for entry in entries]
    )

    def matches_any_usage(color: Color) -> bool:
        return any_within(color, usage_colors, MAX_MEASURED_MATCH_DELTA_E, key=lambda c: c)

    items: list[DivergenceItem] = []

    # DECLARED-BUT-UNUSED: token color with high-intent classification (see
    # HIGH_INTENT_ORIGINS), intent mass, and no rendered usage match.
    for group_index, group in enumerate(groups):
        intent = intents[group_index]
        if not intent:
            continue
        if not group.high_intent:
            continue
        if group.token_weight < DECLARE_MIN_WEIGHT:
            continue
        if matches_any_usage(group.color):
            continue
        argmax_role = max(intent.items(), key=lambda kv: (kv[1], kv[0].value))[0]
        items.append(
            DivergenceItem(
                role=argmax_role,
                color=group.color,
                note=f"declared '{group.representative_name}' unused in render",
            )
        )

    # DECLARED-BUT-UNUSED, relational arm: relational tokens carry no usage intent (so
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
                role=UsageRole.text,
                color=group.color,
                note=f"declared '{group.representative_name}' unused in render",
            )
        )

    # USED-BUT-UNDECLARED: prominent usage entry matching no declared token color.
    # Membership is tested against EVERY resolved declared color — including relational,
    # status, scale, and fallback classifications that carry no usage intent — because
    # "undeclared" is a statement about the stylesheet, not about intent mass: a page
    # rendering exactly its declared ``--on-primary`` text color is not undeclared.
    token_colors = [t.record.resolved for t in tokens if t.record.resolved is not None]

    def matches_any_token(color: Color) -> bool:
        return any_within(color, token_colors, MAX_MEASURED_MATCH_DELTA_E, key=lambda c: c)

    seen: set[tuple[UsageRole, str]] = set()
    for role, entries in usage.mapping.items():
        for entry in entries:
            if entry.probability < UNDECLARED_MIN_PROB:
                continue
            if matches_any_token(entry.color):
                continue
            key = (role, entry.color.hex)
            if key in seen:
                continue
            seen.add(key)
            items.append(
                DivergenceItem(
                    role=role,
                    color=entry.color,
                    note="used but undeclared",
                )
            )

    items.sort(key=lambda d: (d.note, d.color.hex))
    return items
