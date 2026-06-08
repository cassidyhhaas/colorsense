"""Full-page screenshot quantization and logo-color sampling.

Takes a full-page Playwright screenshot, masks out consent/overlay regions (so a cookie
banner cannot pollute the palette), downscales, and quantizes pixels into
:class:`~colorsense.models.ScreenshotBin` objects each carrying an ``area_fraction`` (the
fraction of *sampled* pixels). Also samples dominant colors from a discoverable logo /
favicon into ``logo_colors``.

Quantization is deterministic given a fixed fixture: Pillow's adaptive palette with a
fixed max-color count, computed over the masked, downscaled image.
"""

from __future__ import annotations

import io
from typing import TypedDict, cast

import numpy as np
from PIL import Image
from playwright.sync_api import Page

from colorsense.color.primitives import parse_css_color
from colorsense.models import Color, Rect, ScreenshotBin

# Longest-edge target for the downscaled image used for quantization.
_DOWNSCALE_MAX_EDGE: int = 256

# Number of palette buckets requested from the adaptive quantizer.
_PALETTE_COLORS: int = 16

# Drop bins covering less than this fraction of sampled pixels (noise floor).
_MIN_BIN_FRACTION: float = 0.005

# Max distinct logo colors to keep.
_MAX_LOGO_COLORS: int = 5


class _LogoSource(TypedDict):
    """A candidate logo image: its resolved URL."""

    url: str


def _rgb_to_color(r: int, g: int, b: int) -> Color | None:
    """Convert an 8-bit RGB triple to a :class:`Color`."""
    return parse_css_color(f"rgb({r}, {g}, {b})")


def harvest_screenshot(
    page: Page,
    consent_rects: list[Rect],
    device_scale_factor: float,
) -> list[ScreenshotBin]:
    """Capture, mask, and quantize a full-page screenshot into color bins.

    ``consent_rects`` are CSS-pixel rects (from :class:`RenderSession`); they are scaled by
    ``device_scale_factor`` and zeroed out of the raw screenshot before quantizing.
    """
    png_bytes = page.screenshot(full_page=True)
    image = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    array = np.asarray(image, dtype=np.uint8).copy()

    height, width = array.shape[0], array.shape[1]

    # Build a boolean keep-mask; consent regions are excluded from sampling entirely.
    keep = np.ones((height, width), dtype=bool)
    for rect in consent_rects:
        x0 = max(0, int(rect.x * device_scale_factor))
        y0 = max(0, int(rect.y * device_scale_factor))
        x1 = min(width, int((rect.x + rect.w) * device_scale_factor))
        y1 = min(height, int((rect.y + rect.h) * device_scale_factor))
        if x1 > x0 and y1 > y0:
            keep[y0:y1, x0:x1] = False

    return _quantize(array, keep)


def _quantize(array: np.ndarray, keep: np.ndarray) -> list[ScreenshotBin]:
    """Quantize the kept pixels of ``array`` into area-weighted color bins."""
    height, width = array.shape[0], array.shape[1]

    # Downscale for stable, fast quantization. Nearest keeps colors crisp/deterministic.
    scale = max(height, width) / _DOWNSCALE_MAX_EDGE
    if scale > 1.0:
        new_w = max(1, int(width / scale))
        new_h = max(1, int(height / scale))
        img = Image.fromarray(array).resize((new_w, new_h), Image.Resampling.NEAREST)
        mask_img = Image.fromarray(keep.astype(np.uint8) * 255).resize(
            (new_w, new_h), Image.Resampling.NEAREST
        )
        small = np.asarray(img, dtype=np.uint8)
        small_keep = np.asarray(mask_img, dtype=np.uint8) > 127
    else:
        small = array
        small_keep = keep

    quant = Image.fromarray(small).quantize(colors=_PALETTE_COLORS, method=Image.Quantize.MEDIANCUT)
    palette_indices = np.asarray(quant, dtype=np.int64)
    palette = quant.getpalette()
    if palette is None:
        return []

    flat_idx = palette_indices.reshape(-1)
    flat_keep = small_keep.reshape(-1)
    kept_idx = flat_idx[flat_keep]
    total = int(kept_idx.size)
    if total == 0:
        return []

    counts = np.bincount(kept_idx, minlength=_PALETTE_COLORS)

    bins: list[ScreenshotBin] = []
    for index in range(len(counts)):
        count = int(counts[index])
        if count == 0:
            continue
        fraction = count / total
        if fraction < _MIN_BIN_FRACTION:
            continue
        base = index * 3
        r, g, b = palette[base], palette[base + 1], palette[base + 2]
        color = _rgb_to_color(r, g, b)
        if color is None:
            continue
        bins.append(ScreenshotBin(color=color, area_fraction=fraction))

    bins.sort(key=lambda item: item.area_fraction, reverse=True)
    return bins


# JS discovering a logo/favicon image URL.
_LOGO_SOURCES_JS: str = r"""
() => {
    const out = [];
    const add = (href) => {
        if (!href) return;
        try {
            out.push({url: new URL(href, window.location.href).href});
        } catch (e) {}
    };
    // Favicons / declared icons.
    for (const link of document.querySelectorAll('link[rel~="icon"], link[rel="shortcut icon"]')) {
        add(link.getAttribute('href'));
    }
    // Header logo images.
    const header = document.querySelector('header') || document.body;
    if (header) {
        const img = header.querySelector('img');
        if (img) add(img.getAttribute('src'));
    }
    // Any element whose id/class hints "logo".
    const logoish = document.querySelector('[class*="logo" i], [id*="logo" i]');
    if (logoish) {
        const tag = logoish.tagName.toLowerCase();
        if (tag === 'img') add(logoish.getAttribute('src'));
    }
    return out;
}
"""


def harvest_logo_colors(page: Page) -> list[Color]:
    """Sample dominant colors from a discoverable logo/favicon image.

    Best-effort: returns an empty list if no logo is discoverable or fetching/decoding
    fails. Colors are ordered by coverage (most dominant first), opaque pixels only.
    """
    try:
        sources = cast(list[_LogoSource], page.evaluate(_LOGO_SOURCES_JS))
    except Exception:  # discovery is best-effort
        return []

    for source in sources:
        colors = _sample_logo(page, source["url"])
        if colors:
            return colors
    return []


def _sample_logo(page: Page, url: str) -> list[Color]:
    """Fetch and quantize a single logo image URL into dominant colors."""
    try:
        response = page.request.get(url)
        if not response.ok:
            return []
        data = response.body()
        image = Image.open(io.BytesIO(data)).convert("RGBA")
    except Exception:  # fetch/decode best-effort
        return []

    image.thumbnail((64, 64), Image.Resampling.NEAREST)
    array = np.asarray(image, dtype=np.uint8)
    if array.ndim != 3 or array.shape[2] != 4:
        return []

    rgb = array[:, :, :3].reshape(-1, 3)
    alpha = array[:, :, 3].reshape(-1)
    opaque = rgb[alpha > 128]
    if opaque.shape[0] == 0:
        return []

    quant = Image.fromarray(opaque.reshape(1, -1, 3)).quantize(
        colors=_MAX_LOGO_COLORS, method=Image.Quantize.MEDIANCUT
    )
    indices = np.asarray(quant, dtype=np.int64).reshape(-1)
    palette = quant.getpalette()
    if palette is None:
        return []
    counts = np.bincount(indices, minlength=_MAX_LOGO_COLORS)

    order = np.argsort(counts)[::-1]
    colors: list[Color] = []
    for index in order.tolist():
        if int(counts[index]) == 0:
            continue
        base = index * 3
        color = _rgb_to_color(palette[base], palette[base + 1], palette[base + 2])
        if color is not None:
            colors.append(color)
        if len(colors) >= _MAX_LOGO_COLORS:
            break
    return colors
