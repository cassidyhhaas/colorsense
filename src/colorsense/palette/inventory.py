"""Inventory & clustering: join area-truth with semantics, then cluster per family.

This module fuses two sources of truth from a `Harvest`
and its classified elements into area-weighted `ColorCluster`
objects:

* **Area truth** — `Harvest.screenshot_bins`. Each
  `ScreenshotBin` reports a rendered color and the fraction
  of page area it covers. This is the authoritative area weight.
* **Semantic truth** — the classified elements. Each
  `ClassifiedElement` carries a ``component_dist`` over
  [`ComponentType`][colorsense.ComponentType]. The distribution is split per color
  channel and attributed to the nearest measured color of the *matching* channel:
  ``*_text`` components and ``link`` route to ``element.text``
  (a link paints its typography), `border`
  to ``element.border``, and everything else to ``element.bg``. This channel
  routing is a fixed code-level convention (the shared `models.channel_for`). A channel
  whose measured color is **fully transparent** (``alpha == 0``) paints nothing
  and donates no mass — without this gate, every transparent-background element
  (links, paragraphs, wrappers) piles its votes onto a phantom ``#000000``
  zero-area cluster. The bg channel can attribute to more than one color: a gradient CTA
  (see `_bg_fill_colors`) paints every opaque stop, and the element's bg mass is split
  evenly across them and scaled by each stop's alpha, so a purple→blue button makes both
  purple and blue candidates without out-voting a solid one.

Family-segregated clustering
----------------------------
Attribution and clustering happen **within three separate pools**, one per
[`PropertyFamily`][colorsense.PropertyFamily]: ``background`` (the bg channel),
``text`` (the text channel), and ``border`` (the border channel). The ``background``
pool is seeded with one entry per `ScreenshotBin` (area truth); the ``text`` and
``border`` pools start empty, since text/border colors paint no screenshot area. A
channel's mass only ever nearest-joins or clusters against entries in its own family's
pool — so a low-area near-black text color can no longer be absorbed by a high-area
background bin of a perceptually-near hex and report the bin's hex. Each pool's
representative is chosen by what is authoritative for that family: ``background`` by
largest area weight (hex tiebreak), ``text``/``border`` by largest in-family vote mass
(hex tiebreak). The flat union of all three pools' `ColorCluster`s is returned; because
each cluster's ``component_mass`` only contains its own family's components (by
construction), the downstream usage/reconcile/third-party stages operate on the flat
list unchanged.

Perceptual distance is measured exclusively with
`colorsense.color.primitives.delta_e` (OKLab ``deltaEOK``), whose units are small;
the thresholds below are tuned for that scale.

Determinism
-----------
There is no randomness. Wherever iteration order could affect the result we sort by a
stable key (color ``hex``). The flat union is assembled in fixed family order
(background, text, border), each family pre-sorted by ``(-area_weight, hex)``, then a
**stable** final sort by ``(-area_weight, hex)`` preserves that family order for any
same-(area, hex) tie. The same input always yields identical output.
"""

from __future__ import annotations

from collections import defaultdict

from colorsense.color.match import nearest_within
from colorsense.color.primitives import delta_e, is_painting
from colorsense.models import (
    ClassifiedElement,
    Color,
    ColorCluster,
    ComponentType,
    Harvest,
    HarvestedElement,
    PropertyFamily,
    channel_for,
)

__all__ = ["build_inventory"]


def _bg_fill_colors(element: HarvestedElement) -> list[Color]:
    """The color(s) the element's background paints, for bg-channel attribution.

    A solid (opaque) ``background-color`` is the single fill. When it paints nothing
    (``alpha == 0``) the gradient fill stops take over — a gradient CTA's
    ``background-color`` is transparent, so its brand colors live only in
    ``bg_gradient_stops`` (populated for clickable pill CTAs only; see
    `HarvestedElement`). Returns ``[]`` when the element paints no background at all.
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
DELTA_E_MATCH_BG: float = 0.10

# Maximum distance for the TEXT and BORDER channels — deliberately tighter (0.05, the
# cluster radius below). Text/border colors are exact computed values, not quantized
# pixels, and dark colors sit perceptually close in OKLab: at 0.10 near-black text gets
# absorbed into adjacent dark surface bins instead of forming its own zero-area entry,
# erasing the text hierarchy from the usage view (see docs/how-it-works.md).
DELTA_E_MATCH_TEXT_BORDER: float = 0.05

# Maximum OKLab deltaEOK distance at which two entries are merged into a single
# perceptual cluster. Kept <= the match radii so that only truly near-identical
# colors collapse together.
DELTA_E_CLUSTER: float = 0.05

# Per-channel join radius: bg loose, text/border tight (see the constants above).
_MATCH_BY_CHANNEL: dict[str, float] = {
    "bg": DELTA_E_MATCH_BG,
    "text": DELTA_E_MATCH_TEXT_BORDER,
    "border": DELTA_E_MATCH_TEXT_BORDER,
}

# The PropertyFamily a color channel attributes into. Each channel's mass is clustered
# only against entries in its own family's pool (see the module docstring).
_FAMILY_BY_CHANNEL: dict[str, PropertyFamily] = {
    "bg": PropertyFamily.background,
    "text": PropertyFamily.text,
    "border": PropertyFamily.border,
}


# Channel routing is the shared `models.channel_for` (the single source of truth, used
# identically by the component classifier's per-channel normalization). Aliased here for
# the local call sites; the convention itself lives in `models.py`.
_channel_for = channel_for


class _Entry:
    """A mutable working color entry: a color, its area weight and a raw mix."""

    __slots__ = ("area_weight", "color", "component_mix")

    def __init__(self, color: Color, area_weight: float) -> None:
        self.color = color
        self.area_weight = area_weight
        self.component_mix: dict[ComponentType, float] = defaultdict(float)


def _find(parent: list[int], i: int) -> int:
    """Union-find root with path compression."""
    root = i
    while parent[root] != root:
        root = parent[root]
    while parent[i] != root:
        parent[i], i = root, parent[i]
    return root


def _union(parent: list[int], a: int, b: int) -> None:
    """Union the sets containing ``a`` and ``b`` (lower root wins for stability)."""
    ra, rb = _find(parent, a), _find(parent, b)
    if ra == rb:
        return
    if ra < rb:
        parent[rb] = ra
    else:
        parent[ra] = rb


def _in_family_mass(entry: _Entry) -> float:
    """Total vote mass on a text/border-pool entry (all of one family by construction)."""
    return sum(entry.component_mix.values())


def _cluster_pool(entries: list[_Entry], family: PropertyFamily) -> list[ColorCluster]:
    """Union-find cluster one family's entry pool into `ColorCluster`s.

    Representative selection is family-specific: ``background`` picks the largest area
    weight (hex tiebreak — area is authoritative for surfaces); ``text``/``border`` pick
    the largest in-family vote mass (hex tiebreak — they paint no screenshot area). The
    returned clusters are pre-sorted by ``(-area_weight, hex)``.
    """
    entry_count = len(entries)
    if entry_count == 0:
        return []

    parent = list(range(entry_count))
    for i in range(entry_count):
        for j in range(i + 1, entry_count):
            if delta_e(entries[i].color, entries[j].color) <= DELTA_E_CLUSTER:
                _union(parent, i, j)

    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(entry_count):
        groups[_find(parent, i)].append(i)

    clusters: list[ColorCluster] = []
    for members in groups.values():
        group = [entries[i] for i in members]

        if family is PropertyFamily.background:
            # Area truth: largest area weight, ties (and all-zero) broken by smallest hex.
            representative = min(group, key=lambda entry: (-entry.area_weight, entry.color.hex))
        else:
            # Text/border paint no area: rank by in-family vote mass, hex tiebreak.
            representative = min(
                group, key=lambda entry: (-_in_family_mass(entry), entry.color.hex)
            )

        total_area = sum(entry.area_weight for entry in group)

        summed_mass: dict[ComponentType, float] = defaultdict(float)
        for entry in group:
            for component, mass in entry.component_mix.items():
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
    2. For each classified element, split its ``component_dist`` into per-channel
       sub-distributions via `_channel_for` and process the channels in a
       fixed (bg, text, border) order. For each channel whose measured color is
       non-``None`` and whose sub-distribution is non-empty, find the nearest
       entry **within that channel's family pool only** by `delta_e`. If that
       nearest entry is within the channel's join radius (`DELTA_E_MATCH_BG` for
       bg, the tighter `DELTA_E_MATCH_TEXT_BORDER` for text/border), add
       the channel's mass (each element weighted equally, raw) into the entry's
       mix. Otherwise append a new entry to that pool from the channel's color
       with ``area_weight = 0.0`` so its semantics are not lost. New entries are
       appended in element order then channel order, which is deterministic.
    3. Cluster each pool independently via union-find under `DELTA_E_CLUSTER`. For
       each group emit one `ColorCluster`: representative color =
       member with the largest area weight for ``background`` (largest in-family
       vote mass for ``text``/``border``), ties / all-zero broken by ``hex``;
       ``area_weight`` = sum of member weights (0 for text/border); ``member_count`` =
       group size; ``component_mass`` = the raw summed member mixes (un-normalized vote
       mass — the usage view needs cross-cluster magnitude); ``component_mix`` = the same
       sums normalized to ~1.0 (empty stays empty).

    Returns the flat union of all three pools' clusters, assembled in fixed family order
    (background, text, border) and then **stably** sorted by ``area_weight`` descending,
    ties broken by ``hex`` (the stable sort preserves family order for same-(area, hex)
    ties, keeping the output deterministic).
    """
    pools: dict[PropertyFamily, list[_Entry]] = {
        PropertyFamily.background: [
            _Entry(bin_.color, bin_.area_fraction) for bin_ in harvest.screenshot_bins
        ],
        PropertyFamily.text: [],
        PropertyFamily.border: [],
    }

    # STEP 1b: attribute element semantics to the nearest entry in the channel's family
    # pool (or a new one), routing each component's mass to the channel that paints it.
    for classification in classified:
        if not classification.component_dist:
            continue

        # Split the distribution into per-channel sub-distributions.
        channel_distributions: dict[str, dict[ComponentType, float]] = {
            "bg": {},
            "text": {},
            "border": {},
        }
        for component, mass in classification.component_dist.items():
            channel_distributions[_channel_for(component)][component] = mass

        # Fixed channel order for determinism. The bg channel can carry more than one
        # fill color — a gradient CTA paints every opaque stop (see `_bg_fill_colors`) —
        # so each channel resolves to a list; text/border are always single.
        for channel, colors in (
            ("bg", _bg_fill_colors(classification.element)),
            ("text", [classification.element.text]),
            ("border", [classification.element.border]),
        ):
            channel_distribution = channel_distributions[channel]
            if not channel_distribution:
                continue
            # A fully-transparent fill color (alpha == 0, e.g. the default
            # ``background-color: transparent``) paints nothing: attributing its
            # mass would invent a phantom #000000 cluster.
            fills = [color for color in colors if color is not None and is_painting(color)]
            if not fills:
                continue

            # The channel's mass is attributed (and later clustered) only within its
            # own family's pool — never across families.
            pool = pools[_FAMILY_BY_CHANNEL[channel]]

            # Split the element's channel mass evenly across its fill colors so a
            # multi-stop gradient is not double-counted (a purple->blue button donates
            # the same total cta_bg mass as a solid one, half to each stop). On the bg
            # channel the per-fill share is additionally scaled by the fill's alpha, so a
            # faint ``bg-primary/10`` tint votes its intended (saturated) hex in
            # proportion to how much it actually paints; text/border are not alpha-scaled.
            fill_count = len(fills)
            for color in fills:
                weight = (color.alpha if channel == "bg" else 1.0) / fill_count

                nearest_index = nearest_within(
                    color, pool, _MATCH_BY_CHANNEL[channel], key=lambda entry: entry.color
                )

                if nearest_index is None:
                    new_entry = _Entry(color, 0.0)
                    for component, mass in channel_distribution.items():
                        new_entry.component_mix[component] += mass * weight
                    pool.append(new_entry)
                else:
                    target = pool[nearest_index]
                    for component, mass in channel_distribution.items():
                        target.component_mix[component] += mass * weight

    # STEP 2: cluster each family pool independently, then assemble the flat union in
    # fixed family order and stably re-sort by (-area_weight, hex).
    clusters: list[ColorCluster] = []
    for family in (PropertyFamily.background, PropertyFamily.text, PropertyFamily.border):
        clusters.extend(_cluster_pool(pools[family], family))

    clusters.sort(key=lambda cluster: (-cluster.area_weight, cluster.color.hex))
    return clusters
