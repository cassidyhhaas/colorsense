"""Inventory & clustering: join area-truth with semantics, then cluster.

This module fuses two sources of truth from a :class:`~colorsense.models.Harvest`
and its classified elements into area-weighted :class:`~colorsense.models.ColorCluster`
objects:

* **Area truth** — :attr:`Harvest.screenshot_bins`. Each
  :class:`~colorsense.models.ScreenshotBin` reports a rendered color and the fraction
  of page area it covers. This is the authoritative area weight.
* **Semantic truth** — the classified elements. Each
  :class:`~colorsense.models.ClassifiedElement` carries a ``component_dist`` over
  :class:`~colorsense.models.ComponentType`. We attribute that distribution to the
  nearest screenshot-bin color (its main painted surface, ``element.bg``).

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

# Maximum OKLab deltaEOK distance at which a classified element's bg color is
# considered "the same painted surface" as a screenshot bin and so donates its
# component distribution to that bin's entry. deltaEOK units are small.
DELTA_E_MATCH: float = 0.10

# Maximum OKLab deltaEOK distance at which two entries are merged into a single
# perceptual cluster. Kept <= DELTA_E_MATCH so that only truly near-identical
# colors collapse together.
DELTA_E_CLUSTER: float = 0.05


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
    2. For each classified element with a non-``None`` ``bg`` and a non-empty
       ``component_dist``, find the nearest entry by
       :func:`~colorsense.color.primitives.delta_e`. If that nearest entry is within
       :data:`DELTA_E_MATCH`, add the element's distribution (each element weighted
       equally, raw) into the entry's mix. Otherwise create a new entry from the
       element's ``bg`` with ``area_weight = 0.0`` so its semantics are not lost.
    3. Cluster entries via union-find under :data:`DELTA_E_CLUSTER`. For each group
       emit one :class:`~colorsense.models.ColorCluster`: representative color =
       member with the largest area weight (ties / all-zero broken by ``hex``);
       ``area_weight`` = sum of member weights; ``member_count`` = group size;
       ``component_mix`` = summed member mixes normalized to sum ~1.0 (empty stays
       empty).

    Returns clusters sorted by ``area_weight`` descending, ties broken by ``hex``.
    """
    entries: list[_Entry] = [
        _Entry(bin_.color, bin_.area_fraction) for bin_ in harvest.screenshot_bins
    ]

    # STEP 1b: attribute element semantics to the nearest entry (or a new one).
    for ce in classified:
        bg = ce.element.bg
        if bg is None or not ce.component_dist:
            continue

        best_idx: int | None = None
        best_dist = DELTA_E_MATCH
        for idx, entry in enumerate(entries):
            d = delta_e(bg, entry.color)
            if d <= best_dist:
                best_dist = d
                best_idx = idx

        if best_idx is None:
            new_entry = _Entry(bg, 0.0)
            for comp, val in ce.component_dist.items():
                new_entry.component_mix[comp] += val
            entries.append(new_entry)
        else:
            target = entries[best_idx]
            for comp, val in ce.component_dist.items():
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
