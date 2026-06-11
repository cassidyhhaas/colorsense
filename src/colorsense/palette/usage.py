"""Usage-keyed palette assembly: what colors paint each usage category.

Builds the **measured** :class:`~colorsense.models.UsagePalette` from the color
inventory: per :class:`~colorsense.models.UsageCategory` (surface / text / interactive /
border), a probability-ranked list of :class:`~colorsense.models.UsageEntry` colors.
This is the primary palette view — unlike the 60/30/10 roles view it preserves the
design's actual structure (e.g. a neutral-layered design's gray text/border hierarchy
appears here directly).

Design notes
------------
* The public entry point is :func:`build_usage`. It takes *only* the cluster list (no
  :class:`Config`); every threshold is a documented, module-level **tunable** constant.
* :data:`COMPONENT_USAGE` — the component-type → usage-category routing — is a fixed
  code-level convention, exactly like the inventory's component → color-channel routing
  (``palette/inventory.py``'s ``_channel_for``): it describes what the taxonomy *means*,
  not a tunable weight, so it lives in code rather than the YAML config.
* Prominence is scored differently per category, deliberately (worked examples in
  docs/how-it-works.md):
    - **surface**: prominence ∝ the cluster's screenshot ``area_weight``. Area is the
      authoritative signal for surfaces — vote counts would let repeated small elements
      outrank the page background. Only clusters with nonzero surface vote mass
      participate (area alone does not prove a color is a surface); a zero-area surface
      cluster (element-only, no screenshot-bin match) scores 0 and prunes naturally
      unless it ends up the argmax fallback.
    - **text / interactive / border**: prominence ∝ ``log1p`` of the cluster's raw vote
      mass in that category. These paint negligible screenshot area; vote mass ranks
      them, but only **sub-linearly** — raw (linear) mass let element *count* drown
      high-confidence single-element evidence. ``log1p`` is monotonic, so within-category
      *ordering* is unchanged; only the shares compress, while genuinely tiny masses
      still prune.
* Everything is deterministic: iteration is over stable sort orders, ties are broken by
  color ``hex``, and there is no randomness.
"""

from __future__ import annotations

import math

from colorsense.models import (
    ColorCluster,
    ComponentType,
    UsageCategory,
    UsageEntry,
    UsagePalette,
)

__all__ = ["build_usage"]

# ---------------------------------------------------------------------------
# Tunable constants (all module-level and documented).
# ---------------------------------------------------------------------------

# Entries below this within-category probability share are pruned (then survivors are
# renormalized). If pruning would empty a non-empty category, the argmax entry is kept
# at probability 1.0 instead (the RoleResults / palette.roles pattern).
MIN_SHARE: float = 0.02

# Component-type -> usage-category routing. A fixed code-level convention (see the
# module docstring); ``third_party`` maps to NO category — third-party widget colors are
# excluded from the usage view and surface via ``AnalysisResult.third_party_colors``.
COMPONENT_USAGE: dict[ComponentType, UsageCategory] = {
    ComponentType.page_bg: UsageCategory.surface,
    ComponentType.header_bg: UsageCategory.surface,
    ComponentType.nav_bg: UsageCategory.surface,
    ComponentType.footer_bg: UsageCategory.surface,
    ComponentType.hero_bg: UsageCategory.surface,
    ComponentType.card_bg: UsageCategory.surface,
    ComponentType.modal_bg: UsageCategory.surface,
    ComponentType.input_bg: UsageCategory.surface,
    ComponentType.page_text: UsageCategory.text,
    ComponentType.header_text: UsageCategory.text,
    ComponentType.nav_text: UsageCategory.text,
    ComponentType.footer_text: UsageCategory.text,
    ComponentType.hero_text: UsageCategory.text,
    ComponentType.card_text: UsageCategory.text,
    ComponentType.link: UsageCategory.interactive,
    ComponentType.cta_bg: UsageCategory.interactive,
    ComponentType.cta_text: UsageCategory.interactive,
    ComponentType.button_secondary: UsageCategory.interactive,
    ComponentType.badge: UsageCategory.interactive,
    ComponentType.border: UsageCategory.border,
    # ComponentType.third_party: deliberately absent.
}


def _category_masses(cluster: ColorCluster) -> dict[UsageCategory, dict[ComponentType, float]]:
    """Split a cluster's raw ``component_mass`` by usage category.

    A color used in multiple ways (e.g. the same gray as text *and* border) correctly
    lands in multiple categories, each with its respective component masses.
    Components with no category (``third_party``) are dropped.
    """
    split: dict[UsageCategory, dict[ComponentType, float]] = {}
    for comp, mass in cluster.component_mass.items():
        if mass <= 0.0:
            continue
        category = COMPONENT_USAGE.get(comp)
        if category is None:
            continue
        split.setdefault(category, {})[comp] = mass
    return split


def _build_entries(
    scored: list[tuple[ColorCluster, float, dict[ComponentType, float]]],
) -> tuple[UsageEntry, ...]:
    """Normalize prominence scores into probabilities, prune, renormalize, and rank.

    ``scored`` is ``(cluster, prominence, per-component masses)`` per participating
    cluster. Entries below :data:`MIN_SHARE` are pruned with the keep-argmax fallback;
    output is sorted by ``(-probability, hex)``.
    """
    if not scored:
        return ()

    total = sum(score for _, score, _ in scored)
    if total <= 0.0:
        # All-zero prominence (e.g. a surface-only cluster set with no screenshot area):
        # fall back to the deterministic argmax by (score, hex) — i.e. smallest hex.
        best = min(scored, key=lambda item: item[0].color.hex)
        kept = [(best[0], 1.0, best[2])]
    else:
        probs = [(cluster, score / total, masses) for cluster, score, masses in scored]
        kept = [(c, p, m) for c, p, m in probs if p >= MIN_SHARE]
        if not kept:
            # Pruning emptied the category: keep the single argmax at probability 1.0.
            # Largest probability wins; ties broken by smallest hex for determinism.
            best = min(probs, key=lambda item: (-item[1], item[0].color.hex))
            kept = [(best[0], 1.0, best[2])]
        else:
            kept_total = sum(p for _, p, _ in kept)
            kept = [(c, p / kept_total, m) for c, p, m in kept]

    entries: list[UsageEntry] = []
    for cluster, prob, masses in kept:
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
    """Build the **measured** usage palette from the color inventory.

    For each usage category, the participating clusters (those with nonzero raw vote
    mass routed to the category via :data:`COMPONENT_USAGE`) are scored by prominence —
    screenshot area for ``surface``, ``log1p`` of in-category vote mass for the others
    (see the module docstring for the rationale) — normalized to probabilities, pruned below
    :data:`MIN_SHARE` (argmax kept if pruning empties the category), and ranked by
    ``(-probability, hex)``. A category with no mass anywhere maps to ``()`` (the
    :class:`UsagePalette` validator backfills it). An empty cluster list yields an
    empty (all-``()``) palette.
    """
    per_category: dict[UsageCategory, list[tuple[ColorCluster, float, dict[ComponentType, float]]]]
    per_category = {category: [] for category in UsageCategory}

    # Stable iteration: clusters sorted by (-area_weight, hex), matching inventory order.
    for cluster in sorted(clusters, key=lambda c: (-c.area_weight, c.color.hex)):
        for category, masses in _category_masses(cluster).items():
            if category is UsageCategory.surface:
                # Area-proportional (see the module docstring's Design notes).
                prominence = cluster.area_weight
            else:
                # Sub-linear in vote mass (see the module docstring's Design notes).
                prominence = math.log1p(sum(masses.values()))
            per_category[category].append((cluster, prominence, masses))

    mapping = {
        category: _build_entries(scored) for category, scored in per_category.items() if scored
    }
    return UsagePalette(mapping=mapping)
