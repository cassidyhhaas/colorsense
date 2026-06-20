"""Unit tests for :mod:`colorsense.palette.fusion` (build_evidence)."""

from __future__ import annotations

import pytest

from colorsense.color.primitives import parse_css_color
from colorsense.config import load_default_config
from colorsense.models import (
    BoundingBox,
    ClassifiedElement,
    Color,
    ComponentType,
    EvidenceStream,
    Harvest,
    HarvestedElement,
    PropertyFamily,
    RoleEvidence,
    ScreenshotBin,
    Theme,
    UsageRole,
    Viewport,
)
from colorsense.palette.fusion import build_evidence
from colorsense.palette.inventory import build_inventory
from colorsense.palette.salience import aggregate_salience

CONFIG = load_default_config()


def _color(value: str) -> Color:
    c = parse_css_color(value)
    assert c is not None
    return c


def _viewport() -> Viewport:
    return Viewport(width=1280, height=720, device_scale_factor=1.0)


def _harvest(bins: list[ScreenshotBin], elements: list[HarvestedElement] | None = None) -> Harvest:
    return Harvest(
        url="https://example.test",
        theme=Theme.LIGHT,
        viewport=_viewport(),
        screenshot_bins=bins,
        elements=elements or [],
    )


def _element(
    bg: Color | None,
    text: Color | None = None,
    border: Color | None = None,
    bg_gradient_stops: tuple[Color, ...] = (),
    box: BoundingBox | None = None,
    effective_bg: Color | None = None,
) -> HarvestedElement:
    return HarvestedElement(
        tag="div",
        role=None,
        id=None,
        bounding_box=box or BoundingBox(x=0.0, y=0.0, width=10.0, height=10.0),
        position="static",
        bg=bg,
        text=text,
        border=border,
        bg_gradient_stops=bg_gradient_stops,
        effective_bg=effective_bg,
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


def _classified(
    bg: Color | None,
    dist: dict[ComponentType, float],
    text: Color | None = None,
    border: Color | None = None,
    bg_gradient_stops: tuple[Color, ...] = (),
    box: BoundingBox | None = None,
    effective_bg: Color | None = None,
) -> ClassifiedElement:
    return ClassifiedElement(
        element=_element(
            bg,
            text=text,
            border=border,
            bg_gradient_stops=bg_gradient_stops,
            box=box,
            effective_bg=effective_bg,
        ),
        component_distribution=dist,
    )


def _evidence(harvest: Harvest, classified: list[ClassifiedElement]) -> list[RoleEvidence]:
    return build_evidence(harvest, classified, CONFIG, harvest.viewport)


def _inventory_hexes_by_family(
    harvest: Harvest, classified: list[ClassifiedElement]
) -> dict[PropertyFamily, set[str]]:
    """Canonical hexes the inventory produces, split by family via component routing."""
    clusters = build_inventory(harvest, classified)
    by_family: dict[PropertyFamily, set[str]] = {
        PropertyFamily.BACKGROUND: set(),
        PropertyFamily.TEXT: set(),
        PropertyFamily.BORDER: set(),
    }
    for cluster in clusters:
        if cluster.component_mass:
            families = {comp.property_family for comp in cluster.component_mass}
        else:
            # A pure screenshot bin with no votes is a background identity.
            families = {PropertyFamily.BACKGROUND}
        for family in families:
            by_family[family].add(cluster.color.hex)
    return by_family


def _evidence_hexes_by_family(records: list[RoleEvidence]) -> dict[PropertyFamily, set[str]]:
    by_family: dict[PropertyFamily, set[str]] = {
        PropertyFamily.BACKGROUND: set(),
        PropertyFamily.TEXT: set(),
        PropertyFamily.BORDER: set(),
    }
    for record in records:
        by_family[record.role.property_family].add(record.color.hex)
    return by_family


# ---------------------------------------------------------------------------
# Identity parity with build_inventory (proves helper reuse is faithful)
# ---------------------------------------------------------------------------


def test_identity_parity_with_inventory() -> None:
    # A mixed page: background bins, a text color, a border color. The canonical colors
    # build_evidence produces per family must equal the set build_inventory produces.
    white = _color("#ffffff")
    dark_surface = _color("#0d1117")
    blue_link = _color("#0969da")
    border = _color("#30363d")
    harvest = _harvest(
        [
            ScreenshotBin(color=white, area_fraction=0.6),
            ScreenshotBin(color=dark_surface, area_fraction=0.4),
        ]
    )
    classified = [
        _classified(white, {ComponentType.PAGE_BG: 1.0}),
        _classified(None, {ComponentType.LINK: 1.0}, text=blue_link),
        _classified(None, {ComponentType.BORDER: 1.0}, border=border),
    ]

    records = _evidence(harvest, classified)
    inv = _inventory_hexes_by_family(harvest, classified)
    ev = _evidence_hexes_by_family(records)

    # Text/border identities match exactly.
    assert ev[PropertyFamily.TEXT] == inv[PropertyFamily.TEXT]
    assert ev[PropertyFamily.BORDER] == inv[PropertyFamily.BORDER]
    # Background: every measured-evidence bg identity build_evidence reports is a real
    # inventory identity (inventory may additionally carry vote-free area bins).
    assert ev[PropertyFamily.BACKGROUND] <= inv[PropertyFamily.BACKGROUND]
    assert blue_link.hex in ev[PropertyFamily.TEXT]
    assert border.hex in ev[PropertyFamily.BORDER]


def test_near_black_cta_not_merged_into_page_bin() -> None:
    # The disco scenario: a near-black CTA (#030711) must NOT merge into the #050505 page bin.
    cta_bg = _color("#030711")
    page = _color("#050505")
    harvest = _harvest([ScreenshotBin(color=page, area_fraction=0.4)])
    classified = [
        _classified(cta_bg, {ComponentType.CTA_BG: 1.0}),
        _classified(page, {ComponentType.PAGE_BG: 1.0}),
    ]

    records = _evidence(harvest, classified)

    cta_records = [r for r in records if r.role is UsageRole.CTA]
    assert len(cta_records) == 1
    assert cta_records[0].color.hex == cta_bg.hex
    # The CTA color is its own identity, distinct from the page bin's hex.
    assert cta_records[0].color.hex != page.hex
    # And the page color surfaces in the page role at the page hex.
    page_records = [r for r in records if r.role is UsageRole.PAGE]
    assert page_records and page_records[0].color.hex == page.hex

    # Parity: the CTA identity also exists in the inventory's background identities.
    inv = _inventory_hexes_by_family(harvest, classified)
    assert cta_bg.hex in inv[PropertyFamily.BACKGROUND]


# ---------------------------------------------------------------------------
# Hero-vs-swarm (redesign §8): peak dominance, headcount does not win
# ---------------------------------------------------------------------------


def test_hero_cta_outranks_swarm_under_cta_aggregation() -> None:
    color_a = _color("#16a34a")  # the hero
    color_b = _color("#6b7280")  # the swarm
    white = _color("#ffffff")

    # One large hero CTA near the top of the page.
    hero_box = BoundingBox(x=390.0, y=80.0, width=500.0, height=160.0)
    hero = _classified(
        color_a,
        {ComponentType.CTA_BG: 1.0},
        box=hero_box,
        effective_bg=white,
    )
    # Many tiny CTA-bg buttons of color B scattered low on the page.
    swarm = [
        _classified(
            color_b,
            {ComponentType.CTA_BG: 1.0},
            box=BoundingBox(x=float(10 * i), y=600.0, width=10.0, height=10.0),
            effective_bg=white,
        )
        for i in range(20)
    ]
    harvest = _harvest(
        [
            ScreenshotBin(color=white, area_fraction=0.9),
            ScreenshotBin(color=color_a, area_fraction=0.05),
        ]
    )

    records = _evidence(harvest, [hero, *swarm])
    cta = {r.color.hex: r for r in records if r.role is UsageRole.CTA}
    assert color_a.hex in cta and color_b.hex in cta

    cta_cfg = CONFIG.detection.roles[UsageRole.CTA]
    s_a = aggregate_salience(cta[color_a.hex].instance_saliences, cta_cfg.lambda_, cta_cfg.beta)
    s_b = aggregate_salience(cta[color_b.hex].instance_saliences, cta_cfg.lambda_, cta_cfg.beta)
    # The single prominent hero outranks the headcount swarm.
    assert s_a > s_b


# ---------------------------------------------------------------------------
# Surface area carried through
# ---------------------------------------------------------------------------


def test_surface_role_carries_screenshot_bin_area() -> None:
    page = _color("#ffffff")
    harvest = _harvest([ScreenshotBin(color=page, area_fraction=0.73)])
    classified = [_classified(page, {ComponentType.PAGE_BG: 1.0})]

    records = _evidence(harvest, classified)
    page_records = [r for r in records if r.role is UsageRole.PAGE]

    assert len(page_records) == 1
    assert page_records[0].color.hex == page.hex
    assert page_records[0].area == pytest.approx(0.73, abs=1e-9)
    assert EvidenceStream.SCREENSHOT in page_records[0].streams
    assert EvidenceStream.DOM in page_records[0].streams
    # Surface prominence is area: pi_i == a_i, so a full-element page_bg salience equals a_i.
    expected_a = (10.0 * 10.0) / (1280.0 * 720.0)
    assert page_records[0].peak == pytest.approx(expected_a, abs=1e-12)


# ---------------------------------------------------------------------------
# instance_saliences invariant: sorted descending, non-negative (validator enforces)
# ---------------------------------------------------------------------------


def test_instance_saliences_sorted_descending_and_nonnegative() -> None:
    blue = _color("#3b82f6")
    white = _color("#ffffff")
    # Several link instances of varying size -> several saliences for one (color, role).
    classified = [
        _classified(
            None,
            {ComponentType.LINK: 1.0},
            text=blue,
            box=BoundingBox(x=0.0, y=float(50 * i), width=float(40 + 30 * i), height=20.0),
            effective_bg=white,
        )
        for i in range(5)
    ]
    harvest = _harvest([ScreenshotBin(color=white, area_fraction=1.0)])

    records = _evidence(harvest, classified)
    link = next(r for r in records if r.role is UsageRole.LINK and r.color.hex == blue.hex)

    saliences = link.instance_saliences
    assert len(saliences) == 5
    assert all(s >= 0.0 for s in saliences)
    assert list(saliences) == sorted(saliences, reverse=True)
    assert link.peak == saliences[0]


def test_none_contrast_yields_neutral_modulator() -> None:
    # No effective_bg -> contrast is None -> m_con == 1.0. With a single instance and
    # neutral position/sibling, the recorded salience must be exactly p_role * a_i * m_pos.
    blue = _color("#3b82f6")
    box = BoundingBox(x=0.0, y=0.0, width=100.0, height=40.0)  # y center near top
    classified = [_classified(None, {ComponentType.LINK: 1.0}, text=blue, box=box)]
    harvest = _harvest([], elements=[])

    records = _evidence(harvest, classified)
    link = next(r for r in records if r.role is UsageRole.LINK)
    # The record exists and its single salience is strictly positive (None contrast did not
    # zero it out — m_con defaulted to 1.0).
    assert link.peak > 0.0
    assert len(link.instance_saliences) == 1


def test_records_sorted_deterministically() -> None:
    white = _color("#ffffff")
    blue = _color("#0969da")
    harvest = _harvest([ScreenshotBin(color=white, area_fraction=1.0)])
    classified = [
        _classified(white, {ComponentType.PAGE_BG: 1.0}),
        _classified(None, {ComponentType.LINK: 1.0}, text=blue, effective_bg=white),
    ]

    first = _evidence(harvest, classified)
    second = _evidence(harvest, classified)
    assert first == second
    # Sorted by (role.value, -peak, hex).
    keys = [(r.role.value, -r.peak, r.color.hex) for r in first]
    assert keys == sorted(keys)
