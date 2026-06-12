"""Full-page screenshot quantization.

Takes a full-page Playwright screenshot, masks out consent/overlay regions (so a cookie
banner cannot pollute the palette), downscales, and quantizes pixels into
`ScreenshotBin` objects each carrying an ``area_fraction`` (the
fraction of *sampled* pixels).

Quantization is deterministic given a fixed fixture: Pillow's adaptive palette with a
fixed max-color count, computed over the masked, downscaled image.
"""

from __future__ import annotations

import asyncio
import io
import warnings
from typing import Literal, TypedDict, cast

import numpy as np
from PIL import Image
from playwright.async_api import Page

from colorsense.color.primitives import parse_css_color
from colorsense.harvest.render import EVAL_TIMEOUT_S
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

# Cap the full-page capture dimensions (CSS pixels). A page is decoded into a
# (height x width x 3) uint8 array PLUS a (height x width) bool keep-mask before any
# downscaling, and themes render concurrently, so an attacker-controlled tall *or wide*
# page (e.g. 100,000px in either direction) could force hundreds of MB-GBs of RAM. The
# caps are far above any genuine page (real captures are a few thousand px each way) yet
# bound a single channel to ~20k x ~10k worst case. Pages within the caps AND the decode
# budget below are captured ``full_page=True`` exactly as before; an oversized page falls
# back to a top-left-anchored clip clamped to the caps and then shrunk (height first —
# oversized pages are overwhelmingly tall, and width carries the layout) until the clip
# fits the decode budget at the session's device scale factor, so the harvest still
# succeeds, bounded, at any supported scale factor.
_MAX_CAPTURE_HEIGHT_PX: int = 20_000
_MAX_CAPTURE_WIDTH_PX: int = 10_000

# Decompression-bomb guard for decoding the captured screenshot, counted in DEVICE pixels
# (Playwright captures at ``device_scale_factor`` scale): the capture clip above is
# pre-shrunk so ``(w * dsf) * (h * dsf)`` stays within this budget, making this check a
# pure backstop for captures whose decoded dimensions disagree with the document-size
# probe. ``Image.open`` lazily parses only the header, so the declared dimensions are
# checked against this cap *before* ``.convert("RGB")`` triggers full pixel decoding; an
# oversized image raises ``_OversizedCaptureError`` instead of silently allocating
# gigabytes. Deliberately a local per-decode check, not a process-wide
# ``Image.MAX_IMAGE_PIXELS`` mutation: a library must not overwrite host-process Pillow
# state on import.
_MAX_DECODE_PIXELS: int = 90_000_000

# Safety margin applied when fitting the capture clip into the decode budget: Chromium
# rounds the clip to whole device pixels, so an exactly-budget-sized clip could decode a
# hair over the cap and trip the backstop.
_DECODE_BUDGET_MARGIN: float = 0.98


class _OversizedCaptureError(Exception):
    """A captured screenshot's declared dimensions exceed the decode pixel cap.

    Module-internal: `colorsense.harvest.harvest_page` catches it and surfaces it as
    a public ``RenderError``.
    """


# JS reporting the full document dimensions (CSS pixels) so an oversized capture can be
# clipped to bounded dimensions instead of decoding the whole page.
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


def _rgb_to_color(r: int, g: int, b: int) -> Color | None:
    """Convert an 8-bit RGB triple to a [`Color`][colorsense.Color]."""
    return parse_css_color(f"rgb({r}, {g}, {b})")


async def harvest_screenshot(
    page: Page,
    consent_rects: list[Rect],
    device_scale_factor: float,
) -> list[ScreenshotBin]:
    """Capture, mask, and quantize a full-page screenshot into color bins.

    ``consent_rects`` are CSS-pixel rects (from `RenderSession`); they are scaled by
    ``device_scale_factor`` and zeroed out of the raw screenshot before quantizing.

    Raises `_OversizedCaptureError` if the captured image's declared dimensions
    exceed the decode pixel cap (surfaced by ``harvest_page`` as ``RenderError``).
    """
    # Bound capture dimensions: a pathologically tall or wide (e.g. attacker-controlled)
    # page would decode into a huge array + keep-mask before any downscaling. The clip is
    # clamped to the CSS-pixel caps and then shrunk (height first) until it fits the
    # decode budget at this device scale factor — the screenshot decodes at DEVICE pixels,
    # so a CSS-pixel-capped clip alone would still blow the budget at dsf >= 2 (or when
    # both dimension caps bind at once).
    # Bounded like the DOM/token evaluates (``page.screenshot`` itself carries
    # Playwright's default action timeout, so only this probe needs an explicit bound).
    size = cast(
        _DocumentSize, await asyncio.wait_for(page.evaluate(_DOCUMENT_SIZE_JS), EVAL_TIMEOUT_S)
    )
    dsf = max(device_scale_factor, 1.0)  # a sub-1 dsf shrinks the decode; never inflate
    clip_width = min(size["width"], float(_MAX_CAPTURE_WIDTH_PX))
    clip_height = min(size["height"], float(_MAX_CAPTURE_HEIGHT_PX))
    budget_css_px = _MAX_DECODE_PIXELS * _DECODE_BUDGET_MARGIN / (dsf * dsf)
    if clip_width * clip_height > budget_css_px:
        clip_height = max(1.0, budget_css_px / clip_width)
    if clip_width < size["width"] or clip_height < size["height"]:
        raw_bytes = await page.screenshot(
            type=_SCREENSHOT_TYPE,
            quality=_SCREENSHOT_QUALITY,
            clip={"x": 0, "y": 0, "width": clip_width, "height": clip_height},
        )
    else:
        raw_bytes = await page.screenshot(
            full_page=True, type=_SCREENSHOT_TYPE, quality=_SCREENSHOT_QUALITY
        )
    # ``Image.open`` parses only the header; guard the declared pixel count before
    # ``.convert`` triggers the full decode (decompression-bomb defense). Pillow runs its
    # own default-limit check at open time — silence its advisory warning (our explicit cap
    # below is the authoritative guard) and fold its hard raise (at 2x its default limit)
    # into the same dedicated error. The filter is scoped to this decode, never global.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", Image.DecompressionBombWarning)
        try:
            image = Image.open(io.BytesIO(raw_bytes))
        except Image.DecompressionBombError as err:
            raise _OversizedCaptureError(str(err)) from err
    if image.width * image.height > _MAX_DECODE_PIXELS:
        raise _OversizedCaptureError(
            f"captured screenshot declares {image.width}x{image.height} px, "
            f"exceeding the {_MAX_DECODE_PIXELS} px decode cap"
        )
    array = np.asarray(image.convert("RGB"), dtype=np.uint8).copy()

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
