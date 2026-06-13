"""Inventory & clustering: join area-truth with semantics, then cluster.

This module fuses two sources of truth from a `Harvest`
and its classified elements into area-weighted `ColorCluster`
objects:

* **Area truth** — `Harvest.screenshot_bins`. Each
  `ScreenshotBin` reports a rendered color and the fraction
  of page area it covers. This is the authoritative area weight.
* **Semantic truth** — the classified elements. Each
  `ClassifiedElement` carries a ``component_dist`` over
  [`ComponentType`][colorsense.ComponentType]. The distribution is split per color
  channel and attributed to the nearest screenshot-bin color of the *matching*
  measured color: ``*_text`` components and ``link`` route to ``element.text``
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

Perceptual distance is measured exclusively with
`colorsense.color.primitives.delta_e` (OKLab ``deltaEOK``), whose units are small;
the thresholds below are tuned for that scale.

Determinism
-----------
There is no randomness. Wherever iteration order could affect the result we sort by a
stable key (color ``hex``). The same input always yields identical output.
"""

from __future__ import annotations

from collections import defaultdict

from colorsense.color.primitives import delta_e
from colorsense.models import (
    ClassifiedElement,
    Color,
    ColorCluster,
    ComponentType,
    Harvest,
    HarvestedElement,
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
    if element.bg is not None and element.bg.alpha > 0.0:
        return [element.bg]
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


def build_inventory(harvest: Harvest, classified: list[ClassifiedElement]) -> list[ColorCluster]:
    """Join area-truth with element semantics and cluster perceptually-near colors.

    See the module docstring for the data sources. The algorithm is:

    1. Seed one entry per `ScreenshotBin` (authoritative
       area weight, empty mix).
    2. For each classified element, split its ``component_dist`` into per-channel
       sub-distributions via `_channel_for` and process the channels in a
       fixed (bg, text, border) order. For each channel whose measured color is
       non-``None`` and whose sub-distribution is non-empty, find the nearest
       entry by `delta_e`. If that nearest
       entry is within the channel's join radius (`DELTA_E_MATCH_BG` for
       bg, the tighter `DELTA_E_MATCH_TEXT_BORDER` for text/border), add
       the channel's mass (each element weighted equally, raw) into the entry's
       mix. Otherwise create a
       new entry from the channel's color with ``area_weight = 0.0`` so its
       semantics are not lost. New entries are appended in element order then
       channel order, which is deterministic.
    3. Cluster entries via union-find under `DELTA_E_CLUSTER`. For each group
       emit one `ColorCluster`: representative color =
       member with the largest area weight (ties / all-zero broken by ``hex``);
       ``area_weight`` = sum of member weights; ``member_count`` = group size;
       ``component_mass`` = the raw summed member mixes (un-normalized vote mass —
       the usage view needs cross-cluster magnitude); ``component_mix`` = the same
       sums normalized to ~1.0 (empty stays empty).

    Returns clusters sorted by ``area_weight`` descending, ties broken by ``hex``.
    """
    entries: list[_Entry] = [
        _Entry(bin_.color, bin_.area_fraction) for bin_ in harvest.screenshot_bins
    ]

    # STEP 1b: attribute element semantics to the nearest entry (or a new one),
    # routing each component's mass to the channel that actually paints it.
    for ce in classified:
        if not ce.component_dist:
            continue

        # Split the distribution into per-channel sub-distributions.
        sub_dists: dict[str, dict[ComponentType, float]] = {"bg": {}, "text": {}, "border": {}}
        for comp, val in ce.component_dist.items():
            sub_dists[_channel_for(comp)][comp] = val

        # Fixed channel order for determinism. The bg channel can carry more than one
        # fill color — a gradient CTA paints every opaque stop (see `_bg_fill_colors`) —
        # so each channel resolves to a list; text/border are always single.
        for channel, colors in (
            ("bg", _bg_fill_colors(ce.element)),
            ("text", [ce.element.text]),
            ("border", [ce.element.border]),
        ):
            sub_dist = sub_dists[channel]
            if not sub_dist:
                continue
            # A fully-transparent fill color (alpha == 0, e.g. the default
            # ``background-color: transparent``) paints nothing: attributing its
            # mass would invent a phantom #000000 cluster.
            fills = [c for c in colors if c is not None and c.alpha > 0.0]
            if not fills:
                continue

            # Split the element's channel mass evenly across its fill colors so a
            # multi-stop gradient is not double-counted (a purple->blue button donates
            # the same total cta_bg mass as a solid one, half to each stop). On the bg
            # channel the per-fill share is additionally scaled by the fill's alpha, so a
            # faint ``bg-primary/10`` tint votes its intended (saturated) hex in
            # proportion to how much it actually paints; text/border are not alpha-scaled.
            n = len(fills)
            for color in fills:
                weight = (color.alpha if channel == "bg" else 1.0) / n

                best_idx: int | None = None
                best_dist = _MATCH_BY_CHANNEL[channel]
                for idx, entry in enumerate(entries):
                    d = delta_e(color, entry.color)
                    if d <= best_dist:
                        best_dist = d
                        best_idx = idx

                if best_idx is None:
                    new_entry = _Entry(color, 0.0)
                    for comp, val in sub_dist.items():
                        new_entry.component_mix[comp] += val * weight
                    entries.append(new_entry)
                else:
                    target = entries[best_idx]
                    for comp, val in sub_dist.items():
                        target.component_mix[comp] += val * weight

    if not entries:
        return []

    # STEP 2: union-find clustering under DELTA_E_CLUSTER.
    n = len(entries)
    parent = list(range(n))
    for i in range(n):
        for j in range(i + 1, n):
            if delta_e(entries[i].color, entries[j].color) <= DELTA_E_CLUSTER:
                _union(parent, i, j)

    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        groups[_find(parent, i)].append(i)

    clusters: list[ColorCluster] = []
    for members in groups.values():
        group = [entries[i] for i in members]

        # Representative: largest area weight, ties (and all-zero) broken by smallest hex.
        rep = min(group, key=lambda e: (-e.area_weight, e.color.hex))
        total_area = sum(e.area_weight for e in group)

        summed: dict[ComponentType, float] = defaultdict(float)
        for e in group:
            for comp, val in e.component_mix.items():
                summed[comp] += val

        mix: dict[ComponentType, float] = {}
        total = sum(summed.values())
        if total > 0.0:
            mix = {comp: val / total for comp, val in summed.items()}

        clusters.append(
            ColorCluster(
                color=rep.color,
                area_weight=total_area,
                member_count=len(group),
                component_mix=mix,
                component_mass=dict(summed),
            )
        )

    clusters.sort(key=lambda c: (-c.area_weight, c.color.hex))
    return clusters
