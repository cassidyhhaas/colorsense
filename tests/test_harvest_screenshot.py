"""Unit tests for the pure screenshot binning / area-fraction helpers.

These exercise :func:`colorsense.harvest.screenshot._quantize` and ``_rgb_to_color`` with
small synthetic numpy/PIL images of known composition — no Chromium, no network, so they
are NOT marked ``browser``. They assert the area fractions, bin ordering, mask handling,
and noise-floor behavior of the quantizer.
"""

from __future__ import annotations

import io
from typing import Any

import numpy as np
import pytest
from PIL import Image

from colorsense.harvest.screenshot import (
    _MAX_CAPTURE_HEIGHT_PX,
    _MAX_LOGO_BYTES,
    _MIN_BIN_FRACTION,
    _quantize,
    _rgb_to_color,
    _sample_logo,
    harvest_screenshot,
)


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


def _png_bytes(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), rgb).save(buf, format="PNG")
    return buf.getvalue()


class _FakePage:
    """Minimal async stand-in recording how ``harvest_screenshot`` captures."""

    def __init__(self, doc_height: float, image: bytes) -> None:
        self._doc_height = doc_height
        self._image = image
        self.screenshot_kwargs: dict[str, Any] | None = None

    async def evaluate(self, _js: str, *_args: Any) -> dict[str, float]:
        return {"width": 1000.0, "height": self._doc_height}

    async def screenshot(self, **kwargs: Any) -> bytes:
        self.screenshot_kwargs = kwargs
        return self._image


@pytest.mark.asyncio
async def test_harvest_screenshot_normal_page_uses_full_page() -> None:
    page = _FakePage(doc_height=3000.0, image=_png_bytes(40, 30, (255, 0, 0)))

    bins = await harvest_screenshot(page, [], 1.0)  # type: ignore[arg-type]

    assert page.screenshot_kwargs is not None
    assert page.screenshot_kwargs.get("full_page") is True
    assert "clip" not in page.screenshot_kwargs
    assert [b.color.hex for b in bins] == ["#ff0000"]


@pytest.mark.asyncio
async def test_harvest_screenshot_oversized_page_clips_height() -> None:
    page = _FakePage(
        doc_height=_MAX_CAPTURE_HEIGHT_PX + 50_000.0,
        image=_png_bytes(40, 30, (0, 0, 255)),
    )

    bins = await harvest_screenshot(page, [], 1.0)  # type: ignore[arg-type]

    assert page.screenshot_kwargs is not None
    assert page.screenshot_kwargs.get("full_page") is not True
    clip = page.screenshot_kwargs.get("clip")
    assert clip is not None
    assert clip["height"] == _MAX_CAPTURE_HEIGHT_PX
    assert clip["x"] == 0 and clip["y"] == 0
    assert [b.color.hex for b in bins] == ["#0000ff"]


class _FakeResponse:
    def __init__(self, body: bytes, headers: dict[str, str] | None = None) -> None:
        self.ok = True
        self._body = body
        self.headers = headers or {}

    async def body(self) -> bytes:
        return self._body


class _FakeRequest:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response
        self.timeout: float | None = None

    async def get(self, _url: str, timeout: float | None = None) -> _FakeResponse:
        self.timeout = timeout
        return self._response


class _LogoPage:
    def __init__(self, response: _FakeResponse) -> None:
        self.request = _FakeRequest(response)


@pytest.mark.asyncio
async def test_sample_logo_passes_timeout() -> None:
    page = _LogoPage(_FakeResponse(_png_bytes(16, 16, (10, 200, 30))))

    colors = await _sample_logo(page, "https://x/logo.png")  # type: ignore[arg-type]

    assert page.request.timeout is not None
    assert colors  # a valid logo still yields colors


@pytest.mark.asyncio
async def test_sample_logo_rejects_oversized_content_length() -> None:
    page = _LogoPage(
        _FakeResponse(
            _png_bytes(16, 16, (10, 200, 30)),
            headers={"content-length": str(_MAX_LOGO_BYTES + 1)},
        )
    )

    assert await _sample_logo(page, "https://x/logo.png") == []  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_sample_logo_rejects_oversized_body() -> None:
    page = _LogoPage(_FakeResponse(b"\x00" * (_MAX_LOGO_BYTES + 1)))

    assert await _sample_logo(page, "https://x/logo.png") == []  # type: ignore[arg-type]
