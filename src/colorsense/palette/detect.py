"""Detection-plus-ranking: the self-contained successor to build_usage + index + reconcile.

This module is the semantic-assignment core of the redesign (redesign §6, tuning-spec §2-§4).
It replaces the combined job of ``build_usage`` (role-keyed projection), ``build_color_index``
(color-keyed index), and ``reconcile`` (intent pooling + divergences) with a single pass that
keeps the three questions separate and computes them in order (redesign §4):

1. **Presence (detection)** — per ``(color, role)``, decided on *absolute* per-pair evidence,
   ``K``-independent and unnormalized, so a present-but-minor color is never pruned for being
   less prominent than its role-mates (goal 3).
2. **Ranking (salience)** — survivors are sorted relatively by ``S_final`` (goal 2).
3. **Corroboration (intent)** — declared intent enters only as a bounded multiplier ``f`` on
   the *measured* salience, so it can re-rank or rescue at the margin but never veto a color
   nor manufacture one (goal 4).

Normalization happens **last**, for display only (redesign §6.5): probabilities/weights/
prominence are derived from the surviving ``S_final`` values after detection, so nothing
normalization produces can delete a color.

The math helpers are reused from `colorsense.palette.salience` (``aggregate_salience``,
``intent_multiplier``); the perceptual radius joins from `colorsense.color.match`
(``nearest_within``, ``any_within``). The divergence behavior reproduces
`colorsense.palette.reconcile`'s two arms self-contained (its constants are redefined locally
here) so this module carries no dependency on the stage it replaces.

The internal helper `_score_candidates` is the pre-gate scoring step: it computes
``(S_measured, S_final, q_intent)`` for every ``(color, role)`` evidence record and returns a
flat list of `_Candidate` named tuples.  `detect` calls it and then applies the threshold gates
so the calibration harness in ``eval/calibrate_thresholds.py`` can sweep ``theta_present``
at ANY multiple of ``theta_noise`` without re-deriving scores.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING, NamedTuple

from colorsense.color.match import any_within, first_within, nearest_within
from colorsense.models import (
    ColorUsage,
    DivergenceItem,
    RoleEvidence,
    TokenOrigin,
    Usage,
    UsageEntry,
    UsagePalette,
    UsageRole,
)
from colorsense.palette.salience import aggregate_salience, intent_multiplier
from colorsense.palette.usage import _AREA_RANKED_ROLES

if TYPE_CHECKING:
    from colorsense.config import Config
    from colorsense.models import ClassifiedToken, Color, ComponentType

__all__ = ["_Candidate", "_score_candidates", "detect"]

# --- Tunable constants (reproduced from reconcile.py so detect.py is self-contained) ---

#: Maximum OKLab ΔE at which two DECLARED token colors fold into one intent group. Both sides
#: are exact computed values, so the ceiling stays tight (mirrors reconcile's same-named
#: constant).
MAX_TOKEN_MERGE_DELTA_E: float = 0.08

#: Maximum OKLab ΔE at which a MEASURED color is treated as the same color as a declared token
#: (intent matching) or as the same color as another measured color (divergence membership).
#: A measured representative may be a screenshot-quantizer bin an element joined up to the bg
#: join radius away, so this ceiling must absorb that platform-dependent blending (mirrors
#: reconcile's same-named constant, ``= MAX_BG_MATCH_DELTA_E``).
MAX_MEASURED_MATCH_DELTA_E: float = 0.10

#: Minimum display probability for a surviving usage entry to surface as a used-but-undeclared
#: divergence item (mirrors reconcile's same-named constant).
UNDECLARED_MIN_PROB: float = 0.15

#: Token classification origins eligible to raise a declared-but-unused divergence: only direct
#: evidence of author intent (``relational`` and ``name_rule``). Scale members, alias followers,
#: and fallbacks are excluded (mirrors reconcile's same-named gate).
HIGH_INTENT_ORIGINS: frozenset[TokenOrigin] = frozenset(
    {TokenOrigin.RELATIONAL, TokenOrigin.NAME_RULE}
)


# --- Intent grouping (declared-token side) ----------------------------------


class _IntentGroup:
    """Declared-token intent accumulated for one (approximate) color.

    Folds tokens sharing a color (within `MAX_TOKEN_MERGE_DELTA_E`) into one group, summing
    each token's ``usage_intent[role] * weight`` per role and normalizing to a per-role share
    in ``[0, 1]`` for the intent multiplier. Mirrors reconcile's ``_IntentGroup``.
    """

    def __init__(self, color: Color) -> None:
        self.color: Color = color
        self.intent_raw: dict[UsageRole, float] = defaultdict(float)
        self.token_weight: float = 0.0
        self.representative_name: str = ""
        self._representative_weight: float = float("-inf")
        self.origins: set[TokenOrigin] = set()

    def add(self, token: ClassifiedToken) -> None:
        """Fold ``token`` into this group, accumulating intent, weight, and origin."""
        for role, weight in token.usage_intent.items():
            self.intent_raw[role] += token.weight * weight
        self.token_weight += token.weight
        self.origins.add(token.origin)
        if token.weight > self._representative_weight:
            self._representative_weight = token.weight
            self.representative_name = token.record.name

    @property
    def high_intent(self) -> bool:
        """Whether any contributing token carries a high-intent origin."""
        return bool(self.origins & HIGH_INTENT_ORIGINS)

    def normalized_intent(self) -> dict[UsageRole, float]:
        """The per-role intent share in ``[0, 1]`` (empty when no intent mass)."""
        total = sum(self.intent_raw.values())
        if total <= 0.0:
            return {}
        return {role: value / total for role, value in self.intent_raw.items()}


def _group_by_color(eligible: list[ClassifiedToken]) -> list[_IntentGroup]:
    """Fold ``eligible`` tokens into `_IntentGroup`s by nearest color.

    Tokens are processed sorted by ``record.name`` for determinism; a token joins the first
    existing group within `MAX_TOKEN_MERGE_DELTA_E`, else starts a new group. Mirrors
    reconcile's ``_group_by_color`` (uses `first_within`, not `nearest_within`).

    Args:
        eligible: Tokens to group; callers must pre-filter to a resolved color.

    Returns:
        One `_IntentGroup` per distinct (approximate) color.

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


def _intent_groups(tokens: list[ClassifiedToken]) -> list[_IntentGroup]:
    """Group pooling-eligible tokens (resolved color + non-empty usage intent) by color."""
    return _group_by_color(
        [t for t in tokens if t.record.resolved is not None and len(t.usage_intent) > 0]
    )


def _relational_groups(tokens: list[ClassifiedToken]) -> list[_IntentGroup]:
    """Group relational tokens (resolved, no usage intent) for the divergence relational arm.

    Relational classifications carry an empty ``usage_intent`` (a channel route), so they never
    form intent groups nor shape ranking, but they are direct author intent and must be able to
    raise declared-but-unused. Mirrors reconcile's ``_aggregate_relational``.
    """
    return _group_by_color(
        [
            t
            for t in tokens
            if t.origin is TokenOrigin.RELATIONAL
            and t.record.resolved is not None
            and not t.usage_intent
        ]
    )


# --- Pre-gate candidate scoring (used by detect and the calibration harness) -


class _Candidate(NamedTuple):
    """Pre-gate score record for one ``(color, role)`` evidence record.

    Produced by `_score_candidates` BEFORE the ``theta_noise``/``theta_present`` gates are
    applied.  The calibration harness in ``eval/calibrate_thresholds.py`` uses this to sweep
    ``theta_present`` over a multiplier grid without re-deriving scores from scratch.

    Attributes:
        evidence: The originating `RoleEvidence` record.
        s_measured: The intent-independent measured salience ``S_measured``.
        s_final: The intent-multiplied salience ``S_final = S_measured * f``.
        q_intent: The normalized intent share for this ``(color, role)`` pair, in ``[0, 1]``.
    """

    evidence: RoleEvidence
    s_measured: float
    s_final: float
    q_intent: float


def _score_candidates(
    evidence: list[RoleEvidence],
    tokens: list[ClassifiedToken],
    config: Config,
) -> list[_Candidate]:
    """Score every ``(color, role)`` evidence record, returning pre-gate candidate tuples.

    Computes ``S_measured``, ``q_intent``, and ``S_final = S_measured * f`` for each record
    WITHOUT applying ``theta_noise`` or ``theta_present`` gates.  `detect` calls this and
    then applies the gates; the calibration harness calls this directly so it can sweep
    ``theta_present`` at any multiplier of ``theta_noise`` without re-deriving scores.

    Args:
        evidence: The per-``(canonical color, role)`` records from ``build_evidence``.
        tokens: All classified declared tokens for the site (declared intent).
        config: The loaded config; ``config.detection`` carries ``alpha`` and the per-role
            ``lambda_``/``beta``/``theta_noise``/``theta_present``.

    Returns:
        One `_Candidate` per evidence record, in the same order as ``evidence``.

    """
    groups = _intent_groups(tokens)
    intents = [group.normalized_intent() for group in groups]

    candidates: list[_Candidate] = []
    for record in evidence:
        role = record.role
        s_measured = _s_measured(record, role, config)
        q_intent = _q_intent_for(record.color, role, groups, intents)
        s_final = s_measured * intent_multiplier(q_intent, config.detection.alpha)
        candidates.append(_Candidate(record, s_measured, s_final, q_intent))
    return candidates


# --- Detection survivors -----------------------------------------------------


class _Survivor:
    """One ``(color, role)`` pair that cleared the detection gate.

    Attributes:
        evidence: The originating `RoleEvidence` record.
        s_final: The intent-multiplied salience ``S_final`` (ranking + display statistic).

    """

    __slots__ = ("evidence", "s_final")

    def __init__(self, evidence: RoleEvidence, s_final: float) -> None:
        self.evidence = evidence
        self.s_final = s_final


def _q_intent_for(
    color: Color, role: UsageRole, groups: list[_IntentGroup], intents: list[dict[UsageRole, float]]
) -> float:
    """The normalized intent share for ``(color, role)``, or ``0.0`` if no group matches.

    Matches ``color`` to the nearest intent group carrying ``role`` within
    `MAX_MEASURED_MATCH_DELTA_E`. Pre-filters to groups with the role (keeping the original
    index) so the nearest match remaps to ``intents[group_index]``, mirroring reconcile's
    ``intent_for``.

    Args:
        color: The measured color to match.
        role: The role whose intent share is wanted.
        groups: The declared-intent groups (parallel to ``intents``).
        intents: Each group's normalized per-role intent distribution.

    Returns:
        The matched group's intent share for ``role`` in ``[0, 1]``, else ``0.0``.

    """
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


def _s_measured(evidence: RoleEvidence, role: UsageRole, config: Config) -> float:
    """Measured salience ``S_measured`` for one evidence record (redesign §6.1).

    Surface roles (`_AREA_RANKED_ROLES`) use the screenshot area-truth directly; element roles
    use the peak-dominant aggregation over the per-instance saliences.

    Args:
        evidence: The `RoleEvidence` record.
        role: Its usage role (selects the formula and the aggregation params).
        config: The loaded config (``detection.roles[role]`` carries ``lambda_``/``beta``).

    Returns:
        The unnormalized measured salience.

    """
    if role in _AREA_RANKED_ROLES:
        return evidence.area
    rc = config.detection.roles[role]
    return aggregate_salience(evidence.instance_saliences, rc.lambda_, rc.beta)


def _normalized_components(components: dict[ComponentType, float]) -> dict[ComponentType, float]:
    """Normalize raw component mass to sum ~1.0 (``{}`` when there is no mass)."""
    total = sum(components.values())
    if total <= 0.0:
        return {}
    return {component: mass / total for component, mass in components.items()}


def detect(
    evidence: list[RoleEvidence],
    tokens: list[ClassifiedToken],
    config: Config,
) -> tuple[UsagePalette, tuple[ColorUsage, ...], list[DivergenceItem]]:
    """Detect, rank, corroborate, and present per-``(color, role)`` evidence.

    The single replacement for ``build_usage`` + ``build_color_index`` + ``reconcile``: it
    computes detection on absolute per-pair evidence, ranks survivors relatively by their
    intent-multiplied ``S_final``, and normalizes only for display, populating the existing
    output models so the eval harness and goldens keep working.

    Args:
        evidence: The per-``(canonical color, role)`` records from `build_evidence`.
        tokens: All classified declared tokens for the site (declared intent).
        config: The loaded config; ``config.detection`` carries ``alpha`` and the per-role
            ``lambda_``/``beta``/``theta_noise``/``theta_present``.

    Returns:
        A ``(usage_palette, color_index, divergences)`` triple: the role-keyed
        [`UsagePalette`][colorsense.UsagePalette], the color-keyed
        [`ColorUsage`][colorsense.ColorUsage] tuple, and the deterministic
        [`DivergenceItem`][colorsense.DivergenceItem] list.

    """
    # Score every (color, role) pair (pre-gate): compute S_measured, q_intent, S_final.
    candidates = _score_candidates(evidence, tokens, config)

    # Rebuild the intent groups used for divergence reporting (parallel to score_candidates'
    # internal groups; we need them again for _build_divergences).
    groups = _intent_groups(tokens)
    intents = [group.normalized_intent() for group in groups]

    # --- Detection: keep (color, role) iff S_measured >= theta_noise AND S_final >= theta_present.
    survivors_by_role: dict[UsageRole, list[_Survivor]] = defaultdict(list)
    for candidate in candidates:
        role = candidate.evidence.role
        rc = config.detection.roles[role]
        # theta_noise is intent-INDEPENDENT (the hard noise floor); theta_present may be
        # cleared with intent help. This is goal-3 by construction.
        if candidate.s_measured >= rc.theta_noise and candidate.s_final >= rc.theta_present:
            survivors_by_role[role].append(_Survivor(candidate.evidence, candidate.s_final))

    usage_palette = _build_usage_palette(survivors_by_role)
    color_index = _build_color_index(survivors_by_role)
    divergences = _build_divergences(survivors_by_role, tokens, groups, intents)
    return usage_palette, color_index, divergences


def _build_usage_palette(
    survivors_by_role: dict[UsageRole, list[_Survivor]],
) -> UsagePalette:
    """Build the role-keyed projection: rank survivors by ``S_final``, normalize for display.

    Args:
        survivors_by_role: The detection survivors keyed by role.

    Returns:
        The [`UsagePalette`][colorsense.UsagePalette] (its validator backfills empty roles).

    """
    mapping: dict[UsageRole, tuple[UsageEntry, ...]] = {}
    for role, survivors in survivors_by_role.items():
        if not survivors:
            continue
        survivors_sorted = sorted(survivors, key=lambda s: (-s.s_final, s.evidence.color.hex))
        total = sum(s.s_final for s in survivors_sorted)
        entries = tuple(
            UsageEntry(
                color=s.evidence.color,
                probability=(s.s_final / total) if total > 0.0 else 0.0,
                area=s.evidence.area,
                components=_normalized_components(s.evidence.components),
            )
            for s in survivors_sorted
        )
        mapping[role] = entries
    return UsagePalette(mapping=mapping)


def _build_color_index(
    survivors_by_role: dict[UsageRole, list[_Survivor]],
) -> tuple[ColorUsage, ...]:
    """Build the color-keyed index: per color, its surviving roles + a global prominence.

    ``prominence`` is the color's max ``S_final`` across roles, normalized by the global max
    ``S_final`` across all colors (so the top color is ``1.0``). ``area`` is the color's
    (max) evidence area, clamped. Each `Usage` slot weights a role by its share of the color's
    surviving ``S_final``.

    Args:
        survivors_by_role: The detection survivors keyed by role.

    Returns:
        The [`ColorUsage`][colorsense.ColorUsage] tuple, sorted by ``(-prominence, hex)``.

    """
    # Transpose: gather each color's surviving (role, survivor) slots, keyed by exact hex.
    by_hex: dict[str, list[tuple[UsageRole, _Survivor]]] = defaultdict(list)
    color_by_hex: dict[str, Color] = {}
    for role, survivors in survivors_by_role.items():
        for survivor in survivors:
            hex_ = survivor.evidence.color.hex
            by_hex[hex_].append((role, survivor))
            color_by_hex.setdefault(hex_, survivor.evidence.color)

    if not by_hex:
        return ()

    global_max = max(survivor.s_final for slots in by_hex.values() for _, survivor in slots)

    color_usages: list[ColorUsage] = []
    for hex_, slots in by_hex.items():
        color = color_by_hex[hex_]
        max_s_final = max(survivor.s_final for _, survivor in slots)
        total_s_final = sum(survivor.s_final for _, survivor in slots)
        area = min(max(survivor.evidence.area for _, survivor in slots), 1.0)
        prominence = (max_s_final / global_max) if global_max > 0.0 else 0.0

        usages: list[Usage] = []
        for role, survivor in slots:
            weight = (survivor.s_final / total_s_final) if total_s_final > 0.0 else 0.0
            usages.append(
                Usage(
                    role=role,
                    property_family=role.property_family,
                    weight=weight,
                    components=_normalized_components(survivor.evidence.components),
                )
            )
        usages.sort(key=lambda u: (-u.weight, u.role.value))
        color_usages.append(
            ColorUsage(
                color=color,
                prominence=min(max(prominence, 0.0), 1.0),
                area=area,
                usages=tuple(usages),
            )
        )

    color_usages.sort(key=lambda cu: (-cu.prominence, cu.color.hex))
    return tuple(color_usages)


def _build_divergences(
    survivors_by_role: dict[UsageRole, list[_Survivor]],
    tokens: list[ClassifiedToken],
    groups: list[_IntentGroup],
    intents: list[dict[UsageRole, float]],
) -> list[DivergenceItem]:
    """Report declared-but-unused and used-but-undeclared discrepancies (redesign §6.6).

    Reproduces reconcile's two arms self-contained: declared-but-unused (high-intent intent
    groups + the relational arm) and used-but-undeclared (prominent survivors matching no
    declared token color).

    Args:
        survivors_by_role: The detection survivors keyed by role.
        tokens: All classified declared tokens for the site.
        groups: The declared-intent groups (parallel to ``intents``).
        intents: Each group's normalized per-role intent distribution.

    Returns:
        The divergence items, sorted by ``(note, hex)``.

    """
    # The full set of surviving measured colors (any role), for the declared-but-unused test.
    surviving_colors: list[Color] = [
        survivor.evidence.color
        for survivors in survivors_by_role.values()
        for survivor in survivors
    ]

    def matches_any_surviving(color: Color) -> bool:
        return any_within(color, surviving_colors, MAX_MEASURED_MATCH_DELTA_E, key=lambda c: c)

    items: list[DivergenceItem] = []

    # DECLARED-BUT-UNUSED: a high-intent token color carrying intent mass (token_weight > 0,
    # strictly — the DECLARE_MIN_WEIGHT <= 0.0 fix) that matched no surviving color in any role.
    for group_index, group in enumerate(groups):
        intent = intents[group_index]
        if not intent:
            continue
        if not group.high_intent:
            continue
        if group.token_weight <= 0.0:
            continue
        if matches_any_surviving(group.color):
            continue
        argmax_role = max(intent.items(), key=lambda kv: (kv[1], kv[0].value))[0]
        items.append(
            DivergenceItem(
                role=argmax_role,
                color=group.color,
                note=f"declared '{group.representative_name}' unused in render",
            )
        )

    # DECLARED-BUT-UNUSED, relational arm: relational tokens carry no usage intent (so they
    # never join `groups`) but are direct author intent, attributed to TEXT (the ``text_on``
    # channel).
    for group in _relational_groups(tokens):
        if group.token_weight <= 0.0:
            continue
        if matches_any_surviving(group.color):
            continue
        items.append(
            DivergenceItem(
                role=UsageRole.TEXT,
                color=group.color,
                note=f"declared '{group.representative_name}' unused in render",
            )
        )

    # USED-BUT-UNDECLARED: a prominent surviving entry matching no declared token color.
    # Membership is tested against EVERY resolved declared color (intent-bearing or not):
    # "undeclared" is a statement about the stylesheet, not about intent mass.
    token_colors = [t.record.resolved for t in tokens if t.record.resolved is not None]

    def matches_any_token(color: Color) -> bool:
        return any_within(color, token_colors, MAX_MEASURED_MATCH_DELTA_E, key=lambda c: c)

    # Recompute display probabilities per role to gate on UNDECLARED_MIN_PROB.
    seen: set[tuple[UsageRole, str]] = set()
    for role, survivors in survivors_by_role.items():
        total = sum(s.s_final for s in survivors)
        if total <= 0.0:
            continue
        for survivor in survivors:
            probability = survivor.s_final / total
            if probability < UNDECLARED_MIN_PROB:
                continue
            color = survivor.evidence.color
            if matches_any_token(color):
                continue
            key = (role, color.hex)
            if key in seen:
                continue
            seen.add(key)
            items.append(DivergenceItem(role=role, color=color, note="used but undeclared"))

    items.sort(key=lambda d: (d.note, d.color.hex))
    return items
