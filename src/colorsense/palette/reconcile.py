"""Reconcile declared-token intent with measured usage via log-linear pooling.

This module fuses two independent signals about a site's palette:

* **usage** — the measured per-role likelihood over candidate *colors* produced by
  ``assign_roles``; this is "what actually rendered".
* **tokens** — the declared design-token *intent* produced by
  ``classify_tokens``; each token carries a resolved :class:`Color` and a
  ``palette_prior`` distribution over :class:`PaletteRole`; this is "what the author
  declared".

The two are combined by **log-linear pooling** (a weighted geometric mean) with weight
``alpha`` on intent: ``alpha=0`` -> pure usage, ``alpha=1`` -> pure intent. Colors are
matched across the two sources by nearest-color within a perceptual ΔE threshold.

The output is a posterior :class:`RoleResults` plus a divergence report listing
declared-but-unused and used-but-undeclared discrepancies.

All thresholds are module-level constants, documented and tunable.
"""

from __future__ import annotations

import math

from colorsense.color.primitives import delta_e
from colorsense.models import (
    ClassifiedToken,
    Color,
    DivergenceItem,
    PaletteCandidate,
    PaletteRole,
    RoleResults,
)

__all__ = ["reconcile"]

# --- Tunable constants -------------------------------------------------------

#: Nearest-color join threshold in OKLab ΔE. Two colors within this distance are
#: treated as the "same" color when joining usage candidates and token colors.
DELTA_E_MATCH: float = 0.08

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

#: Minimum posterior-independent usage probability for a usage candidate to surface as a
#: used-but-undeclared divergence item.
UNDECLARED_MIN_PROB: float = 0.15


# --- Internal aggregation structures ----------------------------------------


class _IntentGroup:
    """A color group accumulating declared-token intent for one (approx) color."""

    def __init__(self, color: Color) -> None:
        self.color: Color = color
        self.intent_raw: dict[PaletteRole, float] = {}
        self.token_weight: float = 0.0
        self.rep_name: str = ""
        self._rep_weight: float = -math.inf

    def add(self, token: ClassifiedToken) -> None:
        for role, prior in token.palette_prior.items():
            self.intent_raw[role] = self.intent_raw.get(role, 0.0) + token.weight * prior
        self.token_weight += token.weight
        if token.weight > self._rep_weight:
            self._rep_weight = token.weight
            self.rep_name = token.record.name

    def normalized_intent(self) -> dict[PaletteRole, float]:
        total = sum(self.intent_raw.values())
        if total <= 0.0:
            return {}
        return {role: val / total for role, val in self.intent_raw.items()}


def _aggregate_intent(tokens: list[ClassifiedToken]) -> list[_IntentGroup]:
    """STEP 1 — group declared tokens by color and build per-role intent scores.

    Only tokens with a resolved color and a non-empty ``palette_prior`` are considered.
    Tokens are processed sorted by ``record.name`` for determinism; a token joins an
    existing group when within :data:`DELTA_E_MATCH` of the group's color, else starts a
    new group anchored on its own resolved color.
    """
    eligible = [t for t in tokens if t.record.resolved is not None and len(t.palette_prior) > 0]
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
    usage: RoleResults,
    tokens: list[ClassifiedToken],
    alpha: float = 0.4,
) -> tuple[RoleResults, list[DivergenceItem]]:
    """Fuse declared intent (``tokens``) with measured ``usage`` by log-linear pooling.

    Returns a posterior :class:`RoleResults` and a deterministic list of
    :class:`DivergenceItem`. ``alpha`` weights intent vs. usage and is clamped to
    ``[0, 1]``.
    """
    alpha = _clamp_alpha(alpha)
    groups = _aggregate_intent(tokens)
    intents: list[dict[PaletteRole, float]] = [g.normalized_intent() for g in groups]

    # All palette roles that appear in either signal.
    roles: set[PaletteRole] = set(usage.mapping.keys())
    for intent in intents:
        roles.update(intent.keys())

    posterior_mapping: dict[PaletteRole, tuple[PaletteCandidate, ...]] = {}

    for role in PaletteRole:  # iterate in enum order for determinism
        if role in roles:
            posterior_mapping[role] = tuple(_pool_role(role, usage, groups, intents, alpha))
        else:
            # Backfill: the mapping always contains every PaletteRole, even with no
            # candidates, so callers can index any role without a KeyError.
            posterior_mapping[role] = ()

    posterior = RoleResults(mapping=posterior_mapping)
    divergence = _build_divergence(usage, groups, intents)
    return posterior, divergence


def _pool_role(
    role: PaletteRole,
    usage: RoleResults,
    groups: list[_IntentGroup],
    intents: list[dict[PaletteRole, float]],
    alpha: float,
) -> list[PaletteCandidate]:
    """STEPS 2-3 — build the color universe for ``role``, pool, prune, renormalize."""
    usage_cands = usage.mapping.get(role, ())

    # Color universe entries: (representative_color, p_usage, area, p_intent).
    rep_colors: list[Color] = []
    p_usage_by_idx: list[float] = []
    area_by_idx: list[float] = []
    p_intent_by_idx: list[float] = []
    # Track which intent groups have already been matched to a usage candidate so we
    # don't double-count them when adding token-only colors.
    matched_group: list[bool] = [False] * len(groups)

    def intent_for(color: Color) -> float:
        best_idx: int | None = None
        best_d = DELTA_E_MATCH
        for gi, group in enumerate(groups):
            if role not in intents[gi]:
                continue
            d = delta_e(color, group.color)
            if d <= best_d:
                best_d = d
                best_idx = gi
        if best_idx is None:
            return 0.0
        matched_group[best_idx] = True
        return intents[best_idx][role]

    # Usage candidates first; they own the representative Color object on a match.
    for cand in usage_cands:
        rep_colors.append(cand.color)
        p_usage_by_idx.append(cand.probability)
        area_by_idx.append(cand.area)
        p_intent_by_idx.append(intent_for(cand.color))

    # Token-only colors with mass on this role and not already matched to a usage color.
    for gi, group in enumerate(groups):
        if role not in intents[gi] or matched_group[gi]:
            continue
        rep_colors.append(group.color)
        p_usage_by_idx.append(0.0)
        area_by_idx.append(0.0)
        p_intent_by_idx.append(intents[gi][role])

    if not rep_colors:
        return []

    # Log-linear pool.
    unnorm = [
        (p_usage_by_idx[i] + EPS) ** (1.0 - alpha) * (p_intent_by_idx[i] + EPS) ** alpha
        for i in range(len(rep_colors))
    ]
    total = sum(unnorm)
    if total <= 0.0:  # pragma: no cover - guarded by EPS floor
        return []
    posterior_prob = [u / total for u in unnorm]

    # Prune + renormalize survivors; if pruning empties the role, keep the argmax.
    survivors = [i for i, p in enumerate(posterior_prob) if p >= MIN_POSTERIOR_PROB]
    if not survivors:
        argmax_idx = max(range(len(posterior_prob)), key=lambda i: posterior_prob[i])
        survivors = [argmax_idx]
        posterior_prob = [1.0 if i == argmax_idx else 0.0 for i in range(len(rep_colors))]
    else:
        surv_total = sum(posterior_prob[i] for i in survivors)
        posterior_prob = [
            (posterior_prob[i] / surv_total if i in set(survivors) else 0.0)
            for i in range(len(rep_colors))
        ]

    result = [
        PaletteCandidate(
            color=rep_colors[i],
            probability=posterior_prob[i],
            area=area_by_idx[i],
            evidence={
                "p_usage": p_usage_by_idx[i],
                "p_intent": p_intent_by_idx[i],
                "alpha": alpha,
            },
        )
        for i in survivors
    ]
    result.sort(key=lambda c: (-c.probability, c.color.hex))
    return result


def _build_divergence(
    usage: RoleResults,
    groups: list[_IntentGroup],
    intents: list[dict[PaletteRole, float]],
) -> list[DivergenceItem]:
    """STEP 4 — declared-but-unused and used-but-undeclared discrepancies."""
    # All usage candidate colors across every role (for nearest-color membership tests).
    usage_colors: list[Color] = [cand.color for cands in usage.mapping.values() for cand in cands]

    def matches_any_usage(color: Color) -> bool:
        return any(delta_e(color, uc) <= DELTA_E_MATCH for uc in usage_colors)

    items: list[DivergenceItem] = []

    # DECLARED-BUT-UNUSED: token color with intent mass but no rendered usage match.
    for gi, group in enumerate(groups):
        intent = intents[gi]
        if not intent:
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
                note=f"declared '{group.rep_name}' unused in render",
            )
        )

    # USED-BUT-UNDECLARED: prominent usage candidate with no matching token color.
    token_colors = [g.color for g in groups]

    def matches_any_token(color: Color) -> bool:
        return any(delta_e(color, tc) <= DELTA_E_MATCH for tc in token_colors)

    seen: set[tuple[PaletteRole, str]] = set()
    for role, cands in usage.mapping.items():
        for cand in cands:
            if cand.probability < UNDECLARED_MIN_PROB:
                continue
            if matches_any_token(cand.color):
                continue
            key = (role, cand.color.hex)
            if key in seen:
                continue
            seen.add(key)
            items.append(
                DivergenceItem(
                    role=role,
                    color=cand.color,
                    note="used but undeclared",
                )
            )

    items.sort(key=lambda d: (d.note, d.color.hex))
    return items
