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
from typing import Literal, TypedDict, cast

import numpy as np
from PIL import Image
from playwright.async_api import Page

from colorsense.color.primitives import parse_css_color
from colorsense.models import Color, Rect, ScreenshotBin

# Capture full-page screenshots as high-quality JPEG rather than PNG. The image is only
# ever downscaled to ``_DOWNSCALE_MAX_EDGE`` and quantized into ``_PALETTE_COLORS`` buckets,
# so PNG's lossless fidelity is thrown away immediately; JPEG encodes a tall full-page
# capture far faster (~0.3-0.65s/render saved on real sites). Quality is kept high (92) so
# the introduced error stays perceptually negligible: measured bin shifts are <=0.012 OKLab
# ΔE — well under the cross-platform rendering drift the pipeline already tolerates — and
# the extracted palette is unchanged. Coverage is unaffected (still ``full_page=True``).
_SCREENSHOT_TYPE: Literal["jpeg"] = "jpeg"
_SCREENSHOT_QUALITY: int = 92

# Longest-edge target for the downscaled image used for quantization.
_DOWNSCALE_MAX_EDGE: int = 256

# Number of palette buckets requested from the adaptive quantizer.
_PALETTE_COLORS: int = 16

# Drop bins covering less than this fraction of sampled pixels (noise floor).
_MIN_BIN_FRACTION: float = 0.005

# Max distinct logo colors to keep.
_MAX_LOGO_COLORS: int = 5

# Cap the full-page capture height (CSS pixels). A page is decoded into a
# (height x width x 3) uint8 array PLUS a (height x width) bool keep-mask before any
# downscaling, and themes render concurrently, so an attacker-controlled tall page (e.g.
# 100,000px) could force hundreds of MB-GBs of RAM. 20,000px is far above any genuine page
# (real captures are a few thousand px) yet bounds a single channel to ~20k x ~2k = ~40M px.
# Pages at or under this cap are captured ``full_page=True`` exactly as before; taller pages
# fall back to a top-anchored clip of this height so the harvest still succeeds, bounded.
_MAX_CAPTURE_HEIGHT_PX: int = 20_000

# Decompression-bomb guard for decoding fetched/captured images. Sized to admit the bounded
# full-page capture (~40M px above, ~2x device-scale headroom) while rejecting tiny-file,
# huge-dimension bombs (e.g. a malicious favicon) before they allocate. Applied process-wide
# via ``Image.MAX_IMAGE_PIXELS`` so decoding such an image raises rather than silently
# allocating gigabytes; the callers treat the raise as a best-effort failure.
_MAX_IMAGE_PIXELS: int = 90_000_000
Image.MAX_IMAGE_PIXELS = _MAX_IMAGE_PIXELS

# Cap the bytes read from a page-controlled logo/favicon body, and the request timeout. A
# logo is downscaled to 64x64, so a few MB is already generous; this bounds memory against a
# huge body regardless of any (spoofable) Content-Length header.
_MAX_LOGO_BYTES: int = 5 * 1024 * 1024
_LOGO_REQUEST_TIMEOUT_MS: float = 10_000.0

# JS reporting the full document dimensions (CSS pixels) so an oversized capture can be
# clipped to a bounded height instead of decoding the whole page.
_DOCUMENT_SIZE_JS: str = r"""
() => {
    const d = document.documentElement;
    const b = document.body;
    return {
        width: Math.max(d ? d.scrollWidth : 0, b ? b.scrollWidth : 0, window.innerWidth),
        height: Math.max(d ? d.scrollHeight : 0, b ? b.scrollHeight : 0, window.innerHeight),
    };
}
"""


class _DocumentSize(TypedDict):
    """Full document dimensions in CSS pixels."""

    width: float
    height: float


class _LogoSource(TypedDict):
    """A candidate logo image: its resolved URL."""

    url: str


def _rgb_to_color(r: int, g: int, b: int) -> Color | None:
    """Convert an 8-bit RGB triple to a :class:`Color`."""
    return parse_css_color(f"rgb({r}, {g}, {b})")


async def harvest_screenshot(
    page: Page,
    consent_rects: list[Rect],
    device_scale_factor: float,
) -> list[ScreenshotBin]:
    """Capture, mask, and quantize a full-page screenshot into color bins.

    ``consent_rects`` are CSS-pixel rects (from :class:`RenderSession`); they are scaled by
    ``device_scale_factor`` and zeroed out of the raw screenshot before quantizing.
    """
    # Bound capture height: a pathologically tall (e.g. attacker-controlled) page would decode
    # into a huge array + keep-mask before any downscaling. Pages at/under the cap are captured
    # full-page exactly as before; taller pages fall back to a top-anchored clip.
    size = cast(_DocumentSize, await page.evaluate(_DOCUMENT_SIZE_JS))
    if size["height"] > _MAX_CAPTURE_HEIGHT_PX:
        raw_bytes = await page.screenshot(
            type=_SCREENSHOT_TYPE,
            quality=_SCREENSHOT_QUALITY,
            clip={
                "x": 0,
                "y": 0,
                "width": size["width"],
                "height": _MAX_CAPTURE_HEIGHT_PX,
            },
        )
    else:
        raw_bytes = await page.screenshot(
            full_page=True, type=_SCREENSHOT_TYPE, quality=_SCREENSHOT_QUALITY
        )
    image = Image.open(io.BytesIO(raw_bytes)).convert("RGB")
    array = np.asarray(image, dtype=np.uint8).copy()

    height, width = array.shape[0], array.shape[1]

    # Build a boolean keep-mask; consent regions are excluded from sampling entirely.
    keep = np.ones((height, width), dtype=bool)
    for rect in consent_rects:
        x0 = max(0, int(rect.x * device_scale_factor))
        y0 = max(0, int(rect.y * device_scale_factor))
        x1 = min(width, int((rect.x + rect.width) * device_scale_factor))
        y1 = min(height, int((rect.y + rect.height) * device_scale_factor))
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

    # Stable secondary key on the color hex so equal-area bins sort deterministically
    # regardless of palette-index order.
    bins.sort(key=lambda item: (-item.area_fraction, item.color.hex))
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


async def harvest_logo_colors(page: Page) -> list[Color]:
    """Sample dominant colors from a discoverable logo/favicon image.

    Best-effort: returns an empty list if no logo is discoverable or fetching/decoding
    fails. Colors are ordered by coverage (most dominant first), opaque pixels only.
    """
    try:
        sources = cast(list[_LogoSource], await page.evaluate(_LOGO_SOURCES_JS))
    except Exception:  # discovery is best-effort
        return []

    for source in sources:
        colors = await _sample_logo(page, source["url"])
        if colors:
            return colors
    return []


async def _sample_logo(page: Page, url: str) -> list[Color]:
    """Fetch and quantize a single logo image URL into dominant colors."""
    try:
        response = await page.request.get(url, timeout=_LOGO_REQUEST_TIMEOUT_MS)
        if not response.ok:
            return []
        # Reject an oversized body up front via the (spoofable) Content-Length header...
        content_length = response.headers.get("content-length")
        if content_length is not None and int(content_length) > _MAX_LOGO_BYTES:
            return []
        data = await response.body()
        # ...and again on the actual bytes, since the header may lie or be absent.
        if len(data) > _MAX_LOGO_BYTES:
            return []
        # ``Image.MAX_IMAGE_PIXELS`` makes a tiny-file/huge-dimension bomb raise here.
        image = Image.open(io.BytesIO(data)).convert("RGBA")
    except Exception:  # fetch/decode best-effort (incl. timeout, bad header, decomp bomb)
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
