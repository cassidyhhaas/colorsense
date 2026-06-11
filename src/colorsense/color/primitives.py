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
* Compositing (`composite_over`) is performed in **gamma** sRGB (the CSS
  "source-over" default), matching browser rendering.
"""

from __future__ import annotations

import math

from coloraide import Color as CAColor

from colorsense.models import Color

# OKLCH lightness is defined on the closed interval [0, 1].
_L_MIN: float = 0.0
_L_MAX: float = 1.0


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


def composite_over(fg: Color, bg: Color) -> Color:
    """Alpha-composite ``fg`` over ``bg`` and return an opaque (``alpha = 1.0``) color.

    Uses standard CSS source-over compositing performed in **gamma** sRGB. The result's
    OKLCH coordinates and hex are recomputed from the composited sRGB color.
    """
    fg_ca = _to_coloraide(fg)
    bg_ca = _to_coloraide(bg)
    composited = CAColor.layer([fg_ca, bg_ca], space="srgb").set("alpha", 1.0)
    return _from_coloraide(composited)


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


def is_neutral(c: Color, chroma_max: float) -> bool:
    """Return ``True`` when ``c``'s OKLCH chroma is ``<= chroma_max``."""
    return c.chroma <= chroma_max


def to_hex(c: Color) -> str:
    """Return the normalized lowercase opaque sRGB hex string for ``c``."""
    return _to_coloraide(c).convert("srgb").fit().to_string(hex=True, alpha=False).lower()


def nudge_lightness(c: Color, toward: str, amount: float) -> Color:
    """Return a new [`Color`][colorsense.Color] with OKLCH lightness shifted by ``amount``.

    ``toward`` is ``"light"`` (increase L) or ``"dark"`` (decrease L). The resulting
    lightness is clamped to the valid OKLCH range ``[0, 1]`` and hex / OKLCH are
    recomputed. ``alpha`` is preserved.
    """
    if toward not in ("light", "dark"):
        raise ValueError(f"toward must be 'light' or 'dark', got {toward!r}")

    delta = amount if toward == "light" else -amount
    new_l = min(_L_MAX, max(_L_MIN, c.lightness + delta))

    oklch = CAColor("oklch", [new_l, c.chroma, c.hue]).set("alpha", c.alpha)
    return _from_coloraide(oklch)
