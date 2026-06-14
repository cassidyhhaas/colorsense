"""Usage assembly: the canonical color-keyed index and the role-keyed projection.

Two complementary views are built here from the same `ColorCluster` list:

* **The role-keyed projection** ([`UsagePalette`][colorsense.UsagePalette], via
  `build_usage`): per [`UsageRole`][colorsense.UsageRole]
  (page/surface/banner/cta/action/text/link/border), a probability-ranked list of
  [`UsageEntry`][colorsense.UsageEntry] colors. Answers "which colors paint each role?".
* **The canonical color-keyed index** (a [`ColorUsage`][colorsense.ColorUsage] tuple, via
  `build_color_index`): per measured color, every role it appears in plus an overall
  ``prominence`` ranking. Answers "how is each color used?". Because the inventory now
  clusters per [`PropertyFamily`][colorsense.PropertyFamily], one hex can appear as
  several clusters (e.g. the same gray as a text cluster *and* a border cluster, or
  ``#ffffff`` as both a background bin and a text color); `build_color_index` groups
  clusters by **exact** ``Color.hex`` so each distinct color surfaces as a single atom
  listing all its usages.

Both preserve the design's actual structure: a neutral-layered design's gray
text/border hierarchy appears directly, rather than being flattened into a coarse
aesthetic split.

Design notes
------------
* The two public entry points (`build_usage`, `build_color_index`) take *only* the cluster
  list (no [`Config`][colorsense.Config]); every threshold is a documented, module-level
  **tunable** constant.
* `ROLE_COMPONENTS` — the usage-role → component-type collapse — is a fixed code-level
  convention, exactly like the inventory's component → color-channel routing
  (``models.channel_for``): it describes what the taxonomy *means*, not a tunable weight,
  so it lives in code rather than the YAML config. ``cta_text`` and ``third_party`` are
  deliberately absent from every role: ``cta_text`` never surfaces on real sites, and
  ``third_party`` flows to ``AnalysisResult.third_party_colors`` instead. The inverse
  (`COMPONENT_ROLE`) is built once and asserted to partition every routed component to
  exactly one role.
* Role-keyed prominence is scored differently for *surface* roles vs *element* roles,
  deliberately (see `_AREA_RANKED_ROLES`; worked examples in docs/how-it-works.md):
    - **structural-surface roles** (page/surface/banner): prominence ∝ the cluster's
      screenshot ``area_weight``. Area is authoritative for surfaces — vote counts would let
      repeated small elements outrank the page background — and a dominant-area cluster
      anchors the ranking, so the result is both correct and stable across OSes. Only
      clusters with nonzero vote mass in the role participate; a zero-area cluster scores 0
      and prunes naturally unless it is the argmax fallback.
    - **element-color roles** (cta/action/text/link/border): prominence ∝ ``log1p`` of the
      cluster's raw in-role vote mass. These paint negligible screenshot area, so area would
      be wrong twice over — it buries the brand color under the page background (an
      area-ranked ``cta`` collapses to the page-background hex) and it is OS-non-deterministic
      (which near-zero-area button forms its own median-cut bin flips across OSes). Vote mass
      is DOM-derived, so it ranks the real element color — and the primary button over the
      secondary — correctly and stably. ``log1p`` damps it **sub-linearly** so that element
      *count* (e.g. many neutral "ghost" buttons) cannot drown a high-confidence brand color;
      it is monotonic, so within-role *ordering* is unchanged and only the shares compress.
* Color-keyed ``prominence`` blends area-truth and vote mass (see `_prominence` /
  `PROMINENCE_AREA_WEIGHT`): a first-cut heuristic, monotonic in both, weighted toward
  area so dominant backgrounds rank high while zero-area brand accents are not buried.
* Everything is deterministic: iteration is over stable sort orders, ties are broken by
  color ``hex`` (smallest wins — the shared `prune_distribution` convention), and there
  is no randomness.
"""

from __future__ import annotations

import math

from colorsense.models import (
    Color,
    ColorCluster,
    ColorUsage,
    ComponentType,
    Usage,
    UsageEntry,
    UsagePalette,
    UsageRole,
    family_of,
)
from colorsense.palette._pruning import prune_distribution

__all__ = ["ROLE_COMPONENTS", "build_color_index", "build_usage"]

CT = ComponentType

# ---------------------------------------------------------------------------
# Tunable constants (all module-level and documented).
# ---------------------------------------------------------------------------

# Entries below this within-role probability share are pruned (then survivors are
# renormalized). If pruning would empty a non-empty role, the argmax entry is kept
# at probability 1.0 instead (the shared `prune_distribution` step, used by every
# palette ranking stage).
MIN_SHARE: float = 0.02

# Color-keyed prominence blend weight on the (normalized) area term; the remaining
# ``1 - PROMINENCE_AREA_WEIGHT`` weights the (normalized, log1p-damped) total routed vote
# mass. Both terms are normalized to [0, 1] across the cluster set before blending, so the
# blend is a convex combination in [0, 1]. Tilted toward area so the dominant page/surface
# backgrounds top the ranking, while the vote-mass term keeps zero-area brand accents
# (CTA/link colors, which carry mass but no screenshot area) from sinking to the bottom.
#
# FIRST-CUT HEURISTIC: this constant (and the blend shape) is an initial, intentionally
# simple monotonic choice — it has NOT been empirically tuned the way the role taxonomy
# was against the 12-site corpus. Revisit it with measured data; the intent (area primary,
# vote mass secondary, both monotonic) is the contract, not the exact 0.7.
PROMINENCE_AREA_WEIGHT: float = 0.7

# Usage-role -> component-type collapse. A fixed code-level convention (see the module
# docstring). ``cta_text`` and ``third_party`` map to NO role and are excluded from both
# usage views; third-party widget colors surface via ``AnalysisResult.third_party_colors``.
ROLE_COMPONENTS: dict[UsageRole, tuple[ComponentType, ...]] = {
    UsageRole.page: (CT.page_bg,),
    UsageRole.surface: (CT.card_bg, CT.modal_bg, CT.hero_bg, CT.input_bg),
    UsageRole.banner: (CT.header_bg, CT.nav_bg, CT.footer_bg),
    UsageRole.cta: (CT.cta_bg,),
    UsageRole.action: (CT.button_secondary, CT.badge),
    UsageRole.text: (
        CT.page_text,
        CT.header_text,
        CT.nav_text,
        CT.footer_text,
        CT.hero_text,
        CT.card_text,
    ),
    UsageRole.link: (CT.link,),
    UsageRole.border: (CT.border,),
}


def _build_component_role() -> dict[ComponentType, UsageRole]:
    """Invert `ROLE_COMPONENTS`, asserting it partitions every routed component once.

    A component appearing under two roles (or `ROLE_COMPONENTS` drifting from the
    taxonomy) would be a silent routing bug; the assertion turns it into a load-time
    failure. ``cta_text`` and ``third_party`` are intentionally unrouted.
    """
    inverse: dict[ComponentType, UsageRole] = {}
    for role, components in ROLE_COMPONENTS.items():
        for component in components:
            assert component not in inverse, (
                f"{component} routed to both {inverse[component]} and {role}"
            )
            inverse[component] = role
    return inverse


# Component-type -> usage-role routing (the inverse of `ROLE_COMPONENTS`), built and
# partition-checked once at import.
COMPONENT_ROLE: dict[ComponentType, UsageRole] = _build_component_role()

# Roles whose role-keyed prominence is the cluster's screenshot ``area_weight`` rather than
# its vote mass. These name the structural *surfaces* of a layout — the page canvas, raised
# surfaces (cards/modals/hero/inputs), and header/nav/footer bands — where the right question
# is "how much screen does this color cover" and a dominant-area cluster anchors the ranking,
# so area is both correct and stable.
#
# Every OTHER role names an *element* color (cta/action button fills, text, links, borders),
# ranked by ``log1p`` of in-role vote mass. These paint negligible screenshot area, so area
# is the wrong signal twice over: (1) it is **incorrect** — the page background out-areas
# every button, so an area-ranked ``cta``/``action`` collapses to the page-background hex and
# the actual brand CTA color is pruned below `MIN_SHARE`; (2) it is **non-deterministic** —
# which of two near-zero-area buttons forms its own median-cut screenshot bin (→ area) flips
# across OSes on identical input. Vote mass is DOM-derived (computed colors, not rendered
# pixels), so it is stable across OSes and ranks the brand CTA — and the primary button over
# the secondary — correctly. (Note the cta/action *property family* stays ``background`` for
# the ``family_of`` rollups and the color-keyed index; only their *ranking signal* differs.)
_AREA_RANKED_ROLES: frozenset[UsageRole] = frozenset(
    {UsageRole.page, UsageRole.surface, UsageRole.banner}
)


def _role_masses_from(
    component_mass: dict[ComponentType, float],
) -> dict[UsageRole, dict[ComponentType, float]]:
    """Split a raw ``component_mass`` mapping by usage role.

    A color used in multiple ways (e.g. the same gray as text *and* border) correctly
    lands in multiple roles, each with its respective component masses. Components with
    no role (``cta_text``, ``third_party``) are dropped.
    """
    split: dict[UsageRole, dict[ComponentType, float]] = {}
    for comp, mass in component_mass.items():
        if mass <= 0.0:
            continue
        role = COMPONENT_ROLE.get(comp)
        if role is None:
            continue
        split.setdefault(role, {})[comp] = mass
    return split


def _role_masses(cluster: ColorCluster) -> dict[UsageRole, dict[ComponentType, float]]:
    """Split a cluster's raw ``component_mass`` by usage role (see `_role_masses_from`)."""
    return _role_masses_from(cluster.component_mass)


# ---------------------------------------------------------------------------
# Role-keyed projection (UsagePalette).
# ---------------------------------------------------------------------------


def _build_entries(
    scored: list[tuple[ColorCluster, float, dict[ComponentType, float]]],
) -> tuple[UsageEntry, ...]:
    """Normalize prominence scores into probabilities, prune, renormalize, and rank.

    ``scored`` is ``(cluster, prominence, per-component masses)`` per participating
    cluster. The prune/renormalize/argmax-fallback step is the shared
    `prune_distribution` (which also covers the all-zero-prominence background case —
    every score ties, so the smallest hex wins outright); output is sorted by
    ``(-probability, hex)``.
    """
    kept = prune_distribution(
        scored,
        [score for _, score, _ in scored],
        min_share=MIN_SHARE,
        tie_key=lambda item: item[0].color.hex,
    )

    entries: list[UsageEntry] = []
    for (cluster, _score, masses), prob in kept:
        mass_total = sum(masses.values())
        components = (
            {comp: mass / mass_total for comp, mass in masses.items()} if mass_total > 0.0 else {}
        )
        entries.append(
            UsageEntry(
                color=cluster.color,
                probability=prob,
                area=cluster.area_weight,
                components=components,
            )
        )
    entries.sort(key=lambda e: (-e.probability, e.color.hex))
    return tuple(entries)


def build_usage(clusters: list[ColorCluster]) -> UsagePalette:
    """Build the **measured** role-keyed usage projection from the color inventory.

    For each usage role, the participating clusters (those with nonzero raw vote mass routed
    to the role via `ROLE_COMPONENTS`) are scored by prominence — screenshot area for the
    structural-surface roles (``page``/``surface``/``banner``), ``log1p`` of in-role vote mass
    for every other (element-color) role (see `_AREA_RANKED_ROLES`) — normalized to
    probabilities, pruned below `MIN_SHARE`
    (argmax kept if pruning empties the role), and ranked by ``(-probability, hex)``. A role
    with no mass anywhere maps to ``()`` (the [`UsagePalette`][colorsense.UsagePalette]
    validator backfills it). An empty cluster list yields an empty (all-``()``) palette.
    """
    per_role: dict[UsageRole, list[tuple[ColorCluster, float, dict[ComponentType, float]]]]
    per_role = {role: [] for role in UsageRole}

    # Stable iteration: clusters sorted by (-area_weight, hex), matching inventory order.
    for cluster in sorted(clusters, key=lambda c: (-c.area_weight, c.color.hex)):
        for role, masses in _role_masses(cluster).items():
            if role in _AREA_RANKED_ROLES:
                # Structural surface: area-proportional (see `_AREA_RANKED_ROLES`).
                prominence = cluster.area_weight
            else:
                # Element color: sub-linear in vote mass (see `_AREA_RANKED_ROLES`).
                prominence = math.log1p(sum(masses.values()))
            per_role[role].append((cluster, prominence, masses))

    mapping = {role: _build_entries(scored) for role, scored in per_role.items() if scored}
    return UsagePalette(mapping=mapping)


# ---------------------------------------------------------------------------
# Color-keyed canonical index (ColorUsage tuple).
# ---------------------------------------------------------------------------


def _prominence(area_norm: float, mass_norm: float) -> float:
    """Blend normalized area and normalized vote mass into the overall ranking signal.

    A convex combination (see `PROMINENCE_AREA_WEIGHT`): monotonic in both inputs,
    weighted toward area. Both inputs are pre-normalized to ``[0, 1]`` across the cluster
    set by the caller, so the result is in ``[0, 1]``.
    """
    return PROMINENCE_AREA_WEIGHT * area_norm + (1.0 - PROMINENCE_AREA_WEIGHT) * mass_norm


def _color_usages(role_masses: dict[UsageRole, dict[ComponentType, float]]) -> tuple[Usage, ...]:
    """Build a color's `Usage` slots from its per-role component masses.

    ``weight`` is the role's total mass over the color's total routed mass (so the slots'
    weights sum to ~1.0); ``components`` is the per-ComponentType normalized mass within
    the role. Sorted by ``(-weight, role.value)``.
    """
    total_routed = sum(sum(masses.values()) for masses in role_masses.values())
    if total_routed <= 0.0:
        return ()

    usages: list[Usage] = []
    for role, masses in role_masses.items():
        role_mass = sum(masses.values())
        if role_mass <= 0.0:
            continue
        components = {comp: mass / role_mass for comp, mass in masses.items()}
        usages.append(
            Usage(
                role=role,
                property_family=family_of(role),
                weight=role_mass / total_routed,
                components=components,
            )
        )
    usages.sort(key=lambda u: (-u.weight, u.role.value))
    return tuple(usages)


def build_color_index(clusters: list[ColorCluster]) -> tuple[ColorUsage, ...]:
    """Build the canonical color-keyed index from the (family-segregated) color inventory.

    The inventory clusters per [`PropertyFamily`][colorsense.PropertyFamily], so a single
    color can arrive as several clusters (the same gray as a text cluster and a border
    cluster, or ``#ffffff`` as both a background bin and a text color). To present **one
    atom per color**, clusters are first grouped by **exact** ``Color.hex`` and merged:
    component masses are summed, area is the max member ``area_weight``, and the shared hex
    is the representative. Exact equality is intentional — it fully preserves
    family-distinct hexes (e.g. ``#e5e5ea`` border vs ``#ffffff`` bg), which already
    differ; perceptually-near-but-distinct colors stay as separate atoms.

    For each merged group with at least one routed usage (``cta_text``/``third_party``-only
    groups are dropped — third-party colors surface via
    ``AnalysisResult.third_party_colors``), emit a [`ColorUsage`][colorsense.ColorUsage]
    whose ``usages`` describe every role the color appears in and whose ``prominence``
    blends the group's normalized area with its normalized (``log1p``-damped) total routed
    vote mass (see `_prominence`). The tuple is sorted by ``prominence`` descending,
    ``hex`` tiebreak. An empty cluster list yields ``()``.
    """
    # Group clusters by exact hex (first-seen order over the inventory's stable sort),
    # merging same-hex clusters across families into one atom.
    grouped: dict[str, list[ColorCluster]] = {}
    for cluster in sorted(clusters, key=lambda c: (-c.area_weight, c.color.hex)):
        grouped.setdefault(cluster.color.hex, []).append(cluster)

    # Per merged atom: summed component mass, max area, routed role masses; drop atoms
    # with no routed usage (e.g. third-party-only).
    routed: list[tuple[Color, float, dict[UsageRole, dict[ComponentType, float]], float]] = []
    for members in grouped.values():
        merged_mass: dict[ComponentType, float] = {}
        for cluster in members:
            for comp, mass in cluster.component_mass.items():
                merged_mass[comp] = merged_mass.get(comp, 0.0) + mass
        role_masses = _role_masses_from(merged_mass)
        total_mass = sum(sum(m.values()) for m in role_masses.values())
        if not role_masses or total_mass <= 0.0:
            continue
        # Members share the hex; any color is representative (use the first, in sort order).
        color = members[0].color
        area = max(cluster.area_weight for cluster in members)
        routed.append((color, area, role_masses, total_mass))

    if not routed:
        return ()

    # Normalize area and log1p(vote mass) to [0, 1] across the atom set.
    max_area = max((area for _, area, _, _ in routed), default=0.0)
    damped_masses = [math.log1p(total) for _, _, _, total in routed]
    max_damped = max(damped_masses, default=0.0)

    color_usages: list[ColorUsage] = []
    for (color, area, role_masses, _total), damped in zip(routed, damped_masses, strict=True):
        area_norm = area / max_area if max_area > 0.0 else 0.0
        mass_norm = damped / max_damped if max_damped > 0.0 else 0.0
        color_usages.append(
            ColorUsage(
                color=color,
                prominence=_prominence(area_norm, mass_norm),
                area=area,
                usages=_color_usages(role_masses),
            )
        )

    color_usages.sort(key=lambda cu: (-cu.prominence, cu.color.hex))
    return tuple(color_usages)
