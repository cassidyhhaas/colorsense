"""Inventory & clustering: join area-truth with semantics, then cluster per family.

This module fuses two sources of truth from a `Harvest`
and its classified elements into area-weighted `ColorCluster`
objects:

* **Area truth** — `Harvest.screenshot_bins`. Each
  `ScreenshotBin` reports a rendered color and the fraction
  of page area it covers. This is the authoritative area weight.
* **Semantic truth** — the classified elements. Each
  `ClassifiedElement` carries a ``component_dist`` over
  [`ComponentType`][colorsense.ComponentType]. The distribution is split per
  [`PropertyFamily`][colorsense.PropertyFamily] and attributed to the nearest measured
  color of the *matching* family: ``*_text`` components and ``link`` route to
  ``element.text`` (a link paints its typography), `border`
  to ``element.border``, and everything else to ``element.bg``. This family
  routing is a fixed code-level convention (the shared `ComponentType.property_family`). A
  family whose measured color is **fully transparent** (``alpha == 0``) paints nothing
  and donates no mass — without this gate, every transparent-background element
  (links, paragraphs, wrappers) piles its votes onto a phantom ``#000000``
  zero-area cluster. The background family can attribute to more than one color: a gradient
  CTA paints every opaque stop, and the element's background mass is split
  evenly across them and scaled by each stop's alpha, so a purple→blue button makes both
  purple and blue candidates without out-voting a solid one. A background vote that mixes
  CTA/action and page/surface mass is split so each share routes to its own nearest entry: the
  CTA/action share is kept off any perceptually-distinct near-black surface bin, while the
  page/surface share keeps the plain join.

Family-segregated clustering. Attribution and clustering happen **within three separate
pools**, one per [`PropertyFamily`][colorsense.PropertyFamily]: ``background``, ``text``,
and ``border``.
The ``background`` pool is seeded with one entry per `ScreenshotBin` (area truth); the
``text`` and ``border`` pools start empty, since text/border colors paint no screenshot
area. A family's mass only ever nearest-joins or clusters against entries in its own
pool — so a low-area near-black text color can no longer be absorbed by a high-area
background bin of a perceptually-near hex and report the bin's hex. Each pool's
representative is chosen by what is authoritative for that family: ``background`` by
largest area weight (hex tiebreak), ``text``/``border`` by largest in-family vote mass
(hex tiebreak). The flat union of all three pools' `ColorCluster`s is returned; because
each cluster's ``component_mass`` only contains its own family's components (by
construction), the downstream fusion/detect/third-party stages operate on the flat
list unchanged.

Perceptual distance is measured exclusively with
`colorsense.color.primitives.delta_e` (OKLab ``deltaEOK``), whose units are small;
the thresholds below are tuned for that scale.

Determinism. There is no randomness. Wherever iteration order could affect the result we sort by a
stable key (color ``hex``). The flat union is assembled in fixed family order
(background, text, border), each family pre-sorted by ``(-area_weight, hex)``, then a
**stable** final sort by ``(-area_weight, hex)`` preserves that family order for any
same-(area, hex) tie. The same input always yields identical output.
"""

from __future__ import annotations

from collections import defaultdict

from colorsense.color.match import nearest_within
from colorsense.color.primitives import ciede2000, delta_e, is_painting
from colorsense.models import (
    ClassifiedElement,
    Color,
    ColorCluster,
    ComponentType,
    Harvest,
    HarvestedElement,
    PropertyFamily,
    UsageRole,
)

__all__ = ["build_inventory"]


def _bg_fill_colors(element: HarvestedElement) -> list[Color]:
    """Return the color(s) the element's background paints, for bg-channel attribution.

    A solid (opaque) ``background-color`` is the single fill. When it paints nothing
    (``alpha == 0``) the gradient fill stops take over — a gradient CTA's
    ``background-color`` is transparent, so its brand colors live only in
    ``bg_gradient_stops`` (populated for clickable pill CTAs only).

    Args:
        element: The harvested element whose background is being attributed.

    Returns:
        The opaque fill color(s) the background paints, or ``[]`` when the element paints
        no background at all.

    """
    bg = element.bg
    if bg is not None and is_painting(bg):
        return [bg]
    return list(element.bg_gradient_stops)


# Maximum OKLab deltaEOK distance at which a classified element's BG channel color is
# considered "the same painted surface" as an existing entry (typically a screenshot
# bin) and so donates its component mass to it. deltaEOK units are small. Backgrounds
# match loosely (0.10): screenshot quantization + anti-aliasing smear large surfaces,
# so a generous join radius is what ties element bgs back to their area-truth bins.
MAX_BG_MATCH_DELTA_E: float = 0.10

# Maximum distance for the TEXT and BORDER channels — deliberately tighter (0.05, the
# cluster radius below). Text/border colors are exact computed values, not quantized
# pixels, and dark colors sit perceptually close in OKLab: at 0.10 near-black text gets
# absorbed into adjacent dark surface bins instead of forming its own zero-area entry,
# erasing the text hierarchy from the usage view (see docs/how-it-works.md).
MAX_TEXT_BORDER_MATCH_DELTA_E: float = 0.05

# Maximum OKLab deltaEOK distance at which two entries are merged into a single
# perceptual cluster. Kept <= the match radii so that only truly near-identical
# colors collapse together.
MAX_CLUSTER_MERGE_DELTA_E: float = 0.05

# --- Near-white distinctness check (text/border pools only) ----------------------------
# OKLab deltaEOK is materially non-uniform near the lightness extremes: the 0.05 join/cluster
# radius above spans ~6.5-8.5 ΔE2000 near white, so it collapses clearly-distinct near-white
# *text* colors into one entry. The canonical case is GitHub's `#ffffff` (dominant body text)
# vs Primer's `#f0f6fc`: OKLab ΔE 0.031 (merges) but CIEDE2000 4.02 (plainly distinct) — the
# white text never surfaces because it is absorbed into the `#f0f6fc` entry. OKLab's coarseness
# is an asset for the *background* pool (it denoises screenshot quantization / anti-alias
# smear), so the check is confined to the text/border pools where colors are exact computed
# values, not quantized pixels.
#
# In that regime the check adds a second condition to BOTH the attribution join and the cluster
# merge: two near-white colors merge only if they are also within `NEAR_WHITE_MERGE_MAX_DE2000`
# in CIEDE2000 — the accurate identity metric near white (`colorsense.color.primitives.ciede2000`).
# The radius is a *denoising* radius, deliberately LOOSER than the 1.0 ΔE2000 identity floor: at
# the identity floor the check over-fragments (anti-alias variants ~1-3 ΔE2000 from their
# canonical color split off as noise); ~3 ΔE2000 still collapses those variants while splitting
# genuinely-distinct tokens like `#ffffff`/`#f0f6fc`. Measured on the offline quality panel a
# 3.0 radius recovers GitHub white in text+link (and resend's near-white text tokens) with no
# net change to noise or role winners.
NEAR_WHITE_MIN_LIGHTNESS: float = 0.90
NEAR_WHITE_MERGE_MAX_DE2000: float = 3.0

# The families whose colors are exact computed values (text, border) — these get the near-white
# distinctness check. The background pool is excluded: its colors are quantized screenshot pixels
# where OKLab's coarseness usefully denoises antialiasing, so it keeps the pure OKLab radius.
_EXACT_COLOR_FAMILIES: frozenset[PropertyFamily] = frozenset(
    {PropertyFamily.TEXT, PropertyFamily.BORDER}
)

# --- Near-black CTA/action background distinctness check --------------------------------
# The same OKLab non-uniformity that buries near-white *text* (the check above) also buries small
# dark CTA/secondary-button backgrounds into the big near-black *screenshot bins* of the background
# pool. Measured case: disco's two dark CTA anchors paint `#030711`, which sits OKLab 0.029 from the
# `#050505` footer bin (inside both the 0.10 join radius and the 0.05 cluster radius) but CIEDE2000
# 4.33 away — plainly distinct. Their `cta_bg` mass is absorbed into the footer bin, so `#030711`
# never surfaces in `cta` and `#050505` shows up there as noise instead.
#
# Unlike the text/border check this CANNOT be applied to the whole background pool — OKLab's
# coarseness there is a load-bearing denoiser for the large near-extreme page/surface bins
# (a global swap regressed the panel, +25 noise). So this check is scoped THREE ways: (1) only
# the near-black extreme, (2) only the CTA/action *share* of a bg vote — a mixed vote is split so
# its page/surface/banner share stays on the plain join (see `CTA_ACTION_BG_COMPONENTS`), and
# (3) only at the denoising CIEDE2000 radius. The symmetric near-WHITE variant was prototyped and
# REJECTED on the panel (winners -1, noise +4): near-white "buttons" are faint translucent tints
# (e.g. resend's
# `#d6ebfd` at alpha 0.11) that, once split off white, fragment into several distinct near-white
# entries that themselves become noise and even dethrone correct near-white CTA winners — so only
# solid near-black CTA backgrounds get the check. The radius matches the text/border check's 3.0
# ΔE2000 *denoising* threshold: it splits `#030711`/`#050505` (4.33) while keeping genuine
# near-black anti-alias variants merged (`#000000`/`#010101` is 0.16, `#08090b`/`#050505` is 1.05).
NEAR_BLACK_MAX_LIGHTNESS: float = 0.15
NEAR_BLACK_MERGE_MAX_DE2000: float = 3.0

# Background-channel components whose mass routes to the `cta`/`action` usage roles. At attribution
# the check does not divert an element's *whole* bg vote: it splits the vote and diverts only the
# share carried by these components, leaving any page/surface/banner share on the plain OKLab
# join (see the attribution loop in `build_inventory`). This matters because the bg-channel softmax
# (`classify/components._finalize_distribution`) can leave a near-black element carrying BOTH kinds
# of mass — a dark, full-width *clickable* panel reads as page/surface (area/hero signals) and as a
# button (clickable), so e.g. `{page_bg: 0.9, cta_bg: 0.1}` survives pruning. Diverting only the
# 0.1 CTA share keeps the check's promise that page/surface attribution is untouched, while still
# surfacing the distinct dark CTA. A dominance threshold (check only if CTA mass leads) was the
# rejected alternative: it would silently drop genuine dark CTAs whose classifier mass is split.
# The CTA gate is presence-based so any nonzero CTA/action share is recovered. At the cluster step
# the gate is per-entry presence (`_entry_has_cta_action_mass`): an entry carrying any CTA/action
# mass is kept off a CIEDE2000-distinct near-black surface bin; pure page/surface entries are not.
CTA_ACTION_BG_COMPONENTS: frozenset[ComponentType] = frozenset(
    {ComponentType.CTA_BG, ComponentType.BADGE, ComponentType.BUTTON_SECONDARY}
)


def _is_distinct_near_black_pair(a: Color, b: Color) -> bool:
    """Whether ``a`` and ``b`` are two near-black colors that must NOT be merged.

    True iff both colors are near-black (lightness <= `NEAR_BLACK_MAX_LIGHTNESS`) yet farther
    apart than `NEAR_BLACK_MERGE_MAX_DE2000` in CIEDE2000 — i.e. OKLab would wrongly collapse a
    dark CTA/secondary-button background into the big near-black screenshot bin next to it. This
    tests perceptual distinctness only; the CTA/action scoping is applied separately by the
    callers. The CIEDE2000 call runs only after the cheap lightness gate, so only on the
    near-black subset.

    Args:
        a: First color to compare.
        b: Second color to compare.

    Returns:
        ``True`` if both are near-black yet CIEDE2000-distinct (must not merge), else
        ``False``.

    """
    return (
        a.lightness <= NEAR_BLACK_MAX_LIGHTNESS
        and b.lightness <= NEAR_BLACK_MAX_LIGHTNESS
        and ciede2000(a, b) > NEAR_BLACK_MERGE_MAX_DE2000
    )


def _nearest_mergeable_near_black_entry(
    color: Color, pool: list[_Entry], radius: float
) -> int | None:
    """Nearest background-pool entry to ``color`` that the near-black check permits merging into.

    Like `colorsense.color.match.nearest_within` (argmin over OKLab `delta_e` within ``radius``,
    ``<=``) but skips any entry that is a distinct near-black pair with ``color``. Used only for
    the CTA/action share of a bg vote; ordinary background attribution keeps the plain shared
    helper.

    Args:
        color: The color being attributed.
        pool: The background-pool entries to match against.
        radius: Maximum OKLab `delta_e` join distance.

    Returns:
        The index of the nearest permitted entry, or ``None`` (a fresh entry) when every
        in-radius entry is a forbidden near-black pair.

    """
    nearest_index: int | None = None
    nearest_distance = radius
    for idx, entry in enumerate(pool):
        distance = delta_e(color, entry.color)
        if distance <= nearest_distance and not _is_distinct_near_black_pair(color, entry.color):
            nearest_distance = distance
            nearest_index = idx
    return nearest_index


# Per-family join radius: background loose, text/border tight (see the constants above).
_MATCH_BY_FAMILY: dict[PropertyFamily, float] = {
    PropertyFamily.BACKGROUND: MAX_BG_MATCH_DELTA_E,
    PropertyFamily.TEXT: MAX_TEXT_BORDER_MATCH_DELTA_E,
    PropertyFamily.BORDER: MAX_TEXT_BORDER_MATCH_DELTA_E,
}


def _is_distinct_near_white_pair(a: Color, b: Color) -> bool:
    """Whether ``a`` and ``b`` are two near-white colors that must NOT be merged (text/border).

    True iff both colors are near-white (lightness >= `NEAR_WHITE_MIN_LIGHTNESS`) yet farther
    apart than `NEAR_WHITE_MERGE_MAX_DE2000` in CIEDE2000 — i.e. OKLab would wrongly collapse
    two perceptually-distinct near-white text colors. The CIEDE2000 call is reached only after
    the cheap lightness gates, so it runs on the small near-white subset, never the whole pool.
    Callers apply this only within the text/border pools (`_EXACT_COLOR_FAMILIES`).

    Args:
        a: First color to compare.
        b: Second color to compare.

    Returns:
        ``True`` if both are near-white yet CIEDE2000-distinct (must not merge), else
        ``False``.

    """
    return (
        a.lightness >= NEAR_WHITE_MIN_LIGHTNESS
        and b.lightness >= NEAR_WHITE_MIN_LIGHTNESS
        and ciede2000(a, b) > NEAR_WHITE_MERGE_MAX_DE2000
    )


def _nearest_mergeable_near_white_entry(
    color: Color, pool: list[_Entry], radius: float
) -> int | None:
    """Nearest text/border-pool entry to ``color`` that the near-white check permits merging into.

    Identical to `colorsense.color.match.nearest_within` (argmin over OKLab `delta_e`, running
    best seeded at ``radius``, ``<=`` so the last of equal-distance candidates wins) except an
    entry that is a distinct near-white pair with ``color`` is skipped. The background pool keeps
    the plain shared helper.

    Args:
        color: The color being attributed.
        pool: The text/border-pool entries to match against.
        radius: Maximum OKLab `delta_e` join distance.

    Returns:
        The index of the nearest permitted entry, or ``None`` (a fresh entry) when every
        in-radius entry is a forbidden near-white pair.

    """
    nearest_index: int | None = None
    nearest_distance = radius
    for idx, entry in enumerate(pool):
        distance = delta_e(color, entry.color)
        if distance <= nearest_distance and not _is_distinct_near_white_pair(color, entry.color):
            nearest_distance = distance
            nearest_index = idx
    return nearest_index


class _Entry:
    """A mutable working color entry: a color, its area weight and its raw vote mass.

    ``role_instances`` and ``role_components`` are additional, defaulted fields used only by
    the successor fusion module (`colorsense.palette.fusion`): ``role_instances`` records the
    per-instance salience sigma_i routed to this entry keyed by usage role, and
    ``role_components`` records the raw summed component mass (``mass * weight``) routed to
    this entry, keyed by usage role then component type. `build_inventory` never reads or
    writes either, so their presence is invisible to the shipping inventory path.
    """

    __slots__ = ("area_weight", "color", "role_components", "role_instances", "vote_mass")

    def __init__(self, color: Color, area_weight: float) -> None:
        self.color = color
        self.area_weight = area_weight
        self.vote_mass: dict[ComponentType, float] = defaultdict(float)
        self.role_instances: dict[UsageRole, list[float]] = defaultdict(list)
        self.role_components: dict[UsageRole, dict[ComponentType, float]] = defaultdict(
            lambda: defaultdict(float)
        )


def _find(parent: list[int], i: int) -> int:
    """Union-find root with path compression.

    Args:
        parent: The union-find parent array (mutated in place by path compression).
        i: Index whose set root is sought.

    Returns:
        The representative root index of ``i``'s set.

    """
    root = i
    while parent[root] != root:
        root = parent[root]
    while parent[i] != root:
        parent[i], i = root, parent[i]
    return root


def _union(parent: list[int], a: int, b: int) -> None:
    """Union the sets containing ``a`` and ``b`` (lower root wins for stability).

    Args:
        parent: The union-find parent array (mutated in place).
        a: Index in the first set.
        b: Index in the second set.

    """
    ra, rb = _find(parent, a), _find(parent, b)
    if ra == rb:
        return
    if ra < rb:
        parent[rb] = ra
    else:
        parent[ra] = rb


def _union_merges_distinct_near_white_pair(
    parent: list[int], entries: list[_Entry], i: int, j: int
) -> bool:
    """Whether merging ``i``'s and ``j``'s current clusters would co-locate a forbidden pair.

    Checks every cross-pair between the two sets being merged against
    `_is_distinct_near_white_pair`. Because every union is checked this way, no cluster can ever
    contain a forbidden pair — closing the transitivity gap a per-edge check leaves open
    (a bridge color near two distinct colors would otherwise chain them together). The pools
    are tiny (a handful of text/border entries), so the cross-scan is cheap.

    Args:
        parent: The union-find parent array.
        entries: The pool entries being clustered (parallel to ``parent``).
        i: Index in the first cluster.
        j: Index in the second cluster.

    Returns:
        ``True`` if merging the two clusters would co-locate a distinct near-white pair,
        else ``False``.

    """
    root_i, root_j = _find(parent, i), _find(parent, j)
    if root_i == root_j:
        return False
    left = [k for k in range(len(entries)) if _find(parent, k) == root_i]
    right = [k for k in range(len(entries)) if _find(parent, k) == root_j]
    return any(
        _is_distinct_near_white_pair(entries[p].color, entries[q].color)
        for p in left
        for q in right
    )


def _total_vote_mass(entry: _Entry) -> float:
    """Total vote mass on a text/border-pool entry (all of one family by construction).

    Args:
        entry: The pool entry to sum.

    Returns:
        The summed vote mass across the entry's components.

    """
    return sum(entry.vote_mass.values())


def _entry_has_cta_action_mass(entry: _Entry) -> bool:
    """Whether a background-pool entry carries any CTA/action component mass.

    Args:
        entry: The background-pool entry to test.

    Returns:
        ``True`` if the entry carries mass from any `CTA_ACTION_BG_COMPONENTS` component,
        else ``False``.

    """
    return any(component in CTA_ACTION_BG_COMPONENTS for component in entry.vote_mass)


def _union_merges_distinct_near_black_cta_pair(
    parent: list[int], entries: list[_Entry], i: int, j: int
) -> bool:
    """Whether merging ``i``/``j``'s clusters would co-locate a forbidden CTA/action bg pair.

    Background-pool counterpart of `_union_merges_distinct_near_white_pair`: checks every cross-pair
    between the two sets being merged against `_is_distinct_near_black_pair`, but only forbids when
    at least one member of the offending pair carries CTA/action mass — so ordinary
    page/surface/banner clustering is never blocked (the OKLab denoiser stays intact there).
    Transitivity-safe by the same induction as the near-white check: every union is checked, so no
    cluster ever co-locates a forbidden pair.

    Args:
        parent: The union-find parent array.
        entries: The pool entries being clustered (parallel to ``parent``).
        i: Index in the first cluster.
        j: Index in the second cluster.

    Returns:
        ``True`` if merging the two clusters would co-locate a distinct near-black pair in
        which at least one member carries CTA/action mass, else ``False``.

    """
    root_i, root_j = _find(parent, i), _find(parent, j)
    if root_i == root_j:
        return False
    left = [k for k in range(len(entries)) if _find(parent, k) == root_i]
    right = [k for k in range(len(entries)) if _find(parent, k) == root_j]
    return any(
        _is_distinct_near_black_pair(entries[p].color, entries[q].color)
        and (_entry_has_cta_action_mass(entries[p]) or _entry_has_cta_action_mass(entries[q]))
        for p in left
        for q in right
    )


def _cluster_groups(entries: list[_Entry], family: PropertyFamily) -> list[list[_Entry]]:
    """Union-find group one family's entry pool into perceptual clusters (pure grouping only).

    The side-effect-free grouping core shared by `_cluster_pool` (the shipping inventory path)
    and `colorsense.palette.fusion.build_evidence` (the successor). It applies the family's
    distinctness guards exactly as `_cluster_pool` did inline — the near-white check for
    text/border families and the near-black CTA/action check for the background family — so the
    grouping is identical to the inventory path, only its consumption differs.

    Args:
        entries: The family's working entry pool to cluster.
        family: The [`PropertyFamily`][colorsense.PropertyFamily] the pool belongs to
            (selects which distinctness check applies).

    Returns:
        One ``list[_Entry]`` per perceptual cluster (group membership only — no representative
        selection or aggregation); ``[]`` for an empty pool. Group order follows union-find root
        order, which is deterministic for a given input.

    """
    entry_count = len(entries)
    if entry_count == 0:
        return []

    is_exact_color_family = family in _EXACT_COLOR_FAMILIES
    is_background_family = family is PropertyFamily.BACKGROUND
    parent = list(range(entry_count))
    for i in range(entry_count):
        for j in range(i + 1, entry_count):
            if delta_e(entries[i].color, entries[j].color) > MAX_CLUSTER_MERGE_DELTA_E:
                continue
            # Union-find is transitive: blocking only the direct (i, j) edge would let a
            # near-white "bridge" color (close to both) merge two distinct near-white colors
            # into one cluster anyway. So reject the union if it would co-locate ANY
            # forbidden pair across the two sets being merged — by induction, no resulting
            # cluster can ever contain a forbidden pair. (Skipped for non-exact-color families:
            # the branch is gated on `is_exact_color_family`, so their clustering is
            # byte-identical to the plain union-find.)
            if is_exact_color_family and _union_merges_distinct_near_white_pair(
                parent, entries, i, j
            ):
                continue
            # Background pool: keep a CTA/action background off a CIEDE2000-distinct near-black
            # surface bin (e.g. disco's `#030711` CTA vs the `#050505` footer bin, OKLab 0.029 but
            # ΔE2000 4.33). Scoped to CTA/action-bearing pairs so the page/surface denoiser is
            # untouched; transitivity-safe like the near-white check above.
            if is_background_family and _union_merges_distinct_near_black_cta_pair(
                parent, entries, i, j
            ):
                continue
            _union(parent, i, j)

    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(entry_count):
        groups[_find(parent, i)].append(i)
    return [[entries[i] for i in members] for members in groups.values()]


def _group_representative(group: list[_Entry], family: PropertyFamily) -> _Entry:
    """The representative entry of one perceptual cluster (shared family-specific rule).

    The single source of truth for picking a cluster's canonical color, shared by
    `_cluster_pool` and `colorsense.palette.fusion.build_evidence`: ``background`` picks the
    largest area weight (smallest hex breaks ties and the all-zero case); ``text``/``border``
    pick the largest in-family vote mass (hex tiebreak — they paint no screenshot area).

    Args:
        group: The cluster's member entries (non-empty).
        family: The [`PropertyFamily`][colorsense.PropertyFamily] selecting the rule.

    Returns:
        The member chosen as the cluster's representative.

    """
    if family is PropertyFamily.BACKGROUND:
        # Area truth: largest area weight, ties (and all-zero) broken by smallest hex.
        return min(group, key=lambda entry: (-entry.area_weight, entry.color.hex))
    # Text/border paint no area: rank by in-family vote mass, hex tiebreak.
    return min(group, key=lambda entry: (-_total_vote_mass(entry), entry.color.hex))


def _cluster_pool(entries: list[_Entry], family: PropertyFamily) -> list[ColorCluster]:
    """Union-find cluster one family's entry pool into `ColorCluster`s.

    Representative selection is family-specific: ``background`` picks the largest area
    weight (hex tiebreak — area is authoritative for surfaces); ``text``/``border`` pick
    the largest in-family vote mass (hex tiebreak — they paint no screenshot area).

    Args:
        entries: The family's working entry pool to cluster.
        family: The [`PropertyFamily`][colorsense.PropertyFamily] the pool belongs to
            (selects the representative rule and which distinctness check applies).

    Returns:
        The family's `ColorCluster`s, pre-sorted by ``(-area_weight, hex)``; ``[]`` for an
        empty pool.

    """
    clusters: list[ColorCluster] = []
    for group in _cluster_groups(entries, family):
        representative = _group_representative(group, family)

        total_area = sum(entry.area_weight for entry in group)

        summed_mass: dict[ComponentType, float] = defaultdict(float)
        for entry in group:
            for component, mass in entry.vote_mass.items():
                summed_mass[component] += mass

        mix: dict[ComponentType, float] = {}
        total = sum(summed_mass.values())
        if total > 0.0:
            mix = {component: mass / total for component, mass in summed_mass.items()}

        clusters.append(
            ColorCluster(
                color=representative.color,
                area_weight=total_area,
                member_count=len(group),
                component_mix=mix,
                component_mass=dict(summed_mass),
            )
        )

    clusters.sort(key=lambda cluster: (-cluster.area_weight, cluster.color.hex))
    return clusters


def build_inventory(harvest: Harvest, classified: list[ClassifiedElement]) -> list[ColorCluster]:
    """Join area-truth with element semantics and cluster perceptually-near colors per family.

    See the module docstring for the data sources and family segregation. The algorithm is:

    1. Seed the ``background`` pool with one entry per `ScreenshotBin`
       (authoritative area weight, empty mix); the ``text`` and ``border`` pools start
       empty (text/border colors paint no screenshot area).
    2. For each classified element, split its ``component_dist`` into per-family
       sub-distributions via `ComponentType.property_family` and process the families in a
       fixed (background, text, border) order. For each family whose measured color is
       non-``None`` and whose sub-distribution is non-empty, find the nearest
       entry **within that family's pool only** by `delta_e`. If that
       nearest entry is within the family's join radius (`MAX_BG_MATCH_DELTA_E` for
       background, the tighter `MAX_TEXT_BORDER_MATCH_DELTA_E` for text/border), add
       the family's mass (each element weighted equally, raw) into the entry's
       mix. Otherwise append a new entry to that pool from the family's color
       with ``area_weight = 0.0`` so its semantics are not lost. New entries are
       appended in element order then family order, which is deterministic.
    3. Cluster each pool independently via union-find under `MAX_CLUSTER_MERGE_DELTA_E`. For
       each group emit one `ColorCluster`: representative color =
       member with the largest area weight for ``background`` (largest in-family
       vote mass for ``text``/``border``), ties / all-zero broken by ``hex``;
       ``area_weight`` = sum of member weights (0 for text/border); ``member_count`` =
       group size; ``component_mass`` = the raw summed member mixes (un-normalized vote
       mass — the usage view needs cross-cluster magnitude); ``component_mix`` = the same
       sums normalized to ~1.0 (empty stays empty).

    Args:
        harvest: The page `Harvest`, whose ``screenshot_bins`` seed the background pool's
            area truth.
        classified: The classified DOM elements supplying per-component semantic mass.

    Returns:
        The flat union of all three pools' clusters, assembled in fixed family order
        (background, text, border) and then **stably** sorted by ``area_weight``
        descending, ties broken by ``hex`` (the stable sort preserves family order for
        same-(area, hex) ties, keeping the output deterministic).

    """
    pools: dict[PropertyFamily, list[_Entry]] = {
        PropertyFamily.BACKGROUND: [
            _Entry(bin_.color, bin_.area_fraction) for bin_ in harvest.screenshot_bins
        ],
        PropertyFamily.TEXT: [],
        PropertyFamily.BORDER: [],
    }

    # STEP 1b: attribute element semantics to the nearest entry in the family's pool
    # (or a new one), routing each component's mass to the family that paints it.
    for classification in classified:
        if not classification.component_distribution:
            continue

        # Split the distribution into per-family sub-distributions. Family routing is the
        # shared `ComponentType.property_family` (same convention the component classifier
        # normalizes by).
        family_distributions: dict[PropertyFamily, dict[ComponentType, float]] = {
            PropertyFamily.BACKGROUND: {},
            PropertyFamily.TEXT: {},
            PropertyFamily.BORDER: {},
        }
        for component, mass in classification.component_distribution.items():
            family_distributions[component.property_family][component] = mass

        # Fixed family order for determinism. The background family can carry more than one
        # fill color — a gradient CTA paints every opaque stop (see `_bg_fill_colors`) —
        # so each family resolves to a list; text/border are always single.
        for family, colors in (
            (PropertyFamily.BACKGROUND, _bg_fill_colors(classification.element)),
            (PropertyFamily.TEXT, [classification.element.text]),
            (PropertyFamily.BORDER, [classification.element.border]),
        ):
            family_distribution = family_distributions[family]
            if not family_distribution:
                continue
            # A fully-transparent fill color (alpha == 0, e.g. the default
            # ``background-color: transparent``) paints nothing: attributing its
            # mass would invent a phantom #000000 cluster.
            fills = [color for color in colors if color is not None and is_painting(color)]
            if not fills:
                continue

            # The family's mass is attributed (and later clustered) only within its
            # own pool — never across families.
            pool = pools[family]

            # Split the element's family mass evenly across its fill colors so a
            # multi-stop gradient is not double-counted (a purple->blue button donates
            # the same total cta_bg mass as a solid one, half to each stop). On the
            # background and border families the per-fill share is additionally scaled by the
            # fill's alpha, so a faint ``bg-primary/10`` tint or a near-transparent hairline
            # border votes its intended hex in proportion to how much it actually paints; text
            # is not alpha-scaled (a low-opacity glyph still reads as that text color). Scaling
            # the border family is what stops a swarm of near-transparent hairline borders (e.g.
            # 48 ``alpha 0.08`` icon-container outlines) from out-voting the one opaque
            # divider that actually structures the page.
            # The text/border pools apply the near-white check so OKLab cannot collapse two
            # perceptually-distinct near-white text colors onto one entry. The background pool
            # splits its vote (below) so the near-black CTA check diverts ONLY CTA/action mass;
            # everything else keeps the shared helper.
            is_exact_color_family = family in _EXACT_COLOR_FAMILIES
            # A background vote that carries ANY CTA/action component is split in two: the
            # CTA/action share routes through the near-black check (so a small dark button
            # background is not absorbed into a CIEDE2000-distinct near-black screenshot bin —
            # disco `#030711` vs the `#050505` footer bin), while the page/surface/banner share
            # keeps the plain OKLab join. That split is what keeps the check from touching
            # page/surface attribution when a near-black element carries both kinds of mass (a
            # dark clickable panel). See `CTA_ACTION_BG_COMPONENTS` for why the CTA gate is
            # presence-based, not dominance.
            vote_has_cta_action_mass = family is PropertyFamily.BACKGROUND and any(
                component in CTA_ACTION_BG_COMPONENTS for component in family_distribution
            )
            radius = _MATCH_BY_FAMILY[family]
            fill_count = len(fills)
            for color in fills:
                weight = (
                    color.alpha
                    if family in (PropertyFamily.BACKGROUND, PropertyFamily.BORDER)
                    else 1.0
                ) / fill_count

                # Resolve routing as (component-mass subset, target entry index) pairs. Every
                # lookup runs against the SAME pre-update pool, so the plain page/surface
                # share can't accidentally snap onto the fresh entry the checked CTA share is
                # about to create — it sees only the entries that existed before this vote.
                if is_exact_color_family:
                    routes = [
                        (
                            family_distribution,
                            _nearest_mergeable_near_white_entry(color, pool, radius),
                        )
                    ]
                elif vote_has_cta_action_mass:
                    cta_action_mass = {
                        c: m
                        for c, m in family_distribution.items()
                        if c in CTA_ACTION_BG_COMPONENTS
                    }
                    non_cta_mass = {
                        c: m
                        for c, m in family_distribution.items()
                        if c not in CTA_ACTION_BG_COMPONENTS
                    }
                    routes = [
                        (
                            cta_action_mass,
                            _nearest_mergeable_near_black_entry(color, pool, radius),
                        )
                    ]
                    if non_cta_mass:
                        routes.append(
                            (
                                non_cta_mass,
                                nearest_within(color, pool, radius, key=lambda e: e.color),
                            )
                        )
                else:
                    routes = [
                        (
                            family_distribution,
                            nearest_within(color, pool, radius, key=lambda e: e.color),
                        )
                    ]

                # Materialize. Routes that fall through to a new entry share one (same `color`),
                # so a split vote whose shares both miss every existing entry stays one entry.
                shared_new_entry: _Entry | None = None
                for route_mass, nearest_index in routes:
                    if nearest_index is None:
                        if shared_new_entry is None:
                            shared_new_entry = _Entry(color, 0.0)
                            pool.append(shared_new_entry)
                        target = shared_new_entry
                    else:
                        target = pool[nearest_index]
                    for component, mass in route_mass.items():
                        target.vote_mass[component] += mass * weight

    # STEP 2: cluster each family pool independently, then assemble the flat union in
    # fixed family order and stably re-sort by (-area_weight, hex).
    clusters: list[ColorCluster] = []
    for family in (PropertyFamily.BACKGROUND, PropertyFamily.TEXT, PropertyFamily.BORDER):
        clusters.extend(_cluster_pool(pools[family], family))

    clusters.sort(key=lambda cluster: (-cluster.area_weight, cluster.color.hex))
    return clusters
