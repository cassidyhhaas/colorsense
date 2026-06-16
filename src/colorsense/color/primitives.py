"""Pure color-math primitives wrapping `coloraide`.

All functions here are side-effect free (no I/O). They consume and produce the frozen
[`colorsense.models.Color`][colorsense.Color] contract. The OKLCH coordinates stored on ``Color``
are named ``lightness`` / ``chroma`` / ``hue`` and are always computed by converting the sRGB color
to the ``oklch`` space via `coloraide`.

Conventions
-----------
* ``hex`` on a returned [`Color`][colorsense.Color] is the *opaque* normalized lowercase sRGB hex
  string (``#rrggbb``); alpha is carried separately in ``Color.alpha`` and is **not**
  encoded into ``hex``. This keeps ``hex`` stable for clustering/equality while alpha
  remains available for compositing.
* Achromatic colors yield ``hue = nan`` from coloraide; we normalize that to ``0.0`` so
  the contract never carries NaN.
"""

from __future__ import annotations

import math

from coloraide import Color as CAColor

from colorsense.models import Color


def _normalize_hue(hue: float) -> float:
    """Map a possibly-NaN coloraide hue to a finite degrees value in ``[0, 360)``."""
    if math.isnan(hue):
        return 0.0
    return float(hue % 360.0)


def _from_coloraide(ca: CAColor, *, clamp_srgb: bool = False) -> Color:
    """Build the frozen [`Color`][colorsense.Color] contract from a coloraide color.

    The input is brought into sRGB before extracting hex, and OKLCH coordinates are read
    from the ``oklch`` conversion of the (alpha-stripped) sRGB color.

    ``clamp_srgb`` selects how out-of-gamut sRGB coordinates are reduced into ``[0, 1]``:

    * ``False`` (default): perceptual gamut-mapping via `coloraide.Color.fit`, which
      preserves hue/lightness while pulling chroma in — appropriate for colors that arrive
      from a wide-gamut/OKLCH computation (e.g. compositing, lightness nudges).
    * ``True``: a plain per-channel clamp of each sRGB coordinate to ``[0, 1]``, matching how
      browsers treat out-of-range ``rgb()``/hex inputs (``rgb(300,0,0)`` -> ``#ff0000``).

    Either way, in-gamut colors are unchanged (clamping a value already in ``[0, 1]`` is a
    no-op).
    """
    srgb = ca.convert("srgb")
    if clamp_srgb:
        # Per-channel clamp of the color coordinates (not alpha) — CSS/browser behavior.
        for channel in ("red", "green", "blue"):
            srgb[channel] = min(1.0, max(0.0, float(srgb[channel])))
    else:
        srgb = srgb.fit()
    alpha_raw = srgb[-1]
    alpha = 1.0 if math.isnan(alpha_raw) else float(alpha_raw)

    # Opaque hex: do not encode alpha into the hex string.
    hex_str = srgb.to_string(hex=True, alpha=False).lower()

    oklch = srgb.convert("oklch")
    lightness = float(oklch["lightness"])
    chroma = float(oklch["chroma"])
    hue = _normalize_hue(float(oklch["hue"]))

    return Color(
        hex=hex_str,
        lightness=lightness,
        chroma=chroma,
        hue=hue,
        alpha=alpha,
    )


def _to_coloraide(c: Color) -> CAColor:
    """Reconstruct a coloraide sRGB color (with alpha) from the frozen contract."""
    return CAColor(c.hex).set("alpha", c.alpha)


def parse_css_color(value: str) -> Color | None:
    """Parse a CSS color string into a [`Color`][colorsense.Color], or ``None`` if unparseable.

    Accepts ``rgb()``/``rgba()``, hex (``#rgb``/``#rrggbb``/``#rrggbbaa``),
    ``hsl()``/``hsla()`` and named CSS colors. The keyword ``transparent`` parses to a
    color with ``alpha = 0.0``. Non-color input (e.g. ``"banana"``, ``"none"``, ``""``)
    returns ``None``. The returned ``hex`` is the normalized lowercase opaque sRGB hex
    string; alpha (if any) is carried in ``Color.alpha`` only.
    """
    match = CAColor.match(value.strip())
    if match is None:
        return None
    # Reject trailing garbage (match may parse a valid prefix of a longer string).
    if match.end != len(value.strip()):
        return None
    # sRGB-defined inputs (rgb()/rgba()/hex/named) clamp per-channel like a browser; other
    # spaces (hsl()) keep perceptual fit. hsl() values are in-gamut by construction, so the
    # distinction only matters for out-of-range rgb()/hex.
    clamp_srgb = match.color.space() == "srgb"
    try:
        return _from_coloraide(match.color, clamp_srgb=clamp_srgb)
    except (ValueError, KeyError):  # pragma: no cover - defensive
        return None


def is_painting(color: Color | None) -> bool:
    """Whether ``color`` paints something: present and not fully transparent (alpha > 0.0).

    The "paints anything" predicate — a color with **any** opacity contributes a visible
    fill, so the test is ``alpha > 0.0``. Deliberately distinct from `is_opaque`, which
    asks the stricter ``alpha >= 1.0`` question; the two are easy to confuse, which is why
    both live here as named predicates rather than inline ``alpha`` comparisons. A
    ``None`` color (no color at all) never paints.
    """
    return color is not None and color.alpha > 0.0


def is_opaque(color: Color | None) -> bool:
    """Whether ``color`` is present and fully opaque (alpha >= 1.0).

    The strict counterpart to `is_painting`: a partly-transparent color *paints* but is not
    *opaque*. Use this when only a solid surface qualifies (e.g. deriving the page canvas a
    link reads against). A ``None`` color is never opaque.
    """
    return color is not None and color.alpha >= 1.0


def delta_e(a: Color, b: Color) -> float:
    """Perceptual distance between two colors via OKLab ``deltaEOK``.

    Identical colors return ``~0.0``; perceptually different colors return ``> 0``.

    Computed directly from the OKLCH coordinates cached on [`Color`][colorsense.Color] (no coloraide
    object construction): ``deltaEOK`` is Euclidean distance in OKLab, and OKLab is
    recovered from OKLCH as ``a = C*cos(h)``, ``b = C*sin(h)``. Achromatic colors store
    ``hue = 0.0`` with ``chroma ~ 0``, which is consistent under this formula. Alpha is
    ignored, matching coloraide's ``delta_e(..., method="ok")``.
    """
    a_hue = math.radians(a.hue)
    b_hue = math.radians(b.hue)
    da = a.chroma * math.cos(a_hue) - b.chroma * math.cos(b_hue)
    db = a.chroma * math.sin(a_hue) - b.chroma * math.sin(b_hue)
    dl = a.lightness - b.lightness
    return math.sqrt(dl * dl + da * da + db * db)


def ciede2000(a: Color, b: Color) -> float:
    """Perceptual distance between two colors via **CIEDE2000** (alpha ignored).

    The CIE standard perceptual color-difference metric. Use this — not OKLab
    `delta_e` — for *identity* questions ("are these two colors the same color?"), because
    OKLab ``deltaEOK`` is materially less accurate near the lightness extremes (near-white /
    near-black), which is exactly where page backgrounds and neutral palettes live. ΔE2000
    units: ``~1`` is a just-noticeable difference, ``~2-3`` noticeable. This matches the
    eval's identity metric (``eval/colormetric.py``'s ``pdelta`` + ``IDENTITY_TOLERANCE``),
    so the library and the offline quality eval define color identity the same way.
    """
    return float(_to_coloraide(a).delta_e(_to_coloraide(b), method="2000"))


def relative_luminance(c: Color) -> float:
    """WCAG 2.1 relative luminance of ``c`` (linearized sRGB, Rec. 709 weights).

    White returns ``~1.0`` and black returns ``~0.0``.
    """
    return float(_to_coloraide(c).convert("srgb").luminance())


def contrast_ratio(fg: Color, bg: Color) -> float:
    """WCAG 2.1 relative-luminance contrast ratio ``(L1 + 0.05) / (L2 + 0.05)``.

    White-on-black equals ``21.0`` (within floating-point tolerance).
    """
    l1 = relative_luminance(fg)
    l2 = relative_luminance(bg)
    lighter, darker = (l1, l2) if l1 >= l2 else (l2, l1)
    return (lighter + 0.05) / (darker + 0.05)
