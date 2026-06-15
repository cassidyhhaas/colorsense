"""Perceptual color-identity metric for the eval (CIEDE2000).

The eval repeatedly asks "are these two hexes *the same color*?" — a perceptual-identity
question, and a deliberately different one from the pipeline's internal OKLab clustering.

OKLab ``deltaEOK`` is *not* perceptually uniform: it compresses the near-black and near-white
regions, so a single OKLab threshold means a different perceptual size for dark vs light colors.
Measured on the panel: shadcn's genuinely-distinct near-blacks ``#0a0a0a``/``#171717`` are
0.060 apart in OKLab while vercel's genuinely-distinct near-whites ``#fafafa``/``#ebebeb`` are
only 0.045 — so *any* fixed OKLab cut either splits one pair and merges the other, or sits on a
boundary a real pair straddles. There is no good fixed OKLab tolerance.

CIEDE2000 is the CIE standard perceptual color-difference metric, normalized by lightness,
chroma and hue precisely to remove that non-uniformity. Under it those two distinct pairs are
3.00 and 3.11 — nearly equal — so one threshold behaves the same across the gamut. The eval
uses it for every color-identity decision (matching, separation, recall, bleed).

Empirical basis for :data:`IDENTITY_TOLERANCE` (all 12 panel sites harvested on both macOS and
Linux Chromium and run through this repo's pipeline; the macOS frozen harvests vs fresh Linux
ones):

* **Floor ~0.** Every authored ground-truth color (shadcn + vercel) renders within **0.00
  ΔE2000** cross-OS, and panel-wide the *prominent, structural, page-truth* colors (page /
  surface / text / border / link / brand — what the GT is built from) are stable: jitter ~0 on
  almost every site, ≤ ~1.0 on the rest.
* **Ceiling 1.59.** The tightest *legitimately distinct* GT pair (shadcn ``#000000``/``#0a0a0a``)
  is **1.59 ΔE2000**.
* So ``IDENTITY_TOLERANCE = 1.0`` sits squarely in the (0.0, 1.59) gap.
* **The eval surfaces, not masks, instability.** Large cross-OS drift (5-14 ΔE2000) does occur,
  but it is confined to *zero-area syntax-highlight colors and small decorative/gradient tints*
  (e.g. tailwindcss's code-token colors, supabase ``#afcfc0``, shadcn's ``#5f5f5f`` → ``#737373``
  at 7.62) — exactly the NOISE the GT excludes. That wobble is a pipeline non-determinism the
  eval should report as noise, not paper over with a wide tolerance; no sane identity threshold
  covers 7-14, and widening T toward those values would instead start merging genuinely-distinct
  colors below the 1.59 ceiling.

Distances are ``ΔE2000`` units throughout: ~1 ≈ a just-noticeable difference, ~2-3 ≈ noticeable
at a glance.
"""

from __future__ import annotations

from functools import lru_cache

from coloraide import Color as _CAColor

from colorsense.models import Color

__all__ = ["IDENTITY_TOLERANCE", "pdelta"]

# ΔE2000 below which two colors are "the same color" for the eval. Sits in the measured
# (0.0 cross-OS-jitter, 1.59 tightest-distinct-GT-pair) gap; see module docstring.
IDENTITY_TOLERANCE: float = 1.0


@lru_cache(maxsize=8192)
def _ca(hex_str: str) -> _CAColor:
    return _CAColor(hex_str)


def pdelta(a: Color, b: Color) -> float:
    """Perceptual CIEDE2000 distance between two colors (alpha ignored, like the pipeline)."""
    return float(_ca(a.hex).delta_e(_ca(b.hex), method="2000"))
