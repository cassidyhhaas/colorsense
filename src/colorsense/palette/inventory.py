"""Inventory & clustering: join area-truth with semantics, then cluster.

This module fuses two sources of truth from a :class:`~colorsense.models.Harvest`
and its classified elements into area-weighted :class:`~colorsense.models.ColorCluster`
objects:

* **Area truth** — :attr:`Harvest.screenshot_bins`. Each
  :class:`~colorsense.models.ScreenshotBin` reports a rendered color and the fraction
  of page area it covers. This is the authoritative area weight.
* **Semantic truth** — the classified elements. Each
  :class:`~colorsense.models.ClassifiedElement` carries a ``component_dist`` over
  :class:`~colorsense.models.ComponentType`. The distribution is split per color
  channel and attributed to the nearest screenshot-bin color of the *matching*
  measured color: ``*_text`` components and ``link`` route to ``element.text``
  (a link paints its typography), :attr:`~colorsense.models.ComponentType.border`
  to ``element.border``, and everything else to ``element.bg``. This channel
  routing is a fixed code-level convention (see :func:`_channel_for`). A channel
  whose measured color is **fully transparent** (``alpha == 0``) paints nothing
  and donates no mass — without this gate, every transparent-background element
  (links, paragraphs, wrappers) piles its votes onto a phantom ``#000000``
  zero-area cluster.

Perceptual distance is measured exclusively with
:func:`colorsense.color.primitives.delta_e` (OKLab ``deltaEOK``), whose units are small;
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
)

__all__ = ["build_inventory"]

# Maximum OKLab deltaEOK distance at which a classified element's channel color
# (bg / text / border) is considered "the same painted surface" as a screenshot
# bin and so donates that channel's component mass to the bin's entry. deltaEOK
# units are small.
DELTA_E_MATCH: float = 0.10

# Maximum OKLab deltaEOK distance at which two entries are merged into a single
# perceptual cluster. Kept <= DELTA_E_MATCH so that only truly near-identical
# colors collapse together.
DELTA_E_CLUSTER: float = 0.05


def _channel_for(component: ComponentType) -> str:
    """Return the color channel a component's vote mass routes to.

    The routing convention is fixed in code: components whose value ends with
    ``_text`` — plus ``link``, whose painted color is its typography color, not
    its (usually transparent) background — are painted by the element's
    ``color`` (its ``text`` channel), :attr:`~colorsense.models.ComponentType.border`
    by its ``border-color``, and everything else (including ``badge``,
    ``third_party`` and ``button_secondary``) by its ``background-color``.
    """
    if component.value.endswith("_text") or component is ComponentType.link:
        return "text"
    if component is ComponentType.border:
        return "border"
    return "bg"


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

    1. Seed one entry per :class:`~colorsense.models.ScreenshotBin` (authoritative
       area weight, empty mix).
    2. For each classified element, split its ``component_dist`` into per-channel
       sub-distributions via :func:`_channel_for` and process the channels in a
       fixed (bg, text, border) order. For each channel whose measured color is
       non-``None`` and whose sub-distribution is non-empty, find the nearest
       entry by :func:`~colorsense.color.primitives.delta_e`. If that nearest
       entry is within :data:`DELTA_E_MATCH`, add the channel's mass (each
       element weighted equally, raw) into the entry's mix. Otherwise create a
       new entry from the channel's color with ``area_weight = 0.0`` so its
       semantics are not lost. New entries are appended in element order then
       channel order, which is deterministic.
    3. Cluster entries via union-find under :data:`DELTA_E_CLUSTER`. For each group
       emit one :class:`~colorsense.models.ColorCluster`: representative color =
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

        # Fixed channel order for determinism.
        for channel, color in (
            ("bg", ce.element.bg),
            ("text", ce.element.text),
            ("border", ce.element.border),
        ):
            sub_dist = sub_dists[channel]
            # A fully-transparent channel color (alpha == 0, e.g. the default
            # ``background-color: transparent``) paints nothing: attributing its
            # mass would invent a phantom #000000 cluster.
            if color is None or color.alpha == 0.0 or not sub_dist:
                continue

            best_idx: int | None = None
            best_dist = DELTA_E_MATCH
            for idx, entry in enumerate(entries):
                d = delta_e(color, entry.color)
                if d <= best_dist:
                    best_dist = d
                    best_idx = idx

            if best_idx is None:
                new_entry = _Entry(color, 0.0)
                for comp, val in sub_dist.items():
                    new_entry.component_mix[comp] += val
                entries.append(new_entry)
            else:
                target = entries[best_idx]
                for comp, val in sub_dist.items():
                    target.component_mix[comp] += val

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

        # Representative: largest area weight, ties (and all-zero) broken by hex.
        rep = max(group, key=lambda e: (e.area_weight, _neg_hex_key(e.color.hex)))
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


def _neg_hex_key(hex_str: str) -> tuple[int, ...]:
    """Return a key so that ``max`` on it selects the *smallest* hex string.

    Used as a tie-breaker inside ``max``: negating each code point makes the
    lexicographically smallest hex compare greatest.
    """
    return tuple(-ord(ch) for ch in hex_str)
