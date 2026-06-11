"""Palette role assignment (60/30/10 taxonomy) — a derived, measured-only view.

Assigns each :class:`~colorsense.models.ColorCluster` to the five palette roles
(:class:`~colorsense.models.PaletteRole`) with a per-role probability distribution, then
computes a ``fit_score`` measuring how well the measured palette matches the canonical
60/30/10 split. This view is a **derived 60/30/10 interpretation of measured usage**:
the primary palette view is the usage-keyed one (``palette/usage.py``), and the roles
view is **no longer reconciled against declared tokens** — it is reported exactly as
measured.

Design notes
------------
* The public entry point is :func:`assign_roles`. It takes *only* the cluster list (no
  :class:`Config`); every threshold/weight is a documented, module-level **tunable**
  constant defined below.
* Everything is deterministic: iteration over dicts is sorted, ties are broken by ``hex``,
  and there is no randomness.
* The 60/30/10 mental model:
    - **primary**   ~= the dominant neutral surface (~60%) — anchors contrast.
    - **secondary** ~= structural color (~30%) — cards/headers/nav surfaces.
    - **accent**    ~= the action/brand "pop" color (~10%) — CTAs/links/badges.
    - **neutral_light / neutral_dark** capture the light/dark neutrals.
"""

from __future__ import annotations

import math

from colorsense.color.primitives import contrast_ratio, is_neutral
from colorsense.models import (
    Color,
    ColorCluster,
    ComponentType,
    PaletteCandidate,
    PaletteRole,
    RoleResults,
)

__all__ = ["assign_roles"]

# ---------------------------------------------------------------------------
# Tunable constants (all module-level and documented).
# ---------------------------------------------------------------------------

# A color at/below this OKLCH chroma is treated as a hard "neutral" for is_neutral.
CHROMA_MAX: float = 0.04
# Scale for the *smooth* neutrality signal: neutrality = max(0, 1 - chroma/scale).
# Larger than CHROMA_MAX so slightly-tinted grays still read as fairly neutral.
CHROMA_NEUTRAL_SCALE: float = 0.10
# Reference chroma used to normalize chroma into ~[0, 1] when the cluster set itself has
# no strongly-chromatic member (guards a degenerate all-neutral max).
CHROMA_REF: float = 0.20

# --- Primary-ness scoring weights (step 2) ---
W_AREA: float = 1.0
W_NEUTRAL: float = 0.8
W_COMP_PRIMARY: float = 1.5

# --- Accent scoring weights (step 3): chroma + contrast + action-components win, even at
#     low area, so the area term is deliberately small. ---
W_CHROMA: float = 1.2
W_CONTRAST: float = 0.8
W_COMP_ACCENT: float = 1.5
W_AREA_ACCENT: float = 0.2

# --- Secondary scoring weights (step 3): high-area structural surfaces win (the "card
#     exception"); a non-neutral structural color is rewarded. ---
W_AREA_SEC: float = 1.0
W_COMP_SEC: float = 1.5
W_STRUCT: float = 0.6

# --- Neutral light/dark scoring (step 3): a small area floor so a pure-neutral color with
#     modest area still scores. ---
NEUT_AREA_W: float = 0.3

# --- Softmax / pruning (step 4) ---
# Softmax temperature: smaller => sharper distributions. Tuned so a clear winner dominates
# but plausible runners-up retain meaningful mass.
SOFTMAX_T: float = 0.25
# Candidates below this probability are pruned, then survivors are renormalized.
MIN_CANDIDATE_PROB: float = 0.02

# Target 60/30/10 split for primary/secondary/accent (used by fit_score).
TARGET_SPLIT: tuple[float, float, float] = (0.6, 0.3, 0.1)

# Component -> palette-role affinity. *_text, border, input_bg and third_party intentionally
# contribute nothing to palette-role assignment (they are not palette colors per se).
COMPONENT_AFFINITY: dict[ComponentType, PaletteRole] = {
    # Dominant background -> primary.
    ComponentType.page_bg: PaletteRole.primary,
    # Action / brand "pop" -> accent.
    ComponentType.cta_bg: PaletteRole.accent,
    ComponentType.cta_text: PaletteRole.accent,
    ComponentType.link: PaletteRole.accent,
    ComponentType.badge: PaletteRole.accent,
    # Structural surfaces -> secondary.
    ComponentType.card_bg: PaletteRole.secondary,
    ComponentType.header_bg: PaletteRole.secondary,
    ComponentType.nav_bg: PaletteRole.secondary,
    ComponentType.footer_bg: PaletteRole.secondary,
    ComponentType.hero_bg: PaletteRole.secondary,
    ComponentType.modal_bg: PaletteRole.secondary,
    ComponentType.button_secondary: PaletteRole.secondary,
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


class _Features:
    """Precomputed per-cluster features (computed once in :func:`assign_roles`)."""

    __slots__ = ("area", "chroma", "cluster", "comp_assoc", "lightness", "neutrality")

    def __init__(self, cluster: ColorCluster) -> None:
        color = cluster.color
        self.cluster = cluster
        self.area: float = cluster.area_weight
        self.chroma: float = color.chroma
        self.lightness: float = color.lightness
        # Smooth neutrality in [0, 1]; hard is_neutral consulted for the structural term.
        self.neutrality: float = max(0.0, 1.0 - color.chroma / CHROMA_NEUTRAL_SCALE)
        # Aggregate component_mix weight into role-affinity buckets.
        assoc: dict[PaletteRole, float] = {role: 0.0 for role in PaletteRole}
        for comp, weight in cluster.component_mix.items():
            role = COMPONENT_AFFINITY.get(comp)
            if role is not None:
                assoc[role] += weight
        self.comp_assoc: dict[PaletteRole, float] = assoc

    @property
    def color(self) -> Color:
        return self.cluster.color


def _softmax(scores: list[float], temperature: float) -> list[float]:
    """Numerically-stable softmax of ``scores`` at ``temperature``.

    An empty input yields an empty list; a single score yields ``[1.0]``.
    """
    if not scores:
        return []
    scaled = [s / temperature for s in scores]
    top = max(scaled)
    exps = [math.exp(s - top) for s in scaled]
    total = sum(exps)
    if total <= 0.0:  # pragma: no cover - defensive (exp is always > 0)
        n = len(scores)
        return [1.0 / n] * n
    return [e / total for e in exps]


def _build_candidates(
    feats: list[_Features],
    scores: list[float],
) -> list[PaletteCandidate]:
    """Softmax ``scores`` over clusters, prune, renormalize, and rank candidates.

    Returns a probability-descending (ties by hex) list of :class:`PaletteCandidate`.
    """
    probs = _softmax(scores, SOFTMAX_T)

    kept: list[tuple[_Features, float]] = [
        (f, p) for f, p in zip(feats, probs, strict=True) if p >= MIN_CANDIDATE_PROB
    ]
    if not kept:
        # Pruning emptied the role: keep the single argmax at probability 1.0. Tie-break
        # by larger probability, then by hex for determinism.
        best_idx = max(
            range(len(probs)),
            key=lambda i: (probs[i], feats[i].color.hex),
        )
        kept = [(feats[best_idx], 1.0)]
    else:
        total = sum(p for _, p in kept)
        kept = [(f, p / total) for f, p in kept]

    candidates = [
        PaletteCandidate(
            color=f.color,
            probability=p,
            area=f.cluster.area_weight,
        )
        for f, p in kept
    ]
    candidates.sort(key=lambda c: (-c.probability, c.color.hex))
    return candidates


def _fit_score(mapping: dict[PaletteRole, tuple[PaletteCandidate, ...]]) -> float:
    """Score how well the measured palette matches the 60/30/10 target.

    Reads the top (most-probable) candidate's ``area`` for primary/secondary/accent,
    normalizes the triple onto the probability simplex, and compares to ``TARGET_SPLIT``::

        fit = 1 - 0.5 * sum(|measured_i - target_i|)

    The ``0.5`` factor maps the L1 distance between two points on the simplex (max 2.0)
    onto ``[0, 1]``. An all-zero / missing triple yields ``0.0``. Result clamped to
    ``[0, 1]``.
    """
    areas: list[float] = []
    for role in (PaletteRole.primary, PaletteRole.secondary, PaletteRole.accent):
        cands = mapping.get(role, ())
        areas.append(cands[0].area if cands else 0.0)

    total = sum(areas)
    if total <= 0.0:
        return 0.0

    measured = [a / total for a in areas]
    l1 = sum(abs(m - t) for m, t in zip(measured, TARGET_SPLIT, strict=True))
    fit = 1.0 - 0.5 * l1
    return max(0.0, min(1.0, fit))


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------


def assign_roles(clusters: list[ColorCluster]) -> tuple[RoleResults, float]:
    """Assign clusters to palette roles and compute the 60/30/10 ``fit_score``.

    Returns ``(RoleResults, fit_score)``. ``RoleResults.mapping`` is populated for all five
    palette roles (each a probability-descending candidate list). The empty-cluster case
    returns ``(RoleResults(mapping={}), 0.0)``.
    """
    if not clusters:
        return RoleResults(mapping={}), 0.0

    feats = [_Features(c) for c in clusters]

    # Reference for chroma normalization: the most chromatic cluster, floored by CHROMA_REF
    # so an all-neutral set does not divide by ~0 and inflate weak chroma.
    max_chroma = max((f.chroma for f in feats), default=0.0)
    chroma_ref = max(max_chroma, CHROMA_REF)

    # --- Step 2: primary scoring + provisional primary anchor. ---
    primary_scores: list[float] = []
    for f in feats:
        comp_primary = f.comp_assoc[PaletteRole.primary]
        score = W_AREA * f.area + W_NEUTRAL * f.neutrality + W_COMP_PRIMARY * comp_primary
        primary_scores.append(score)

    # Provisional primary = argmax primary_score; tie-break by larger area, then hex.
    primary_idx = max(
        range(len(feats)),
        key=lambda i: (primary_scores[i], feats[i].area, feats[i].color.hex),
    )
    primary_color = feats[primary_idx].color

    # --- Step 3: score the remaining roles (primary anchor now known). ---
    accent_scores: list[float] = []
    secondary_scores: list[float] = []
    nlight_scores: list[float] = []
    ndark_scores: list[float] = []

    for f in feats:
        chroma_norm = f.chroma / chroma_ref
        contrast = contrast_ratio(f.color, primary_color)
        contrast_norm = (contrast - 1.0) / 20.0  # maps [1, 21] -> [0, 1]

        comp_accent = f.comp_assoc[PaletteRole.accent]
        comp_sec = f.comp_assoc[PaletteRole.secondary]
        # Non-neutral structural signal: structural component weight gated by chromaticity.
        non_neutral = 0.0 if is_neutral(f.color, CHROMA_MAX) else 1.0
        struct = comp_sec * non_neutral

        # Accent: chroma / contrast / action-components win even at low area.
        a_score = (
            W_CHROMA * chroma_norm
            + W_CONTRAST * contrast_norm
            + W_COMP_ACCENT * comp_accent
            + W_AREA_ACCENT * f.area
        )
        accent_scores.append(a_score)

        # Secondary: high-area structural surfaces (the "card exception").
        s_score = W_AREA_SEC * f.area + W_COMP_SEC * comp_sec + W_STRUCT * struct
        secondary_scores.append(s_score)

        # Neutral light / dark.
        nl_score = f.neutrality * f.lightness * (NEUT_AREA_W + f.area)
        nlight_scores.append(nl_score)

        nd_score = f.neutrality * (1.0 - f.lightness) * (NEUT_AREA_W + f.area)
        ndark_scores.append(nd_score)

    # --- Step 4: per-role softmax -> prune -> renormalize -> rank. ---
    mapping: dict[PaletteRole, tuple[PaletteCandidate, ...]] = {
        PaletteRole.primary: tuple(_build_candidates(feats, primary_scores)),
        PaletteRole.secondary: tuple(_build_candidates(feats, secondary_scores)),
        PaletteRole.accent: tuple(_build_candidates(feats, accent_scores)),
        PaletteRole.neutral_light: tuple(_build_candidates(feats, nlight_scores)),
        PaletteRole.neutral_dark: tuple(_build_candidates(feats, ndark_scores)),
    }

    # --- Step 5: fit_score. ---
    fit = _fit_score(mapping)

    return RoleResults(mapping=mapping), fit
