"""Reconcile declared-token intent with measured usage via log-linear pooling.

This module fuses two independent signals about a site's palette, in **usage space**
(:class:`~colorsense.models.UsageCategory`):

* **usage** — the measured per-category prominence over rendered *colors* produced by
  ``build_usage``; this is "what actually rendered".
* **tokens** — the declared design-token *intent* produced by ``classify_tokens``; each
  token carries a resolved :class:`Color` and a ``usage_prior`` distribution over
  :class:`UsageCategory`; this is "what the author declared".

The two are combined by **log-linear pooling** (a weighted geometric mean) with weight
``alpha`` on intent: ``alpha=0`` -> pure usage, ``alpha=1`` -> pure intent. Colors are
matched across the two sources by nearest-color under two perceptual ΔE radii — the
tight :data:`DELTA_E_MATCH` for declared-vs-declared and the looser
:data:`DELTA_E_MATCH_MEASURED` for measured-vs-declared (rationale at each constant).

The output is a posterior :class:`UsagePalette` plus a divergence report listing
declared-but-unused and used-but-undeclared discrepancies. Declared-but-unused items are
gated to **high-intent** tokens (:data:`HIGH_INTENT_ORIGINS`, where the gate's rationale
lives).

All thresholds are module-level constants, documented and tunable.
"""

from __future__ import annotations

import math
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
from colorsense.palette.inventory import DELTA_E_MATCH_BG

__all__ = ["reconcile"]

# --- Tunable constants -------------------------------------------------------

#: Nearest-color join threshold in OKLab ΔE for grouping DECLARED token colors with
#: each other. Both sides are exact computed values, so the radius stays tight.
DELTA_E_MATCH: float = 0.08

#: Join threshold for matching a MEASURED usage entry against a declared token color.
#: A measured entry's representative is a screenshot-quantizer bin whenever the cluster
#: matched one, and an element may join a bin up to the bg join radius away
#: (:data:`~colorsense.palette.inventory.DELTA_E_MATCH_BG`) — so this radius must be at
#: least that, or a pixel-perfect rendered token can fail its own intent match purely
#: from (platform-dependent) quantizer blending (see docs/how-it-works.md).
DELTA_E_MATCH_MEASURED: float = DELTA_E_MATCH_BG

#: Floor added inside the geometric mean so that a missing signal contributes
#: ``log(EPS)`` (a large finite penalty) rather than ``log(0)`` (undefined). Also makes
#: the alpha boundaries clean: at ``alpha=0`` intent collapses to ``EPS**0 == 1`` and at
#: ``alpha=1`` usage collapses similarly, so one-sided colors prune out.
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


def _aggregate_intent(tokens: list[ClassifiedToken]) -> list[_IntentGroup]:
    """Group declared tokens by color and build per-category intent scores.

    Only tokens with a resolved color and a non-empty ``usage_prior`` are considered.
    Tokens are processed sorted by ``record.name`` for determinism; a token joins an
    existing group when within :data:`DELTA_E_MATCH` of the group's color, else starts a
    new group anchored on its own resolved color.
    """
    eligible = [t for t in tokens if t.record.resolved is not None and len(t.usage_prior) > 0]
    eligible.sort(key=lambda t: t.record.name)

    groups: list[_IntentGroup] = []
    for token in eligible:
        color = token.record.resolved
        assert color is not None  # narrowed by the filter above
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
) -> tuple[UsagePalette, list[DivergenceItem]]:
    """Fuse declared intent (``tokens``) with measured ``usage`` by log-linear pooling.

    The pipeline is aggregation (:func:`_aggregate_intent`) → per-category pooling
    (:func:`_pool_category`) → divergence (:func:`_build_divergence`).

    Returns a posterior :class:`UsagePalette` and a deterministic list of
    :class:`DivergenceItem`. ``alpha`` weights intent vs. usage and is clamped to
    ``[0, 1]``. Posterior entries carry the matched measured entry's ``area`` and
    ``components``; token-only colors get ``area=0.0`` and empty ``components``.
    """
    alpha = _clamp_alpha(alpha)
    groups = _aggregate_intent(tokens)
    intents: list[dict[UsageCategory, float]] = [g.normalized_intent() for g in groups]

    posterior_mapping: dict[UsageCategory, tuple[UsageEntry, ...]] = {}
    for category in UsageCategory:  # iterate in enum order for determinism
        posterior_mapping[category] = tuple(_pool_category(category, usage, groups, intents, alpha))

    posterior = UsagePalette(mapping=posterior_mapping)
    divergence = _build_divergence(usage, groups, intents)
    return posterior, divergence


@dataclass
class _PoolCandidate:
    """One color in a category's pooling universe.

    ``measured`` is the matched usage entry (supplying ``area`` + ``components``) or
    ``None`` for a token-only color.
    """

    measured: UsageEntry | None
    color: Color
    p_usage: float
    p_intent: float


def _pool_category(
    category: UsageCategory,
    usage: UsagePalette,
    groups: list[_IntentGroup],
    intents: list[dict[UsageCategory, float]],
    alpha: float,
) -> list[UsageEntry]:
    """Build the color universe for ``category``, pool, prune, renormalize."""
    usage_entries = usage.mapping.get(category, ())

    # EMPTY-CATEGORY GATE: a category with no measured usage candidates yields an empty
    # posterior — token-only colors are NOT injected (with zero measurement the posterior
    # collapses to ``intent**alpha``, a near-uniform spread where everything survives
    # pruning; see docs/how-it-works.md). Honest emptiness beats intent-only noise;
    # declared intent can still surface through the divergence report. When measurement
    # EXISTS, token-only colors stay in the universe: pooling against real usage mass
    # crushes them unless intent is strong enough to clear MIN_POSTERIOR_PROB.
    if not usage_entries:
        return []

    candidates: list[_PoolCandidate] = []
    # Track which intent groups have already been matched to a usage entry so we
    # don't double-count them when adding token-only colors.
    matched_group: list[bool] = [False] * len(groups)

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
        matched_group[best_idx] = True
        return intents[best_idx][category]

    # Usage entries first; they own the representative Color object on a match.
    for entry in usage_entries:
        candidates.append(
            _PoolCandidate(
                measured=entry,
                color=entry.color,
                p_usage=entry.probability,
                p_intent=intent_for(entry.color),
            )
        )

    # Token-only colors with mass on this category and not already matched to a usage color.
    for gi, group in enumerate(groups):
        if category not in intents[gi] or matched_group[gi]:
            continue
        candidates.append(
            _PoolCandidate(
                measured=None,
                color=group.color,
                p_usage=0.0,
                p_intent=intents[gi][category],
            )
        )

    if not candidates:
        return []

    # Log-linear pool.
    unnorm = [(c.p_usage + EPS) ** (1.0 - alpha) * (c.p_intent + EPS) ** alpha for c in candidates]
    total = sum(unnorm)
    if total <= 0.0:  # pragma: no cover - guarded by EPS floor
        return []
    posterior_prob = [u / total for u in unnorm]

    # Prune + renormalize survivors; if pruning empties the category, keep the argmax.
    survivors = [i for i, p in enumerate(posterior_prob) if p >= MIN_POSTERIOR_PROB]
    if not survivors:
        argmax_idx = max(range(len(posterior_prob)), key=lambda i: posterior_prob[i])
        survivors = [argmax_idx]
        posterior_prob = [1.0 if i == argmax_idx else 0.0 for i in range(len(candidates))]
    else:
        surv_total = sum(posterior_prob[i] for i in survivors)
        posterior_prob = [
            (posterior_prob[i] / surv_total if i in set(survivors) else 0.0)
            for i in range(len(candidates))
        ]

    result = [
        UsageEntry(
            color=candidates[i].color,
            probability=posterior_prob[i],
            area=measured.area if (measured := candidates[i].measured) is not None else 0.0,
            components=dict(measured.components) if measured is not None else {},
        )
        for i in survivors
    ]
    result.sort(key=lambda e: (-e.probability, e.color.hex))
    return result


def _build_divergence(
    usage: UsagePalette,
    groups: list[_IntentGroup],
    intents: list[dict[UsageCategory, float]],
) -> list[DivergenceItem]:
    """Report declared-but-unused and used-but-undeclared discrepancies."""
    # All measured usage colors across every category (for nearest-color membership tests).
    usage_colors: list[Color] = [
        entry.color for entries in usage.mapping.values() for entry in entries
    ]

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

    # USED-BUT-UNDECLARED: prominent usage entry with no matching token color.
    token_colors = [g.color for g in groups]

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
