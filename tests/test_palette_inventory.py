"""Unit tests for :mod:`colorsense.palette.inventory`."""

from __future__ import annotations

import pytest

from colorsense.color.primitives import delta_e, parse_css_color
from colorsense.models import (
    ClassifiedElement,
    Color,
    ComponentType,
    Harvest,
    HarvestedElement,
    Rect,
    ScreenshotBin,
    Theme,
    Viewport,
)
from colorsense.palette.inventory import (
    DELTA_E_CLUSTER,
    DELTA_E_MATCH,
    build_inventory,
)


def _color(value: str) -> Color:
    c = parse_css_color(value)
    assert c is not None
    return c


def _viewport() -> Viewport:
    return Viewport(w=1280, h=720, device_scale_factor=1.0)


def _harvest(bins: list[ScreenshotBin]) -> Harvest:
    return Harvest(
        url="https://example.test",
        theme=Theme.light,
        viewport=_viewport(),
        screenshot_bins=bins,
    )


def _element(bg: Color | None) -> HarvestedElement:
    return HarvestedElement(
        tag="div",
        role=None,
        id=None,
        rect=Rect(x=0.0, y=0.0, w=10.0, h=10.0),
        position="static",
        bg=bg,
        text=None,
        border=None,
        is_iframe=False,
        cross_origin=False,
        shadow_host=False,
        clickable=False,
        has_hover_color_change=False,
        hover_bg=None,
        vendor_match=False,
        visible=True,
        aria_hidden=False,
    )


def _classified(bg: Color | None, dist: dict[ComponentType, float]) -> ClassifiedElement:
    return ClassifiedElement(element=_element(bg), component_dist=dist)


def test_near_identical_colors_merge() -> None:
    near_a = _color("#3366cc")
    near_b = _color("#3367cc")
    distinct = _color("#ffffff")

    # Sanity: these two truly merge / stay-separate under the module thresholds.
    assert delta_e(near_a, near_b) <= DELTA_E_CLUSTER
    assert delta_e(near_a, distinct) > DELTA_E_CLUSTER

    harvest = _harvest(
        [
            ScreenshotBin(color=near_a, area_fraction=0.3),
            ScreenshotBin(color=near_b, area_fraction=0.4),
            ScreenshotBin(color=distinct, area_fraction=0.3),
        ]
    )

    clusters = build_inventory(harvest, [])

    assert len(clusters) == 2
    merged = max(clusters, key=lambda c: c.member_count)
    assert merged.member_count == 2
    assert merged.area_weight == pytest.approx(0.7, abs=1e-9)

    other = min(clusters, key=lambda c: c.member_count)
    assert other.member_count == 1
    assert other.area_weight == pytest.approx(0.3, abs=1e-9)
    assert other.color.hex == "#ffffff"


def test_component_mix_aggregates() -> None:
    surface = _color("#3366cc")
    harvest = _harvest([ScreenshotBin(color=surface, area_fraction=0.5)])

    classified = [_classified(_color("#3366cc"), {ComponentType.cta_bg: 1.0})]

    clusters = build_inventory(harvest, classified)

    assert len(clusters) == 1
    mix = clusters[0].component_mix
    assert mix
    dominant = max(mix, key=lambda k: mix[k])
    assert dominant == ComponentType.cta_bg
    assert sum(mix.values()) == pytest.approx(1.0, abs=1e-9)


def test_component_mix_aggregates_multiple_types() -> None:
    surface = _color("#3366cc")
    harvest = _harvest([ScreenshotBin(color=surface, area_fraction=0.5)])

    classified = [
        _classified(_color("#3366cc"), {ComponentType.cta_bg: 0.75}),
        _classified(_color("#3367cc"), {ComponentType.header_bg: 0.25}),
    ]

    clusters = build_inventory(harvest, classified)

    assert len(clusters) == 1
    mix = clusters[0].component_mix
    assert sum(mix.values()) == pytest.approx(1.0, abs=1e-9)
    dominant = max(mix, key=lambda k: mix[k])
    assert dominant == ComponentType.cta_bg


def test_unmatched_element_creates_zero_area_entry() -> None:
    # No bins at all; an element's semantics must still be preserved.
    harvest = _harvest([])
    classified = [_classified(_color("#112233"), {ComponentType.link: 1.0})]

    clusters = build_inventory(harvest, classified)

    assert len(clusters) == 1
    assert clusters[0].area_weight == pytest.approx(0.0, abs=1e-9)
    assert clusters[0].color.hex == "#112233"
    assert max(clusters[0].component_mix) == ComponentType.link


def test_element_far_from_all_bins_is_new_cluster() -> None:
    harvest = _harvest([ScreenshotBin(color=_color("#ffffff"), area_fraction=0.8)])
    far = _color("#000000")
    assert delta_e(far, _color("#ffffff")) > DELTA_E_MATCH

    classified = [_classified(far, {ComponentType.page_text: 1.0})]
    clusters = build_inventory(harvest, classified)

    assert len(clusters) == 2
    black = next(c for c in clusters if c.color.hex == "#000000")
    assert black.area_weight == pytest.approx(0.0, abs=1e-9)
    assert max(black.component_mix) == ComponentType.page_text


def test_element_without_bg_or_dist_is_ignored() -> None:
    harvest = _harvest([ScreenshotBin(color=_color("#3366cc"), area_fraction=0.5)])
    classified = [
        _classified(None, {ComponentType.cta_bg: 1.0}),
        _classified(_color("#3366cc"), {}),
    ]

    clusters = build_inventory(harvest, classified)

    assert len(clusters) == 1
    assert clusters[0].component_mix == {}


def test_clusters_sorted_by_area_descending() -> None:
    harvest = _harvest(
        [
            ScreenshotBin(color=_color("#ff0000"), area_fraction=0.1),
            ScreenshotBin(color=_color("#00ff00"), area_fraction=0.5),
            ScreenshotBin(color=_color("#0000ff"), area_fraction=0.3),
        ]
    )

    clusters = build_inventory(harvest, [])
    weights = [c.area_weight for c in clusters]
    assert weights == sorted(weights, reverse=True)


def test_determinism() -> None:
    harvest = _harvest(
        [
            ScreenshotBin(color=_color("#3366cc"), area_fraction=0.3),
            ScreenshotBin(color=_color("#3367cc"), area_fraction=0.4),
            ScreenshotBin(color=_color("#ffffff"), area_fraction=0.3),
        ]
    )
    classified = [
        _classified(_color("#3366cc"), {ComponentType.cta_bg: 1.0}),
        _classified(_color("#ffffff"), {ComponentType.page_bg: 1.0}),
    ]

    first = build_inventory(harvest, classified)
    second = build_inventory(harvest, classified)

    assert first == second


def test_thresholds_relationship() -> None:
    # Clustering threshold must not exceed the match threshold.
    assert DELTA_E_CLUSTER <= DELTA_E_MATCH
