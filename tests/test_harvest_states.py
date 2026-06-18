"""Browserless tests for hover-state change detection.

:func:`colorsense.harvest.states.probe_hover_states` decides whether a clickable element
has a *real* hover color change (resting bg is ``None``, OR hover hex differs, OR hover
alpha differs) and degrades to a no-op when no CDP session can be established. Both behaviors
are normally only exercised behind the ``browser`` marker, but the function is seam-isolated:
``_open_cdp`` / ``_document_root`` / ``_read_hover_bg`` are module-level async helpers that
take the live ``Page``/``CDPSession``. By monkeypatching those seams we drive the real
predicate with synthetic elements and hover colors — no Playwright, no Chromium.
"""

from __future__ import annotations

import pytest

from colorsense.color.primitives import parse_css_color
from colorsense.harvest import states
from colorsense.models import BoundingBox, Color, HarvestedElement


def _color(value: str) -> Color:
    c = parse_css_color(value)
    assert c is not None, f"unparseable test color {value!r}"
    return c


def _element(
    *,
    bg: Color | None,
    clickable: bool = True,
) -> HarvestedElement:
    """A minimal clickable HarvestedElement with the given resting background."""
    return HarvestedElement(
        tag="button",
        role=None,
        id=None,
        bounding_box=BoundingBox(x=0.0, y=0.0, width=100.0, height=40.0),
        position="static",
        bg=bg,
        text=None,
        border=None,
        is_iframe=False,
        cross_origin=False,
        shadow_host=False,
        clickable=clickable,
        has_hover_color_change=False,
        hover_bg=None,
        vendor_match=False,
        visible=True,
        aria_hidden=False,
    )


# A stand-in for a real Playwright Page: never actually touched because we replace every
# seam that would call into it. ``object()`` would do, but a named class reads clearer.
class _FakePage:
    pass


def _patch_cdp(
    monkeypatch: pytest.MonkeyPatch,
    hover_by_selector: dict[str, Color | None],
) -> None:
    """Replace the CDP seams so the pure predicate runs without a browser.

    ``_open_cdp`` returns a non-None sentinel (so the no-op early return is skipped),
    ``_document_root`` returns a fake root nodeId, and ``_read_hover_bg`` looks the forced
    hover color up by selector from ``hover_by_selector``.
    """

    async def _fake_open_cdp(_page: object) -> object:
        return object()  # any non-None client

    async def _fake_document_root(_client: object) -> int:
        return 1

    async def _fake_read_hover_bg(_client: object, _root: int, selector: str) -> Color | None:
        return hover_by_selector.get(selector)

    monkeypatch.setattr(states, "_open_cdp", _fake_open_cdp)
    monkeypatch.setattr(states, "_document_root", _fake_document_root)
    monkeypatch.setattr(states, "_read_hover_bg", _fake_read_hover_bg)


async def test_no_resting_bg_is_a_change(monkeypatch: pytest.MonkeyPatch) -> None:
    # Resting bg is None: any readable hover color counts as a real change.
    el = _element(bg=None)
    hover = _color("#ff6600")
    _patch_cdp(monkeypatch, {"#sel": hover})

    [result] = await states.probe_hover_states(_FakePage(), [el], ["#sel"])

    assert result.has_hover_color_change is True
    assert result.hover_bg == hover


async def test_same_hex_and_alpha_is_no_change(monkeypatch: pytest.MonkeyPatch) -> None:
    # Hover bg is byte-identical to resting: not a change, element returned unchanged.
    resting = _color("#2244aa")
    el = _element(bg=resting)
    _patch_cdp(monkeypatch, {"#sel": _color("#2244aa")})

    [result] = await states.probe_hover_states(_FakePage(), [el], ["#sel"])

    assert result.has_hover_color_change is False
    assert result.hover_bg is None
    assert result is el  # untouched: same object, no model_copy


async def test_hex_difference_is_a_change(monkeypatch: pytest.MonkeyPatch) -> None:
    # Resting #2244aa -> hover #ff6600: differing hex is a real change.
    resting = _color("#2244aa")
    hover = _color("#ff6600")
    el = _element(bg=resting)
    _patch_cdp(monkeypatch, {"#sel": hover})

    [result] = await states.probe_hover_states(_FakePage(), [el], ["#sel"])

    assert result.has_hover_color_change is True
    assert result.hover_bg == hover


async def test_alpha_only_difference_is_a_change(monkeypatch: pytest.MonkeyPatch) -> None:
    # Same hex but different alpha must still register as a change (the predicate ORs alpha).
    resting = Color(hex="#2244aa", lightness=0.4, chroma=0.1, hue=260.0, alpha=1.0)
    hover = Color(hex="#2244aa", lightness=0.4, chroma=0.1, hue=260.0, alpha=0.5)
    el = _element(bg=resting)
    _patch_cdp(monkeypatch, {"#sel": hover})

    [result] = await states.probe_hover_states(_FakePage(), [el], ["#sel"])

    assert result.has_hover_color_change is True
    assert result.hover_bg is hover
    assert result.hover_bg.alpha == pytest.approx(0.5)


async def test_unreadable_hover_leaves_element_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    # _read_hover_bg returning None (element gone / unparseable) is skipped, not a change.
    el = _element(bg=_color("#2244aa"))
    _patch_cdp(monkeypatch, {"#sel": None})

    [result] = await states.probe_hover_states(_FakePage(), [el], ["#sel"])

    assert result.has_hover_color_change is False
    assert result is el


async def test_non_clickable_elements_are_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    # Only clickable elements are probed; a non-clickable element is returned untouched even
    # when its selector would have reported a hover color.
    clickable = _element(bg=_color("#2244aa"))
    static = _element(bg=_color("#cccccc"), clickable=False)
    _patch_cdp(
        monkeypatch,
        {"#a": _color("#ff6600"), "#b": _color("#000000")},
    )

    result = await states.probe_hover_states(_FakePage(), [clickable, static], ["#a", "#b"])

    assert result[0].has_hover_color_change is True
    assert result[1].has_hover_color_change is False
    assert result[1] is static


async def test_cdp_unavailable_degrades_to_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    # When a CDP session cannot be established the whole pass is a no-op: the original
    # elements are returned unchanged (graceful degradation, never aborting the harvest).
    async def _no_cdp(_page: object) -> None:
        return None

    monkeypatch.setattr(states, "_open_cdp", _no_cdp)

    elements = [_element(bg=None), _element(bg=_color("#2244aa"))]
    result = await states.probe_hover_states(_FakePage(), elements, ["#a", "#b"])

    assert result == elements
    assert all(not e.has_hover_color_change for e in result)
    # A fresh list is returned (not the caller's), but the elements are the same objects.
    assert result is not elements
    assert all(r is e for r, e in zip(result, elements, strict=True))


async def test_document_root_unavailable_degrades_to_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # CDP opens but the document root can't be fetched: also a no-op.
    async def _open(_page: object) -> object:
        return object()

    async def _no_root(_client: object) -> None:
        return None

    monkeypatch.setattr(states, "_open_cdp", _open)
    monkeypatch.setattr(states, "_document_root", _no_root)

    elements = [_element(bg=None)]
    result = await states.probe_hover_states(_FakePage(), elements, ["#a"])

    assert result == elements
    assert result[0].has_hover_color_change is False
