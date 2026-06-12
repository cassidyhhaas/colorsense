"""Unit tests for the screenshot binning / capture-bounding helpers.

These exercise :func:`colorsense.harvest.screenshot._quantize`, ``_rgb_to_color``, and the
capture-bounding / decode-guard behavior of :func:`harvest_screenshot` with small synthetic
numpy/PIL images and fake pages of known composition — no Chromium, no network, so they are
NOT marked ``browser``. They assert the area fractions, bin ordering, mask handling,
noise-floor behavior, dimension clipping, and the oversized-decode guard, plus that
``harvest_page`` surfaces harvest failures as the public ``RenderError``.
"""

from __future__ import annotations

import io
import struct
import zlib
from typing import Any

import numpy as np
import pytest
from PIL import Image

import colorsense.harvest as harvest_mod
from colorsense.config import load_default_config
from colorsense.harvest import RenderError, harvest_page
from colorsense.harvest.screenshot import (
    _MAX_CAPTURE_HEIGHT_PX,
    _MAX_CAPTURE_WIDTH_PX,
    _MAX_DECODE_PIXELS,
    _MIN_BIN_FRACTION,
    _OversizedCaptureError,
    _quantize,
    _rgb_to_color,
    harvest_screenshot,
)
from colorsense.models import Rect, Theme, Viewport

VIEWPORT = Viewport(width=1280, height=800, device_scale_factor=1.0)


def _solid(height: int, width: int, rgb: tuple[int, int, int]) -> np.ndarray:
    img = np.zeros((height, width, 3), dtype=np.uint8)
    img[:, :] = rgb
    return img


def test_rgb_to_color_basic() -> None:
    color = _rgb_to_color(255, 0, 0)
    assert color is not None
    assert color.hex == "#ff0000"


def test_quantize_two_regions_area_fractions() -> None:
    # Top 75 rows red, bottom 25 rows blue -> 0.75 / 0.25 split, red dominant first.
    array = np.zeros((100, 100, 3), dtype=np.uint8)
    array[:75, :] = (255, 0, 0)
    array[75:, :] = (0, 0, 255)
    keep = np.ones((100, 100), dtype=bool)

    bins = _quantize(array, keep)

    assert [b.color.hex for b in bins] == ["#ff0000", "#0000ff"]
    assert bins[0].area_fraction == pytest.approx(0.75, abs=1e-6)
    assert bins[1].area_fraction == pytest.approx(0.25, abs=1e-6)
    # Area fractions of the surviving bins sum to ~1 (whole image kept, no noise dropped).
    assert sum(b.area_fraction for b in bins) == pytest.approx(1.0, abs=1e-6)


def test_quantize_sorted_descending_by_area() -> None:
    array = np.zeros((100, 100, 3), dtype=np.uint8)
    array[:20, :] = (255, 0, 0)
    array[20:, :] = (0, 255, 0)  # green dominant (80%)
    keep = np.ones((100, 100), dtype=bool)

    bins = _quantize(array, keep)

    fractions = [b.area_fraction for b in bins]
    assert fractions == sorted(fractions, reverse=True)
    assert bins[0].color.hex == "#00ff00"


def test_quantize_mask_excludes_region() -> None:
    # Whole image is red, but the bottom quarter is a (would-be) different color that the
    # mask excludes; result should be pure red over the kept (top three-quarters) region.
    array = np.zeros((100, 100, 3), dtype=np.uint8)
    array[:75, :] = (255, 0, 0)
    array[75:, :] = (0, 0, 255)
    keep = np.ones((100, 100), dtype=bool)
    keep[75:, :] = False  # mask out the blue band

    bins = _quantize(array, keep)

    assert len(bins) == 1
    assert bins[0].color.hex == "#ff0000"
    assert bins[0].area_fraction == pytest.approx(1.0, abs=1e-6)


def test_quantize_all_masked_returns_empty() -> None:
    array = _solid(50, 50, (10, 20, 30))
    keep = np.zeros((50, 50), dtype=bool)
    assert _quantize(array, keep) == []


def test_quantize_drops_below_noise_floor() -> None:
    # A tiny speck below the noise floor is dropped; only the dominant color survives.
    side = 100
    array = _solid(side, side, (0, 0, 0))
    # Mark a region smaller than the noise floor as white.
    speck = max(1, int((_MIN_BIN_FRACTION * side * side) ** 0.5) - 1)
    array[:speck, :speck] = (255, 255, 255)
    keep = np.ones((side, side), dtype=bool)

    bins = _quantize(array, keep)

    hexes = {b.color.hex for b in bins}
    assert "#ffffff" not in hexes  # below-floor speck dropped
    assert "#000000" in hexes


def test_quantize_area_fractions_in_unit_range() -> None:
    array = np.zeros((128, 128, 3), dtype=np.uint8)
    array[:, :64] = (200, 100, 50)
    array[:, 64:] = (50, 100, 200)
    keep = np.ones((128, 128), dtype=bool)

    bins = _quantize(array, keep)

    assert bins  # at least one bin
    for b in bins:
        assert 0.0 <= b.area_fraction <= 1.0


def test_quantize_equal_area_bins_sorted_by_hex() -> None:
    # Two bins with identical area fractions must order by hex (the secondary sort
    # key); a regression to palette-index ordering would put red (palette-first)
    # ahead of blue.
    array = np.zeros((100, 100, 3), dtype=np.uint8)
    array[:, :50] = (255, 0, 0)
    array[:, 50:] = (0, 0, 255)
    keep = np.ones((100, 100), dtype=bool)

    bins = _quantize(array, keep)

    assert [b.area_fraction for b in bins] == [pytest.approx(0.5), pytest.approx(0.5)]
    assert [b.color.hex for b in bins] == ["#0000ff", "#ff0000"]


def _png_bytes(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), rgb).save(buf, format="PNG")
    return buf.getvalue()


def _bomb_png_bytes(pixels: int = _MAX_DECODE_PIXELS + 1) -> bytes:
    """A tiny PNG whose IHDR *declares* a pixel count of at least ``pixels``.

    Mimics a decompression bomb: the header parses but the declared pixel count is huge,
    so the guard must reject it before full decode.
    """
    data = bytearray(_png_bytes(10, 10, (0, 0, 0)))
    side = int(pixels**0.5) + 1  # side*side reaches at least ``pixels``
    # PNG layout: 8-byte signature, then IHDR chunk = 4B length + 4B type + 13B data + 4B CRC.
    # Width/height are the first 8 bytes of the IHDR data (offset 16); CRC covers type+data.
    struct.pack_into(">II", data, 16, side, side)
    struct.pack_into(">I", data, 29, zlib.crc32(data[12:29]))
    return bytes(data)


class _FakePage:
    """Minimal async stand-in recording how ``harvest_screenshot`` captures."""

    def __init__(self, doc_width: float, doc_height: float, image: bytes) -> None:
        self._doc_width = doc_width
        self._doc_height = doc_height
        self._image = image
        self.screenshot_kwargs: dict[str, Any] | None = None

    async def evaluate(self, _js: str, *_args: Any) -> dict[str, float]:
        return {"width": self._doc_width, "height": self._doc_height}

    async def screenshot(self, **kwargs: Any) -> bytes:
        self.screenshot_kwargs = kwargs
        return self._image


@pytest.mark.asyncio
async def test_harvest_screenshot_normal_page_uses_full_page() -> None:
    page = _FakePage(doc_width=1000.0, doc_height=3000.0, image=_png_bytes(40, 30, (255, 0, 0)))

    bins = await harvest_screenshot(page, [], 1.0)  # type: ignore[arg-type]

    assert page.screenshot_kwargs is not None
    assert page.screenshot_kwargs.get("full_page") is True
    assert "clip" not in page.screenshot_kwargs
    assert [b.color.hex for b in bins] == ["#ff0000"]


@pytest.mark.asyncio
async def test_harvest_screenshot_oversized_page_clips_height() -> None:
    page = _FakePage(
        doc_width=1000.0,
        doc_height=_MAX_CAPTURE_HEIGHT_PX + 50_000.0,
        image=_png_bytes(40, 30, (0, 0, 255)),
    )

    bins = await harvest_screenshot(page, [], 1.0)  # type: ignore[arg-type]

    assert page.screenshot_kwargs is not None
    assert page.screenshot_kwargs.get("full_page") is not True
    clip = page.screenshot_kwargs.get("clip")
    assert clip is not None
    assert clip["height"] == _MAX_CAPTURE_HEIGHT_PX
    assert clip["width"] == 1000.0  # within the width cap: passed through unclipped
    assert clip["x"] == 0 and clip["y"] == 0
    assert [b.color.hex for b in bins] == ["#0000ff"]


@pytest.mark.asyncio
async def test_harvest_screenshot_oversized_page_clips_width() -> None:
    # A pathologically WIDE page must also hit the clip branch, with the width clamped
    # (previously only height triggered clipping and width passed through unbounded).
    page = _FakePage(
        doc_width=_MAX_CAPTURE_WIDTH_PX + 50_000.0,
        doc_height=3000.0,
        image=_png_bytes(40, 30, (0, 255, 0)),
    )

    bins = await harvest_screenshot(page, [], 1.0)  # type: ignore[arg-type]

    assert page.screenshot_kwargs is not None
    assert page.screenshot_kwargs.get("full_page") is not True
    clip = page.screenshot_kwargs.get("clip")
    assert clip is not None
    assert clip["width"] == _MAX_CAPTURE_WIDTH_PX
    assert clip["height"] == 3000.0  # within the height cap: passed through unclipped
    assert clip["x"] == 0 and clip["y"] == 0
    assert [b.color.hex for b in bins] == ["#00ff00"]


@pytest.mark.asyncio
async def test_harvest_screenshot_oversized_page_clips_both_dimensions() -> None:
    # Both caps binding at once: the raw capped clip (20k x 10k = 200M px) exceeds the
    # 90M decode budget, so the height is additionally shrunk until the clip fits — the
    # fallback must SUCCEED on the pages it exists for, not trade RenderError for
    # RenderError (previously this exact shape raised at decode time).
    page = _FakePage(
        doc_width=_MAX_CAPTURE_WIDTH_PX + 1.0,
        doc_height=_MAX_CAPTURE_HEIGHT_PX + 1.0,
        image=_png_bytes(40, 30, (0, 0, 255)),
    )

    await harvest_screenshot(page, [], 1.0)  # type: ignore[arg-type]

    assert page.screenshot_kwargs is not None
    clip = page.screenshot_kwargs.get("clip")
    assert clip is not None
    assert clip["width"] == _MAX_CAPTURE_WIDTH_PX
    assert clip["height"] < _MAX_CAPTURE_HEIGHT_PX  # shrunk to fit the decode budget
    assert clip["width"] * clip["height"] <= _MAX_DECODE_PIXELS


@pytest.mark.asyncio
async def test_harvest_screenshot_retina_scale_fits_decode_budget() -> None:
    # device_scale_factor=2 (public via Viewport / the CLI --scale flag): the screenshot
    # decodes at DEVICE pixels, so the CSS-px clip must satisfy (w*2)*(h*2) <= budget.
    # A >20k-tall page at a 1280-wide viewport previously clipped to 20_000x1280 CSS px
    # = 102.4M device px > 90M and raised RenderError on exactly the pages the clip
    # fallback was built to survive.
    dsf = 2.0
    page = _FakePage(
        doc_width=1280.0,
        doc_height=_MAX_CAPTURE_HEIGHT_PX + 50_000.0,
        image=_png_bytes(40, 30, (0, 0, 255)),
    )

    bins = await harvest_screenshot(page, [], dsf)  # type: ignore[arg-type]

    assert page.screenshot_kwargs is not None
    clip = page.screenshot_kwargs.get("clip")
    assert clip is not None
    assert clip["width"] == 1280.0
    assert (clip["width"] * dsf) * (clip["height"] * dsf) <= _MAX_DECODE_PIXELS
    assert [b.color.hex for b in bins] == ["#0000ff"]


@pytest.mark.asyncio
async def test_harvest_screenshot_oversized_decode_raises() -> None:
    # An image whose declared dimensions exceed the decode cap is rejected from the header
    # alone (decompression-bomb guard), before any pixel data is decoded.
    page = _FakePage(doc_width=1000.0, doc_height=1000.0, image=_bomb_png_bytes())

    with pytest.raises(_OversizedCaptureError):
        await harvest_screenshot(page, [], 1.0)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_harvest_screenshot_pillow_bomb_raise_folds_into_oversize_error() -> None:
    # Beyond 2x Pillow's own default limit, ``Image.open`` itself raises
    # ``DecompressionBombError``; that must surface as the same dedicated oversize error.
    page = _FakePage(
        doc_width=1000.0,
        doc_height=1000.0,
        image=_bomb_png_bytes(pixels=_MAX_DECODE_PIXELS * 4),
    )

    with pytest.raises(_OversizedCaptureError):
        await harvest_screenshot(page, [], 1.0)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Consent-rect -> keep-mask scaling arithmetic (CSS px rects, device px image)
# ---------------------------------------------------------------------------


def _banded_png_bytes(width: int, height: int, band_rows: int) -> bytes:
    """A white image (device px) with the top ``band_rows`` rows solid red."""
    array = np.full((height, width, 3), 255, dtype=np.uint8)
    array[:band_rows, :] = (255, 0, 0)
    buf = io.BytesIO()
    Image.fromarray(array).save(buf, format="PNG")
    return buf.getvalue()


# A 200x100 device-px image whose top half (50 rows) is red, rest white.
_BAND_IMAGE = _banded_png_bytes(200, 100, 50)
# The CSS-px rect covering exactly that red band at device_scale_factor=2.0.
_BAND_RECT_CSS_AT_DSF2 = Rect(x=0.0, y=0.0, width=100.0, height=25.0)


@pytest.mark.asyncio
async def test_consent_rect_scaled_by_device_scale_factor() -> None:
    # At dsf=2 the CSS rect (100x25) must scale to device px (200x50) and mask the
    # whole red band; a dsf-scaling bug would mask only a quarter and leave red in.
    page = _FakePage(doc_width=100.0, doc_height=50.0, image=_BAND_IMAGE)

    bins = await harvest_screenshot(page, [_BAND_RECT_CSS_AT_DSF2], 2.0)  # type: ignore[arg-type]

    assert [b.color.hex for b in bins] == ["#ffffff"]
    assert bins[0].area_fraction == pytest.approx(1.0, abs=1e-6)


@pytest.mark.asyncio
async def test_consent_rect_at_dsf1_masks_only_unscaled_region() -> None:
    # Control for the dsf test: at dsf=1 the same rect masks only the 100x25
    # top-left corner, so most of the red band (7500 of 17500 kept px) survives.
    page = _FakePage(doc_width=200.0, doc_height=100.0, image=_BAND_IMAGE)

    bins = await harvest_screenshot(page, [_BAND_RECT_CSS_AT_DSF2], 1.0)  # type: ignore[arg-type]

    fractions = {b.color.hex: b.area_fraction for b in bins}
    assert fractions["#ffffff"] == pytest.approx(10000 / 17500, abs=1e-6)
    assert fractions["#ff0000"] == pytest.approx(7500 / 17500, abs=1e-6)
    assert bins[0].color.hex == "#ffffff"  # white dominates


@pytest.mark.asyncio
async def test_consent_rect_out_of_bounds_is_clamped() -> None:
    # A rect spilling past every edge (negative origin, oversized extent) must be
    # clamped to the image, not raise or wrap around with negative indices.
    page = _FakePage(doc_width=200.0, doc_height=100.0, image=_BAND_IMAGE)
    rect = Rect(x=-50.0, y=-30.0, width=1000.0, height=80.0)  # clamps to rows 0..50

    bins = await harvest_screenshot(page, [rect], 1.0)  # type: ignore[arg-type]

    assert [b.color.hex for b in bins] == ["#ffffff"]
    assert bins[0].area_fraction == pytest.approx(1.0, abs=1e-6)


@pytest.mark.asyncio
async def test_degenerate_consent_rects_are_ignored() -> None:
    # Rects whose scaled width/height collapse to zero (or are negative) must be
    # skipped entirely, leaving the full image sampled.
    page = _FakePage(doc_width=200.0, doc_height=100.0, image=_BAND_IMAGE)
    rects = [
        Rect(x=10.0, y=10.0, width=0.4, height=0.4),  # int-truncates to zero size
        Rect(x=10.0, y=10.0, width=-5.0, height=20.0),  # negative width
    ]

    bins = await harvest_screenshot(page, rects, 1.0)  # type: ignore[arg-type]

    fractions = {b.color.hex: b.area_fraction for b in bins}
    assert fractions == {
        "#ff0000": pytest.approx(0.5, abs=1e-6),
        "#ffffff": pytest.approx(0.5, abs=1e-6),
    }


# ---------------------------------------------------------------------------
# harvest_page surfaces harvest failures as the public RenderError
# ---------------------------------------------------------------------------


class _HarvestFakePage:
    """A fake live page driving the real harvest JS callers without a browser.

    ``evaluate`` dispatches on the payload JS: the document-size probe gets the configured
    dimensions, the DOM walker gets ``dom_payload``, and everything else (tokens) gets an
    empty list. ``screenshot`` returns the configured bytes. There is no ``context``
    attribute, so hover probing's CDP open fails and degrades to its documented no-op.
    """

    def __init__(self, image: bytes, dom_payload: list[dict[str, Any]] | None = None) -> None:
        self._image = image
        self._dom_payload = dom_payload or []

    async def evaluate(self, js: str, *_args: Any) -> Any:
        if "scrollHeight" in js:  # the document-size probe
            return {"width": 1000.0, "height": 1000.0}
        if "querySelectorAll('*')" in js:  # the DOM element walker
            return self._dom_payload
        return []  # tokens / anything else

    async def screenshot(self, **_kwargs: Any) -> bytes:
        return self._image


class _FakeRenderSession:
    """A fake ``RenderSession`` exposing a :class:`_HarvestFakePage`."""

    page: _HarvestFakePage
    consent_rects: list[Any]

    _next_page: _HarvestFakePage  # set by the test before construction

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self.page = type(self)._next_page
        self.consent_rects = []

    async def __aenter__(self) -> _FakeRenderSession:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def goto(self, _url: str, **_kwargs: Any) -> None:
        return None


def _install_fake_session(monkeypatch: pytest.MonkeyPatch, page: _HarvestFakePage) -> None:
    _FakeRenderSession._next_page = page
    monkeypatch.setattr(harvest_mod, "RenderSession", _FakeRenderSession)


@pytest.mark.asyncio
async def test_harvest_page_wraps_oversized_capture_as_render_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = "https://example.test/"
    _install_fake_session(monkeypatch, _HarvestFakePage(image=_bomb_png_bytes()))

    with pytest.raises(RenderError) as excinfo:
        await harvest_page(url, Theme.light, load_default_config(), VIEWPORT)

    assert excinfo.value.url == url
    assert isinstance(excinfo.value.__cause__, _OversizedCaptureError)


@pytest.mark.asyncio
async def test_harvest_page_wraps_malformed_payload_as_render_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A hostile page that tampers with DOM APIs can make the in-page JS return malformed
    # element records; the resulting KeyError/TypeError/ValidationError must surface as the
    # public RenderError, not leak as a raw internal exception.
    url = "https://example.test/"
    _install_fake_session(
        monkeypatch,
        _HarvestFakePage(image=_png_bytes(40, 30, (255, 0, 0)), dom_payload=[{"bogus": 1}]),
    )

    with pytest.raises(RenderError) as excinfo:
        await harvest_page(url, Theme.light, load_default_config(), VIEWPORT)

    assert excinfo.value.url == url
    assert excinfo.value.__cause__ is not None
