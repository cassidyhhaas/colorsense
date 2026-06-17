"""Harvest integration tests.

All tests run against LOCAL fixture HTML loaded via ``file://`` URLs (no public network).
A single Chromium-backed harvest is reasonably cheap; each test harvests its own fixture
under the light theme (the dark-theme media block is asserted indirectly via the token
records, which capture every declared scope including the dark ``@media`` block).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from colorsense.classify.components import classify_components
from colorsense.config import Config, load_default_config
from colorsense.harvest import RenderSession, harvest_page
from colorsense.models import ComponentType, Harvest, Theme, Viewport
from colorsense.palette.inventory import build_inventory

VIEWPORT = Viewport(width=1280, height=800, device_scale_factor=1.0)

# Every test here drives a real Chromium render; skip in browserless CI.
pytestmark = pytest.mark.browser


@pytest.fixture(scope="module")
def config() -> Config:
    """The real palette config (vendor prefixes drive vendor_match)."""
    return load_default_config()


async def _harvest(fixture: Path, config: Config, theme: Theme = Theme.LIGHT) -> Harvest:
    return await harvest_page(fixture.as_uri(), theme, config, VIEWPORT)


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------


async def test_tokens_extracted_and_alias_graph(fixtures_dir: Path, config: Config) -> None:
    harvest = await _harvest(fixtures_dir / "tokens.html", config)

    by_name = {token.name: token for token in harvest.tokens}

    # Declared custom properties appear.
    assert "--color-primary" in by_name
    assert "--accent" in by_name
    assert "--btn-bg" in by_name

    # Resolved colors parse for color-valued tokens.
    assert by_name["--color-primary"].resolved is not None
    assert by_name["--accent"].resolved is not None
    assert by_name["--accent"].resolved is not None
    assert by_name["--accent"].resolved.hex == "#e91e63"

    # The non-color token resolves to None.
    assert by_name["--not-a-color"].resolved is None

    # Alias graph: --btn-bg: var(--accent) -> alias_target carries the leading '--'.
    assert by_name["--btn-bg"].alias_target == "--accent"

    # The dark @media block is captured with its media text on at least one record.
    dark_records = [t for t in harvest.tokens if t.media is not None and "dark" in t.media]
    assert dark_records, "expected at least one token declared under prefers-color-scheme: dark"


# ---------------------------------------------------------------------------
# Elements
# ---------------------------------------------------------------------------


async def test_elements_extracted_with_colors_and_exclusions(
    fixtures_dir: Path, config: Config
) -> None:
    harvest = await _harvest(fixtures_dir / "tokens.html", config)

    classes = {tuple(el.class_tokens) for el in harvest.elements}
    assert ("primary-box",) in classes
    assert ("btn",) in classes

    primary = next(el for el in harvest.elements if "primary-box" in el.class_tokens)
    assert primary.bg is not None
    assert primary.bg.hex == "#3366cc"
    assert primary.text is not None
    assert primary.text.hex == "#ffffff"

    # Hidden (display:none) and zero-area elements are excluded.
    for el in harvest.elements:
        assert "hidden-box" not in el.class_tokens
        assert "zero-box" not in el.class_tokens

    # Every returned element is visible and not aria-hidden.
    assert all(el.visible and not el.aria_hidden for el in harvest.elements)


async def test_border_only_reported_when_painted(fixtures_dir: Path, config: Config) -> None:
    """``border`` is width-gated: only elements that paint a border carry a color.

    Regression: ungated computed ``borderTopColor`` resolves for every element, which used
    to make ``border`` non-None (usually black) on borderless elements.
    """
    harvest = await _harvest(fixtures_dir / "tokens.html", config)

    btn = next(el for el in harvest.elements if "btn" in el.class_tokens)
    assert btn.border is not None
    assert btn.border.hex == "#aa0044"

    primary = next(el for el in harvest.elements if "primary-box" in el.class_tokens)
    assert primary.border is None
    assert primary.has_box_shadow is False


async def test_min_corner_radius_harvested_and_pill_chips_classify_as_badge(
    fixtures_dir: Path, config: Config
) -> None:
    """End-to-end (disconetwork.com shape): pill chips harvest a min radius and are badges.

    Confirms the harvest populates ``min_corner_radius`` from the live computed style and
    that the classifier routes the fully-rounded, short, text-bearing chips to ``badge``
    rather than ``card_bg`` (they repeat with a ring + tinted bg, which would otherwise
    trip the card detector). The square-cornered cards harvest a tiny radius; the
    one-corner-rounded tab harvests ``min_corner_radius == 0`` (the MIN reducer rejects a
    single rounded corner), so neither classifies as a badge.
    """
    harvest = await _harvest(fixtures_dir / "badge_chips.html", config)

    chips = [el for el in harvest.elements if "chip" in el.class_tokens]
    assert len(chips) == 4
    for chip in chips:
        # rounded-full resolves to a large px radius, well over half the 28px height.
        assert chip.min_corner_radius >= chip.rect.height / 2.0

    card = next(el for el in harvest.elements if "card" in el.class_tokens)
    assert card.min_corner_radius < card.rect.height / 2.0  # 8px vs 200px

    # Only the top-left corner is rounded, so the MIN of the four corners is 0.
    tab = next(el for el in harvest.elements if "tab" in el.class_tokens)
    assert tab.min_corner_radius == 0.0

    classified = classify_components(harvest.elements, config, VIEWPORT)
    by_element = {id(c.element): c for c in classified}
    for chip in chips:
        dist = by_element[id(chip)].component_dist
        assert max(dist, key=lambda comp: dist[comp]) is ComponentType.BADGE
        assert ComponentType.CARD_BG not in dist

    tab_dist = by_element[id(tab)].component_dist
    assert ComponentType.BADGE not in tab_dist


async def test_gradient_cta_stops_harvested_and_vote_both_brand_colors(
    fixtures_dir: Path, config: Config
) -> None:
    """End-to-end (disconetwork.com shape): a clickable gradient pill harvests its stops.

    The brand button is a clickable pill painting a ``linear-gradient(purple, blue)`` over
    a transparent ``background-color``; the harvester must capture both stops in
    ``bg_gradient_stops`` so the button's brand colors are not invisible, and the inventory
    must attribute its interactive mass to both purple and blue. The negatives pin the
    interactive-pill gate: a clickable gradient *card* (a rounded rectangle, not a pill —
    a stripe.com-style decorative card) and a *non-clickable* gradient pill (a divider)
    both yield no stops, as do a solid background-color and a decorative transparent-fading
    glow.
    """
    harvest = await _harvest(fixtures_dir / "gradient_cta.html", config)

    cta = next(el for el in harvest.elements if "cta" in el.class_tokens)
    stops = {c.hex for c in cta.bg_gradient_stops}
    assert stops == {"#7c3bed", "#3c83f6"}
    assert cta.bg is not None and cta.bg.alpha == 0.0  # gradient is the only fill

    # A clickable but rectangular (non-pill) gradient card is decorative -> no stops.
    card = next(el for el in harvest.elements if "card" in el.class_tokens)
    assert card.bg_gradient_stops == ()

    # A pill-shaped but NON-clickable gradient divider -> no stops.
    divider = next(el for el in harvest.elements if "divider" in el.class_tokens)
    assert divider.bg_gradient_stops == ()

    # An opaque background-color wins: its gradient is not harvested as a fill.
    solid = next(el for el in harvest.elements if "solid" in el.class_tokens)
    assert solid.bg_gradient_stops == ()

    # A decorative glow (fades to rgba(0,0,0,0)) is not a fill.
    glow = next(el for el in harvest.elements if "glow" in el.class_tokens)
    assert glow.bg_gradient_stops == ()

    classified = classify_components(harvest.elements, config, VIEWPORT)
    clusters = build_inventory(harvest, classified)
    by_hex = {c.color.hex: c for c in clusters}
    # Both brand stops carry the CTA's interactive (cta_bg) mass.
    assert ComponentType.CTA_BG in by_hex["#7c3bed"].component_mass
    assert ComponentType.CTA_BG in by_hex["#3c83f6"].component_mass


async def test_has_text_set_for_direct_text_only(fixtures_dir: Path, config: Config) -> None:
    """``has_text`` is true iff the element has a DIRECT non-whitespace child text node.

    Descendant text must not count — ``<body>`` contains text-bearing children but only
    whitespace text nodes of its own, so it carries ``has_text=False`` while the leaf
    elements carry ``True``.
    """
    harvest = await _harvest(fixtures_dir / "tokens.html", config)

    primary = next(el for el in harvest.elements if "primary-box" in el.class_tokens)
    assert primary.has_text is True  # <div class="primary-box">Primary</div>
    btn = next(el for el in harvest.elements if "btn" in el.class_tokens)
    assert btn.has_text is True

    body = next(el for el in harvest.elements if el.tag == "body")
    assert body.has_text is False  # only whitespace text nodes between children
    html = next(el for el in harvest.elements if el.tag == "html")
    assert html.has_text is False


async def test_input_type_harvested(fixtures_dir: Path, config: Config) -> None:
    """``input_type`` carries the lowercased ``type`` attribute for inputs only.

    ``None`` means "not an input, or no ``type`` attribute declared" — the untyped
    input deliberately reports ``None`` rather than the HTML default ``text``.
    The fixture declares ``type="TEXT"`` uppercase to pin the lowercasing.
    """
    harvest = await _harvest(fixtures_dir / "inputs.html", config)
    by_id = {el.id: el for el in harvest.elements if el.id is not None}

    assert by_id["submit-btn"].input_type == "submit"
    assert by_id["search-box"].input_type == "text"
    assert by_id["untyped-box"].input_type is None
    assert by_id["real-btn"].input_type is None  # non-input elements never carry a type

    # clickable stays gated: the submit input is clickable, a plain text input is not,
    # and a cursor:pointer text input is clickable yet still reports its real type.
    assert by_id["submit-btn"].clickable is True
    assert by_id["search-box"].clickable is False
    assert by_id["pointer-box"].clickable is True
    assert by_id["pointer-box"].input_type == "text"


async def test_vendor_match_detected(fixtures_dir: Path, config: Config) -> None:
    harvest = await _harvest(fixtures_dir / "tokens.html", config)
    intercom = next(el for el in harvest.elements if "intercom-launcher" in el.class_tokens)
    assert intercom.vendor_match is True


# ---------------------------------------------------------------------------
# Hover states
# ---------------------------------------------------------------------------


async def test_hover_color_change_detected(fixtures_dir: Path, config: Config) -> None:
    harvest = await _harvest(fixtures_dir / "hover.html", config)

    cta = next(el for el in harvest.elements if el.id == "cta")
    assert cta.clickable is True
    assert cta.has_hover_color_change is True
    assert cta.hover_bg is not None
    assert cta.hover_bg.hex == "#ff6600"

    # A clickable button with no :hover rule does not report a change.
    plain = next(el for el in harvest.elements if el.id == "plain")
    assert plain.has_hover_color_change is False


# ---------------------------------------------------------------------------
# Screenshot bins + consent masking
# ---------------------------------------------------------------------------


async def test_screenshot_bins_valid(fixtures_dir: Path, config: Config) -> None:
    harvest = await _harvest(fixtures_dir / "tokens.html", config)

    assert harvest.screenshot_bins, "expected non-empty screenshot bins"
    for bin_ in harvest.screenshot_bins:
        assert 0.0 <= bin_.area_fraction <= 1.0
    total = sum(b.area_fraction for b in harvest.screenshot_bins)
    assert total <= 1.0 + 1e-6


async def test_consent_region_masked(fixtures_dir: Path, config: Config) -> None:
    harvest = await _harvest(fixtures_dir / "consent.html", config)

    # The banner detector found the fixed full-width cookie banner.
    # The banner's unique magenta (#ff00aa) must not dominate the bins after masking.
    banner_hex = "#ff00aa"
    banner_fraction = sum(
        b.area_fraction for b in harvest.screenshot_bins if b.color.hex == banner_hex
    )
    # Masking should strongly suppress the banner color (it covered ~18% unmasked).
    assert banner_fraction < 0.02, (
        f"banner color not masked: fraction={banner_fraction}, "
        f"bins={[(b.color.hex, round(b.area_fraction, 3)) for b in harvest.screenshot_bins]}"
    )

    # White page background should dominate instead.
    assert harvest.screenshot_bins
    assert harvest.screenshot_bins[0].color.hex in {"#ffffff", "#fefefe"}


async def test_media_region_masked_gradient_and_svg_kept(
    fixtures_dir: Path, config: Config
) -> None:
    # media_mask.html stacks three full-width 600px bands of equal area:
    #   (a) a url() background photo in magenta #ff00aa  -> MUST be masked (raster media),
    #   (b) a CSS linear-gradient in blue                -> MUST be kept (no url() token),
    #   (c) an inline <svg> filled green #00cc00         -> MUST be kept (vector content).
    # Masking must suppress the photo's magenta while the gradient blue and the svg green
    # both remain — the whole point of excluding photography without eating design colors.
    harvest = await _harvest(fixtures_dir / "media_mask.html", config)
    assert harvest.screenshot_bins

    def fraction_near(target: tuple[int, int, int], tol: int = 24) -> float:
        # Quantization nudges exact hexes; sum bins whose RGB is within ``tol`` per channel.
        total = 0.0
        for b in harvest.screenshot_bins:
            r = int(b.color.hex[1:3], 16)
            g = int(b.color.hex[3:5], 16)
            bl = int(b.color.hex[5:7], 16)
            near = all(abs(c - t) <= tol for c, t in zip((r, g, bl), target, strict=True))
            if near:
                total += b.area_fraction
        return total

    photo = fraction_near((255, 0, 170))
    gradient = fraction_near((0, 0, 220))  # the gradient's blue stops (#0000ff..#0000cc)
    svg = fraction_near((0, 204, 0))

    # The photo band covered ~1/3 of the page unmasked; masking drives it to near-zero.
    assert photo < 0.02, (
        f"photo color not masked: fraction={photo}, "
        f"bins={[(b.color.hex, round(b.area_fraction, 3)) for b in harvest.screenshot_bins]}"
    )
    # The gradient and inline-svg design colors must survive masking.
    assert gradient > 0.1, f"gradient color wrongly suppressed: fraction={gradient}"
    assert svg > 0.1, f"svg color wrongly suppressed: fraction={svg}"


# ---------------------------------------------------------------------------
# RenderSession contract
# ---------------------------------------------------------------------------


async def test_request_filter_blocks_subresource_but_page_still_harvests(
    fixtures_dir: Path,
) -> None:
    # subresource.html links an external stylesheet that paints the body red. A filter that
    # blocks the stylesheet URL must abort that one request (the inline white background
    # survives) while the navigation itself still renders and is harvestable.
    page_url = (fixtures_dir / "subresource.html").as_uri()

    def block_asset(url: str) -> bool:
        return not url.endswith("subresource_asset.css")

    async with RenderSession(Theme.LIGHT, VIEWPORT, request_filter=block_asset) as session:
        await session.goto(page_url)
        bg = await session.page.evaluate("() => getComputedStyle(document.body).backgroundColor")
    assert bg == "rgb(255, 255, 255)"  # the blocked stylesheet never painted the body red


async def test_no_request_filter_lets_subresource_load(fixtures_dir: Path) -> None:
    # Control for the blocking test above: without a filter the stylesheet loads and paints
    # the body red — proving the blocked-case assertion is meaningful.
    page_url = (fixtures_dir / "subresource.html").as_uri()
    async with RenderSession(Theme.LIGHT, VIEWPORT) as session:
        await session.goto(page_url)
        bg = await session.page.evaluate("() => getComputedStyle(document.body).backgroundColor")
    assert bg == "rgb(255, 0, 0)"


async def test_websocket_refused_when_request_filter_installed(fixtures_dir: Path) -> None:
    # websocket.html attempts `new WebSocket('ws://127.0.0.1:1/')` and records every
    # lifecycle event. WebSocket handshakes bypass context.route, so the egress gate
    # refuses them outright via a route_web_socket handler whenever a request_filter is
    # configured — even a permissive one: the HTTP filter below allows everything, proving
    # it is the refusal route (not the filter) that kills the socket. The page-side socket
    # must never open, and the render must still complete and be harvestable.
    page_url = (fixtures_dir / "websocket.html").as_uri()

    def allow_all_http(_url: str) -> bool:
        return True

    async with RenderSession(Theme.LIGHT, VIEWPORT, request_filter=allow_all_http) as session:
        await session.goto(page_url)
        await session.page.wait_for_function("() => window.__wsDone", timeout=5000)
        events = await session.page.evaluate("() => window.__wsEvents")
        bg = await session.page.evaluate("() => getComputedStyle(document.body).backgroundColor")

    assert "open" not in events  # the socket never opened: no handshake ever went out
    assert "close" in events  # the page observed a dead (closed) socket, nothing more
    assert bg == "rgb(255, 255, 255)"  # the page itself rendered fine for harvesting


async def test_websocket_fixture_harvests_under_request_filter(
    fixtures_dir: Path, config: Config
) -> None:
    # End-to-end arm of the test above: a full harvest_page over the WS-attempting fixture
    # completes normally with a request_filter installed (the dead socket is harmless).
    harvest = await harvest_page(
        (fixtures_dir / "websocket.html").as_uri(),
        Theme.LIGHT,
        config,
        VIEWPORT,
        request_filter=lambda _url: True,
    )
    assert harvest.elements  # the page rendered and was walked despite the refused socket


async def test_render_session_exposes_page_and_consent(fixtures_dir: Path) -> None:
    async with RenderSession(Theme.LIGHT, VIEWPORT) as session:
        await session.goto((fixtures_dir / "consent.html").as_uri())
        # The page is exposed for module JS.
        title = await session.page.title()
        assert title == "Consent fixture"
        # A consent banner region was detected for masking.
        assert session.consent_rects, "expected a detected consent region"


async def test_render_session_exposes_media_rects(fixtures_dir: Path) -> None:
    async with RenderSession(Theme.LIGHT, VIEWPORT) as session:
        await session.goto((fixtures_dir / "media_mask.html").as_uri())
        # The url()-background photo (and only it) was detected as maskable raster media;
        # the gradient and inline <svg> bands must NOT contribute rects.
        assert session.media_rects, "expected a detected raster-media region"
        assert len(session.media_rects) == 1, (
            f"only the url() photo should be masked, got {len(session.media_rects)} rects"
        )


# ---------------------------------------------------------------------------
# Release-review hardening: payload caps, selector uniqueness, adopted sheets
# ---------------------------------------------------------------------------


async def test_adopted_stylesheet_tokens_harvested(fixtures_dir: Path, config: Config) -> None:
    # document.adoptedStyleSheets is not part of document.styleSheets; constructed
    # sheets (the web-component design-system pattern) must still surface tokens.
    harvest = await _harvest(fixtures_dir / "harvest_hardening.html", config)

    by_name = {token.name: token for token in harvest.tokens}
    assert "--plain-token" in by_name  # regular <style> sheet still covered
    adopted = by_name.get("--adopted-token")
    assert adopted is not None
    assert adopted.resolved is not None and adopted.resolved.hex == "#112233"


async def test_duplicate_id_selectors_resolve_uniquely(fixtures_dir: Path) -> None:
    # Selectors must match EXACTLY the element they were built for: bare '#dup' would
    # make the hover prober read the first duplicate for both.
    from colorsense.harvest.dom import harvest_elements

    fixture = fixtures_dir / "harvest_hardening.html"
    async with RenderSession(Theme.LIGHT, VIEWPORT) as session:
        await session.goto(fixture.as_uri())
        _elements, selectors = await harvest_elements(session.page, [])
        assert "#dup" not in selectors
        # A unique id still uses the fast '#id' form.
        assert "#unique-btn" in selectors
        for selector in selectors:
            if not selector:
                continue  # deliberately skipped (pathological nesting)
            count = await session.page.evaluate(
                "(sel) => document.querySelectorAll(sel).length", selector
            )
            assert count == 1, f"selector {selector!r} matches {count} elements"


async def test_element_payload_cap_keeps_largest_area(fixtures_dir: Path) -> None:
    # The collection JS receives the cap as an argument; evaluating with a tiny cap
    # exercises the truncation exactly as the shipped cap would on a hostile page.
    from colorsense.harvest.dom import _COLLECT_DOM_JS

    fixture = fixtures_dir / "harvest_hardening.html"
    async with RenderSession(Theme.LIGHT, VIEWPORT) as session:
        await session.goto(fixture.as_uri())
        uncapped = await session.page.evaluate(_COLLECT_DOM_JS, 10_000)
        assert len(uncapped) > 3
        capped = await session.page.evaluate(_COLLECT_DOM_JS, 3)
        assert len(capped) == 3
        # The largest-area records survive (body and the 600x400 block among them),
        # in document order.
        kept_tags_classes = [(rec["tag"], rec["class_tokens"]) for rec in capped]
        assert ("body", []) in kept_tags_classes
        assert ("div", ["big"]) in kept_tags_classes
        areas = [rec["rect"]["w"] * rec["rect"]["h"] for rec in capped]
        dropped_areas = [
            rec["rect"]["w"] * rec["rect"]["h"]
            for rec in uncapped
            if (rec["tag"], rec["class_tokens"]) not in kept_tags_classes
        ]
        assert min(areas) >= max(dropped_areas)


async def test_token_payload_cap_stops_collection(fixtures_dir: Path) -> None:
    from colorsense.harvest.tokens import _COLLECT_TOKENS_JS

    fixture = fixtures_dir / "harvest_hardening.html"
    async with RenderSession(Theme.LIGHT, VIEWPORT) as session:
        await session.goto(fixture.as_uri())
        uncapped = await session.page.evaluate(_COLLECT_TOKENS_JS, 5_000)
        assert len(uncapped) >= 3  # two declared + one adopted
        capped = await session.page.evaluate(_COLLECT_TOKENS_JS, 2)
        assert len(capped) == 2
