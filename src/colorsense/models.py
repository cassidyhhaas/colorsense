"""Shared contracts for the colorsense pipeline.

These models are the single shared-contract surface for the pipeline. This file is
**frozen** by design: downstream code must not modify it. A change to a contract here
must be made centrally and re-validated against every dependent module, never patched
locally by a consumer.

Value objects (``Color``, ``BoundingBox``, ``Viewport``) are immutable. The **public result tree**
reachable from [`AnalysisResult`][colorsense.AnalysisResult] is also immutable: every output model
is ``frozen=True`` and its sequence fields are ``tuple`` (not ``list``), so neither attribute
reassignment nor in-place ``.append()`` works. ``dict`` fields stay regular dicts — ``frozen``
blocks reassigning them, but their contents are intentionally not deep-frozen (deep-freezing needs
exotic types and breaks JSON round-trip). Tuples serialize to JSON arrays and re-parse into tuples,
so ``model_dump_json()`` / ``model_validate_json()`` round-trips cleanly.

The **internal-only** assembly models (``Harvest``, ``HarvestedElement``,
``ScreenshotBin``, ``ClassifiedElement``, ``ColorCluster``) remain mutable: they are
scratch structures the pipeline mutates while building the result and never escape to the
caller. The frozen classification scratch types (``TokenRecord``, ``ClassifiedToken``,
``TokenOrigin``) are likewise internal: consumers see declared tokens only through the
public [`DesignToken`][colorsense.DesignToken]. Along with the internal value type ``BoundingBox``,
they are deliberately excluded from ``__all__``. ``ComponentType`` is **public**: it keys the
fine-grained component evidence in the result tree (both
[`Usage`][colorsense.Usage]``.components`` on the color-keyed index and
[`UsageEntry`][colorsense.UsageEntry]``.components`` on the role-keyed projection).

The **public contract** types are enumerated in ``__all__`` below and re-exported from the
top-level `colorsense` package, which is the canonical import path for consumers.
``colorsense.models`` is their documented home, but importing from the package root
(``from colorsense import ...``) is preferred.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "AnalysisResult",
    "Color",
    "ColorUsage",
    "ComponentType",
    "DesignToken",
    "DivergenceItem",
    "PropertyFamily",
    "RunMetadata",
    "Theme",
    "ThemePalette",
    "TokenSemanticRole",
    "Usage",
    "UsageEntry",
    "UsagePalette",
    "UsageRole",
    "Viewport",
]

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class PropertyFamily(StrEnum):
    """Which family of CSS properties paints the color — the rollup axis over roles.

    Coarser than [`UsageRole`][colorsense.UsageRole]: every role belongs to exactly one
    family (the mapping is [`UsageRole.property_family`][colorsense.UsageRole.property_family]).

    Attributes:
        BACKGROUND: ``background-color`` / ``background-image`` fills.
        TEXT: The ``color`` property (typography).
        BORDER: The ``border-color`` property.

    """

    BACKGROUND = "background"
    TEXT = "text"
    BORDER = "border"


class UsageRole(StrEnum):
    """Developer-facing usage role — how a rendered color is used on the page.

    The role taxonomy keys the role-keyed palette projection
    ([`UsagePalette`][colorsense.UsagePalette]) and labels each
    [`Usage`][colorsense.Usage] slot of the color-keyed index. It splits the two axes the
    old 4-value usage taxonomy conflated — *which CSS property paints the color* (the
    [`PropertyFamily`][colorsense.PropertyFamily] rollup) versus *what kind of element it
    is* — so that, e.g., link text and CTA button backgrounds no longer share one slot.

    Attributes:
        PAGE: The base canvas (the page background).
        SURFACE: Raised content backgrounds: cards, modals, hero, inputs.
        BANNER: Chrome-bar backgrounds: header, nav, footer.
        CTA: The primary action background (CTA buttons).
        ACTION: Secondary action backgrounds: secondary buttons, badges.
        TEXT: Body/heading typography at every layer.
        LINK: Link color (typography of anchors).
        BORDER: Borders and dividers.

    """

    PAGE = "page"
    SURFACE = "surface"
    BANNER = "banner"
    CTA = "cta"
    ACTION = "action"
    TEXT = "text"
    LINK = "link"
    BORDER = "border"

    @property
    def property_family(self) -> PropertyFamily:
        """The [`PropertyFamily`][colorsense.PropertyFamily] this usage role rolls up to.

        A fixed code-level convention, the role-side twin of
        [`ComponentType.property_family`][colorsense.ComponentType.property_family]: ``text``
        and ``link`` are painted by the element's ``color`` (the ``text`` family), ``border``
        by its ``border-color``, and every other role
        (``page``/``surface``/``banner``/``cta``/``action``) by a background property. ``link``
        is a text role even though it names an interactive element, because a link's painted
        color is its typography color, not its (usually transparent) background.

        Returns:
            [`PropertyFamily.TEXT`][colorsense.PropertyFamily] for ``text``/``link``,
            [`PropertyFamily.BORDER`][colorsense.PropertyFamily] for ``border``, and
            [`PropertyFamily.BACKGROUND`][colorsense.PropertyFamily] for every other role.

        """
        if self in (UsageRole.TEXT, UsageRole.LINK):
            return PropertyFamily.TEXT
        if self is UsageRole.BORDER:
            return PropertyFamily.BORDER
        return PropertyFamily.BACKGROUND


class TokenSemanticRole(StrEnum):
    """Semantic role inferred for a declared design token (CSS custom property).

    Attributes:
        BRAND_PRIMARY: Primary brand color.
        BRAND_SECONDARY: Secondary brand color.
        BRAND_ACCENT: Accent brand color.
        INTERACTIVE: Interactive-element color (links, controls).
        SURFACE_BASE: Base page/surface background.
        SURFACE_RAISED: Raised surface background (cards, modals).
        TEXT_BODY: Body text color.
        NEUTRAL: Neutral/gray with no specific role.
        BORDER: Border/divider color.
        TEXT_ON: Foreground meant to sit on a colored fill (an "on" color).
        STATUS: Status color (success/warning/error/info).
        IGNORE: Not a meaningful palette token (filtered out).

    """

    BRAND_PRIMARY = "brand_primary"
    BRAND_SECONDARY = "brand_secondary"
    BRAND_ACCENT = "brand_accent"
    INTERACTIVE = "interactive"
    SURFACE_BASE = "surface_base"
    SURFACE_RAISED = "surface_raised"
    TEXT_BODY = "text_body"
    NEUTRAL = "neutral"
    BORDER = "border"
    TEXT_ON = "text_on"
    STATUS = "status"
    IGNORE = "ignore"


class ComponentType(StrEnum):
    """Visual component a rendered element belongs to (source of a measured color).

    Public: keys the fine-grained component evidence in the result tree — both
    [`Usage`][colorsense.Usage]``.components`` on the color-keyed index and
    [`UsageEntry`][colorsense.UsageEntry]``.components`` on the role-keyed projection —
    naming which component types contributed a color to a usage role.

    Attributes:
        PAGE_BG: Page background.
        PAGE_TEXT: Page body text.
        HEADER_BG: Header background.
        HEADER_TEXT: Header text.
        NAV_BG: Nav background.
        NAV_TEXT: Nav text.
        FOOTER_BG: Footer background.
        FOOTER_TEXT: Footer text.
        HERO_BG: Hero background.
        HERO_TEXT: Hero text.
        CARD_BG: Card background.
        CARD_TEXT: Card text.
        CTA_BG: Primary call-to-action button background.
        CTA_TEXT: Primary call-to-action button text/label.
        LINK: Hyperlink text color.
        BUTTON_SECONDARY: Secondary button background.
        MODAL_BG: Modal/dialog background.
        INPUT_BG: Form input background.
        BORDER: Border/divider color.
        BADGE: Badge/chip/pill background.
        THIRD_PARTY: Color from an embedded third-party widget.

    """

    PAGE_BG = "page_bg"
    PAGE_TEXT = "page_text"
    HEADER_BG = "header_bg"
    HEADER_TEXT = "header_text"
    NAV_BG = "nav_bg"
    NAV_TEXT = "nav_text"
    FOOTER_BG = "footer_bg"
    FOOTER_TEXT = "footer_text"
    HERO_BG = "hero_bg"
    HERO_TEXT = "hero_text"
    CARD_BG = "card_bg"
    CARD_TEXT = "card_text"
    CTA_BG = "cta_bg"
    CTA_TEXT = "cta_text"
    LINK = "link"
    BUTTON_SECONDARY = "button_secondary"
    MODAL_BG = "modal_bg"
    INPUT_BG = "input_bg"
    BORDER = "border"
    BADGE = "badge"
    THIRD_PARTY = "third_party"

    @property
    def property_family(self) -> PropertyFamily:
        """The [`PropertyFamily`][colorsense.PropertyFamily] this component's vote mass routes to.

        The routing convention is fixed in code, the component-side twin of
        [`UsageRole.property_family`][colorsense.UsageRole.property_family]: components whose
        value ends with ``_text`` — plus ``link``, whose painted color is its typography color,
        not its (usually transparent) background — are painted by the element's ``color`` (the
        ``text`` family), ``border`` by its ``border-color``, and everything else (including
        ``badge``, ``third_party`` and ``button_secondary``) by its ``background-color`` (the
        ``background`` family).

        This is the single source of truth for component routing, shared by the component
        classifier's per-family normalization (``classify/components.py``) and the inventory's
        per-family attribution (``palette/inventory.py``). The two partitions MUST stay
        identical, so both read this one property — it lives here in the shared-contracts module
        so neither importer creates a cross-layer dependency.

        Returns:
            [`PropertyFamily.TEXT`][colorsense.PropertyFamily] for ``*_text`` components and
            ``link``, [`PropertyFamily.BORDER`][colorsense.PropertyFamily] for ``border``, and
            [`PropertyFamily.BACKGROUND`][colorsense.PropertyFamily] for every other component.

        """
        if self.value.endswith("_text") or self is ComponentType.LINK:
            return PropertyFamily.TEXT
        if self is ComponentType.BORDER:
            return PropertyFamily.BORDER
        return PropertyFamily.BACKGROUND


def is_pill_shape(width: float, height: float, min_corner_radius: float) -> bool:
    """Whether a box is a fully-rounded, elongated pill/stadium shape.

    True iff all four corners are fully rounded (``min_corner_radius >= height/2``) AND the
    box is wider than tall (``width > height``, which excludes circles where
    ``width == height``). Size-agnostic — it tests shape only, never absolute dimensions.

    This is the single source of truth for the stadium-shape test, shared by the harvester
    (``harvest/dom.py``, gating which gradient fills track the brand palette) and the
    component classifier (``classify/components.py``, the badge/card-exclusion rule). It
    lives here in the shared-contracts module — like the ``property_family`` routing — so
    neither layer has to import the other (``harvest`` and ``classify`` must not depend on each
    other) and the two cannot drift out of sync.

    Args:
        width: Box width in CSS pixels.
        height: Box height in CSS pixels.
        min_corner_radius: Smallest of the four computed corner radii in CSS pixels.

    Returns:
        ``True`` if the box is a fully-rounded pill (excluding circles), else ``False``.

    """
    return height > 0.0 and min_corner_radius >= height / 2.0 and width > height


def is_circle_shape(width: float, height: float, min_corner_radius: float) -> bool:
    """Whether a box is a fully-rounded **circle/dot** (``rounded-full`` with ``width == height``).

    The square counterpart to `is_pill_shape`: all four corners fully rounded
    (``min_corner_radius >= height/2``) AND the box is (within a 1px tolerance) square.
    `is_pill_shape` deliberately *excludes* circles (it demands ``width > height``); this is
    the predicate for that excluded case. Size-agnostic — it tests shape only. The badge
    rule and the card-detector exclusion layer the absolute size/clickable/recurrence gates
    on top, so the bare shape predicate stays reusable.

    Lives here in the shared-contracts module alongside `is_pill_shape` so the harvester and
    the component classifier share one definition and cannot drift out of sync. The 1px
    tolerance absorbs sub-pixel layout rounding (a ``size-2.5`` dot computes to 10.0x10.0,
    but fractional widths occur) without admitting plainly non-square rects.

    Args:
        width: Box width in CSS pixels.
        height: Box height in CSS pixels.
        min_corner_radius: Smallest of the four computed corner radii in CSS pixels.

    Returns:
        ``True`` if the box is a fully-rounded square circle/dot (within a 1px tolerance),
        else ``False``.

    """
    return height > 0.0 and min_corner_radius >= height / 2.0 and abs(width - height) <= 1.0


class Theme(StrEnum):
    """Color scheme a site is rendered under.

    Attributes:
        LIGHT: Light color scheme (the default).
        DARK: Dark color scheme (``prefers-color-scheme: dark``).

    """

    LIGHT = "light"
    DARK = "dark"


# ---------------------------------------------------------------------------
# Value models (immutable)
# ---------------------------------------------------------------------------


class Color(BaseModel):
    """An sRGB color with cached OKLCH coordinates.

    ``hex`` is always the *opaque* normalized lowercase 7-char sRGB hex string
    (``#rrggbb``) — alpha is carried separately in ``alpha`` and never encoded in the
    hex (the invariant ``color/primitives.py`` establishes; fixed-length hexes are also
    what keeps lexicographic tie-breaks well-defined). ``lightness``/``chroma``/``hue``
    are the OKLCH coordinates of the (composited) color; ``alpha`` is the source alpha.
    """

    model_config = ConfigDict(frozen=True)

    hex: str = Field(
        description=(
            "Opaque normalized lowercase 7-char sRGB hex (``#rrggbb``); alpha is never "
            "encoded here (it lives in ``alpha``)."
        ),
        pattern=r"^#[0-9a-f]{6}$",
        examples=["#1f6feb", "#ffffff", "#0d1117"],
    )
    lightness: float = Field(
        description="OKLCH lightness of the (composited) color; 0.0 is black, 1.0 is white.",
        ge=0.0,
        le=1.0,
        examples=[0.62],
    )
    chroma: float = Field(
        description=(
            "OKLCH chroma (saturation) of the (composited) color; 0.0 is achromatic. "
            "Unbounded above, though sRGB colors rarely exceed ~0.4."
        ),
        ge=0.0,
        examples=[0.18],
    )
    hue: float = Field(
        description=(
            "OKLCH hue angle in degrees; achromatic colors are normalized to 0.0 (never NaN)."
        ),
        ge=0.0,
        lt=360.0,
        examples=[256.3],
    )
    alpha: float = Field(
        default=1.0,
        description="Source alpha: 0.0 is fully transparent, 1.0 is fully opaque.",
        ge=0.0,
        le=1.0,
        examples=[1.0],
    )


class BoundingBox(BaseModel):
    """Axis-aligned bounding box in CSS pixels."""

    model_config = ConfigDict(frozen=True)

    x: float = Field(
        description=(
            "Left edge in CSS pixels, relative to the document origin; may be negative for "
            "elements scrolled or positioned off the left of the viewport."
        ),
        examples=[0.0],
    )
    y: float = Field(
        description=(
            "Top edge in CSS pixels, relative to the document origin; may be negative for "
            "elements above the viewport."
        ),
        examples=[120.0],
    )
    width: float = Field(
        description=(
            "Box width in CSS pixels. Harvested element boxes are non-negative (zero-area "
            "elements are filtered), but externally-supplied mask boxes may be degenerate, so "
            "no lower bound is enforced here."
        ),
        examples=[1280.0],
    )
    height: float = Field(
        description="Box height in CSS pixels (degenerate mask boxes may be non-positive).",
        examples=[64.0],
    )


class Viewport(BaseModel):
    """Rendering viewport."""

    model_config = ConfigDict(frozen=True)

    width: int = Field(
        description="Viewport width in CSS pixels.",
        ge=1,
        examples=[1280],
    )
    height: int = Field(
        description="Viewport height in CSS pixels.",
        ge=1,
        examples=[800],
    )
    device_scale_factor: float = Field(
        description="Device pixel ratio (DIP→raster multiplier); 1.0 is non-retina, 2.0 is retina.",
        gt=0.0,
        examples=[1.0],
    )


# ---------------------------------------------------------------------------
# Harvest models (produced by the harvest stage)
# ---------------------------------------------------------------------------


class TokenRecord(BaseModel):
    """Internal: a declared CSS custom property and its resolved color (if any).

    Not part of the public contract — ``scope``/``media``/``alias_target`` are harvest and
    classification scratch detail. The public projection is [`DesignToken`][colorsense.DesignToken].
    """

    model_config = ConfigDict(frozen=True)

    name: str = Field(
        description="Declared CSS custom property name, including the leading ``--``.",
        examples=["--fgColor-default"],
    )
    raw_value: str = Field(
        description="The property's declared value text, before color resolution.",
        examples=["var(--base-color-neutral-emphasis)", "#1f6feb"],
    )
    resolved: Color | None = Field(
        description="The token's value resolved to a color, or ``None`` if it is not a color.",
    )
    scope: str = Field(
        description="CSS selector the declaration is scoped to (e.g. ``:root``).",
        examples=[":root"],
    )
    media: str | None = Field(
        default=None,
        description=(
            "Media query the declaration sits under, or ``None`` for an unconditional rule."
        ),
        examples=["(prefers-color-scheme: dark)"],
    )
    alias_target: str | None = Field(
        default=None,
        description=(
            "Name of the token this one aliases via ``var(...)``, or ``None`` if the value is "
            "not a bare alias."
        ),
        examples=["--base-color-neutral-emphasis"],
    )


class HarvestedElement(BaseModel):
    """A rendered DOM element and its measured computed colors + structural flags.

    The ``bounding_box`` field also accepts the legacy key ``rect`` on input (with
    ``populate_by_name`` so the canonical name still works in code). This keeps frozen
    harvest corpora captured before the ``rect`` -> ``bounding_box`` rename loadable —
    notably the ``eval/harvests/*.json.gz`` panel, which cannot be regenerated offline.

    ``border`` is the computed border color only when the element actually paints a
    border (border width > 0); borderless elements carry ``None``. ``has_box_shadow``
    defaults to ``False`` (mirroring ``has_hover_color_change``'s harvest-time default)
    so pre-existing constructions remain valid; the DOM harvester always sets it.
    ``has_text`` follows the same precedent (default ``False``, always set by the
    harvester): true iff the element has at least one **direct child** text node with
    non-whitespace content — descendant text does not count, otherwise every ancestor
    wrapper of any text would carry the flag.
    ``input_type`` follows the same precedent (default ``None``, always set by the
    harvester): the lowercased ``type`` attribute when the element is an ``<input>``
    with a non-empty ``type``; ``None`` means "not an input, or no ``type`` attribute
    declared" (the HTML default type is ``text``, but the harvester reports the absence
    rather than inferring it).
    ``min_corner_radius`` follows the same precedent (default ``0.0``, always set by the
    harvester): the smallest of the four computed corner radii in CSS pixels (percentage
    radii resolved against width). ``min_corner_radius >= height/2`` means all four corners
    are fully rounded — the intrinsic signal of a pill/chip (stadium) shape, distinct from
    a card (square-ish corners) and from a one-corner-rounded tab. The component classifier
    uses it (with a ``width > height`` test) to detect badge-shaped elements.
    ``bg_gradient_stops`` (default empty, set by the harvester) carries the opaque color
    stops of a gradient that fills a **clickable pill (a CTA)** — the only place a
    gradient reliably tracks the brand palette. It is populated only when the element is
    a clickable pill, its computed ``background-color`` paints nothing (``alpha == 0``),
    and the ``background-image`` gradient has no fully-transparent stop (the last test
    excludes decorative fades, glow halos, and dot-grid textures, which always fade to
    transparent). It is how a gradient CTA — whose computed ``background-color`` is
    transparent — still contributes its brand colors: each stop is an equal member of the
    fill, so a purple→blue button makes both purple and blue candidates. Gradients on
    card backgrounds are deliberately excluded (decorative flavor that varies page to
    page); empty for those, and for solid-background and no-gradient elements.
    """

    model_config = ConfigDict(populate_by_name=True)

    tag: str = Field(
        description="Lowercased HTML tag name of the element.",
        examples=["div", "a", "button"],
    )
    role: str | None = Field(
        description="The element's ARIA ``role`` attribute, or ``None`` if unset.",
        examples=["button"],
    )
    id: str | None = Field(
        description="The element's ``id`` attribute, or ``None`` if unset.",
    )
    class_tokens: list[str] = Field(
        default_factory=list,
        description="The element's ``class`` attribute split into individual tokens.",
        examples=[["btn", "btn-primary"]],
    )
    bounding_box: BoundingBox = Field(
        validation_alias=AliasChoices("bounding_box", "rect"),
        description="The element's layout rectangle from ``getBoundingClientRect()``.",
    )
    position: str = Field(
        description="Computed CSS ``position`` value.",
        examples=["static", "fixed", "absolute"],
    )
    bg: Color | None = Field(
        description="Computed ``background-color``, or ``None`` if it paints nothing.",
    )
    text: Color | None = Field(
        description="Computed text ``color``, or ``None`` if absent.",
    )
    border: Color | None = Field(
        description=(
            "Computed border color when the element paints a border (width > 0), else ``None``."
        ),
    )
    input_type: str | None = Field(
        default=None,
        description=(
            "Lowercased ``type`` attribute for an ``<input>`` with a non-empty type; ``None`` "
            "for non-inputs and inputs with no declared type."
        ),
        examples=["text", "checkbox"],
    )
    min_corner_radius: float = Field(
        default=0.0,
        description=(
            "Smallest of the four computed corner radii in CSS pixels (percentage radii "
            "resolved against width); ``>= height/2`` signals a fully-rounded pill/chip."
        ),
        ge=0.0,
        examples=[0.0],
    )
    bg_gradient_stops: tuple[Color, ...] = Field(
        default=(),
        description=(
            "Opaque color stops of a gradient that fills a clickable pill (CTA); empty for "
            "every other element."
        ),
    )
    effective_bg: Color | None = Field(
        default=None,
        description=(
            "First fully-opaque background found walking the element and its ancestors to the "
            "document root; ``None`` when no opaque background exists up the chain."
        ),
    )
    effective_bg_from_clickable: bool = Field(
        default=False,
        description=(
            "Whether the ancestor that contributed ``effective_bg`` is itself "
            "clickable/button-styled."
        ),
    )
    has_box_shadow: bool = Field(
        default=False,
        description="Whether the element paints a non-``none`` ``box-shadow``.",
    )
    has_text: bool = Field(
        default=False,
        description=(
            "Whether the element has at least one direct child text node with non-whitespace "
            "content (descendant text does not count)."
        ),
    )
    is_iframe: bool = Field(
        description="Whether the element is an ``<iframe>``.",
    )
    cross_origin: bool = Field(
        description="Whether the element is a cross-origin frame (its contents are not readable).",
    )
    shadow_host: bool = Field(
        description="Whether the element hosts a shadow root.",
    )
    clickable: bool = Field(
        description="Whether the element is interactive (link, button, or button-styled).",
    )
    has_hover_color_change: bool = Field(
        description="Whether hovering the element changes one of its measured colors.",
    )
    hover_bg: Color | None = Field(
        description="Background color measured under hover, or ``None`` if no hover change.",
    )
    vendor_match: bool = Field(
        description="Whether the element matched a known third-party/vendor widget.",
    )
    visible: bool = Field(
        description="Whether the element is visible (rendered and non-zero area).",
    )
    aria_hidden: bool = Field(
        description="Whether the element is hidden from assistive technology (``aria-hidden``).",
    )


class ScreenshotBin(BaseModel):
    """A quantized screenshot color and the fraction of page area it covers."""

    color: Color = Field(
        description="The quantized bin color.",
    )
    area_fraction: float = Field(
        description="Fraction of kept (masked) page area this color covers; bins sum to ~1.0.",
        ge=0.0,
        le=1.0,
        examples=[0.42],
    )


class Harvest(BaseModel):
    """Everything extracted from a single rendered page under one theme."""

    url: str = Field(
        description="The analyzed page URL.",
        examples=["https://example.com/"],
    )
    theme: Theme = Field(
        description="Color scheme the page was rendered under.",
    )
    viewport: Viewport = Field(
        description="Viewport the page was rendered at.",
    )
    tokens: list[TokenRecord] = Field(
        default_factory=list,
        description="Declared CSS custom properties harvested from the page.",
    )
    elements: list[HarvestedElement] = Field(
        default_factory=list,
        description="Visible DOM elements with their measured computed colors.",
    )
    screenshot_bins: list[ScreenshotBin] = Field(
        default_factory=list,
        description="Area-weighted quantized colors from the masked full-page screenshot.",
    )


# ---------------------------------------------------------------------------
# Classification models
# ---------------------------------------------------------------------------


class TokenOrigin(StrEnum):
    """Internal: which classification path produced a `ClassifiedToken`.

    Mirrors the classifier precedence in ``classify.tokens`` (relational > name rule >
    scale > fallback, with alias inheritance). Reconciliation uses it to gate
    declared-but-unused divergence to high-intent tokens (``relational`` / ``name_rule``)
    only — scale members, alias followers, and fallbacks are not author intent signals.

    Attributes:
        RELATIONAL: Inferred from a relation to other tokens (highest intent).
        NAME_RULE: Inferred from the token's name.
        SCALE: A member of a detected color scale.
        ALIAS: Inherited from an aliased token via ``var(...)``.
        FALLBACK: No signal matched (lowest intent).

    """

    RELATIONAL = "relational"
    NAME_RULE = "name_rule"
    SCALE = "scale"
    ALIAS = "alias"
    FALLBACK = "fallback"


class ClassifiedToken(BaseModel):
    """Internal: a token tagged with its semantic role and its usage intent over usage roles.

    Not part of the public contract — consumers see declared tokens only through
    [`DesignToken`][colorsense.DesignToken]. ``weight`` is an internal scoring
    input; ``origin`` records the classification path for divergence gating.
    """

    model_config = ConfigDict(frozen=True)

    record: TokenRecord = Field(
        description="The declared token this classification describes.",
    )
    semantic_role: TokenSemanticRole = Field(
        description="The semantic role inferred for the token.",
    )
    weight: float = Field(
        description="Internal classification scoring weight (relative confidence/intent mass).",
        ge=0.0,
        examples=[1.0],
    )
    usage_intent: dict[UsageRole, float] = Field(
        default_factory=dict,
        description=(
            "Per-[`UsageRole`][colorsense.UsageRole] intent distribution (each value in "
            "``[0, 1]``), the token's expected usage."
        ),
    )
    origin: TokenOrigin = Field(
        default=TokenOrigin.FALLBACK,
        description="Which classification path produced this token, used for divergence gating.",
    )


class ClassifiedElement(BaseModel):
    """A harvested element with a probability distribution over component types."""

    element: HarvestedElement = Field(
        description="The harvested element being classified.",
    )
    component_distribution: dict[ComponentType, float] = Field(
        default_factory=dict,
        description=(
            "Normalized probability distribution over [`ComponentType`][colorsense.ComponentType] "
            "(each value in ``[0, 1]``, summing to ~1.0 when non-empty)."
        ),
    )


# ---------------------------------------------------------------------------
# Palette models
# ---------------------------------------------------------------------------


class ColorCluster(BaseModel):
    """An area-weighted cluster of perceptually-near colors with a component mix.

    ``component_mix`` is the per-cluster *normalized* mix (sums to ~1.0 when non-empty);
    ``component_mass`` is the same sums kept **raw** (un-normalized vote mass).
    Normalization destroys cross-cluster magnitude, which the usage view needs to rank
    colors within a category, so both are carried.
    """

    color: Color = Field(
        description="The cluster's representative color.",
    )
    area_weight: float = Field(
        description="Screenshot area fraction the cluster covers (raw, non-negative).",
        ge=0.0,
        examples=[0.31],
    )
    member_count: int = Field(
        description="Number of perceptually-near colors merged into this cluster.",
        ge=1,
        examples=[12],
    )
    component_mix: dict[ComponentType, float] = Field(
        default_factory=dict,
        description=(
            "Per-cluster normalized component distribution (each value in ``[0, 1]``, "
            "summing to ~1.0 when non-empty)."
        ),
    )
    component_mass: dict[ComponentType, float] = Field(
        default_factory=dict,
        description=(
            "Same component sums kept raw (un-normalized vote mass), preserving cross-cluster "
            "magnitude."
        ),
    )


class EvidenceStream(StrEnum):
    """Which of the four harvest evidence streams contributed to a record.

    Names which stream supplied a [`RoleEvidence`][colorsense.models.RoleEvidence]
    record's measurement, used for a confidence read and for divergence logic.

    Attributes:
        DECLARED: Declared CSS custom properties (design tokens).
        DOM: Computed colors of visible DOM elements.
        HOVER: Hover/focus color changes probed via CDP.
        SCREENSHOT: Area-weighted bins from the masked full-page screenshot.

    """

    DECLARED = "declared"
    DOM = "dom"
    HOVER = "hover"
    SCREENSHOT = "screenshot"


class RoleEvidence(BaseModel):
    """The per-``(canonical color, role)`` evidence record emitted by fusion.

    The central data-model object of the detection-plus-ranking redesign (redesign
    §5.3): one record per ``(color, role)`` pair, preserving the per-instance salience
    distribution rather than collapsing instances to a single summed mass. Detection,
    ranking, and intent corroboration all read these records.

    ``instance_saliences`` carries the per-instance salience sigma_i (each ``= p_role * pi_i``)
    sorted descending, so the peak instance is ``instance_saliences[0]``. The peak-dominant
    aggregation (``palette/salience.py``) consumes them in that order.
    """

    model_config = ConfigDict(frozen=True)

    color: Color = Field(
        description="The canonical color this evidence record describes.",
    )
    role: UsageRole = Field(
        description="The usage role this evidence record describes.",
    )
    instance_saliences: tuple[float, ...] = Field(
        default=(),
        description=(
            "Per-instance salience sigma_i (each ``= p_role * pi_i``), sorted **descending** so "
            "``instance_saliences[0]`` is the peak instance. Every value is non-negative."
        ),
    )
    area: float = Field(
        default=0.0,
        description=(
            "Area-fraction evidence routed to this role (screenshot/element area), used for "
            "surface-role salience and auditing; a viewport fraction in ``[0, 1]``."
        ),
        ge=0.0,
        le=1.0,
        examples=[0.31],
    )
    components: dict[ComponentType, float] = Field(
        default_factory=dict,
        description=(
            "Raw summed component mass that contributed this color to this role: which "
            "[`ComponentType`][colorsense.ComponentType]s routed mass here (un-normalized). The "
            "detection stage normalizes these to ~1.0 per slot for the output models."
        ),
        examples=[{"cta_bg": 1.4}],
    )
    streams: tuple[EvidenceStream, ...] = Field(
        default=(),
        description=(
            "Which evidence streams contributed to this record (sorted and deduped by the caller)."
        ),
    )

    @property
    def peak(self) -> float:
        """The peak (most-prominent) instance salience sigma_(1).

        Returns:
            ``instance_saliences[0]`` when non-empty, else ``0.0``.

        """
        return self.instance_saliences[0] if self.instance_saliences else 0.0

    @model_validator(mode="after")
    def _validate_sorted(self) -> RoleEvidence:
        """Require ``instance_saliences`` to be non-negative and sorted descending.

        Returns:
            This validated model.

        Raises:
            ValueError: If any salience is negative or the sequence is not sorted descending.

        """
        previous: float | None = None
        for value in self.instance_saliences:
            if value < 0.0:
                raise ValueError(f"instance_saliences must be non-negative, got {value}")
            if previous is not None and value > previous:
                raise ValueError(
                    f"instance_saliences must be sorted descending, got {self.instance_saliences!r}"
                )
            previous = value
        return self


class Usage(BaseModel):
    """One usage slot of a color in the color-keyed index ([`ColorUsage`][colorsense.ColorUsage]).

    A color may be used in several roles (e.g. the same gray as ``text`` *and* ``border``);
    each gets its own ``Usage``. ``role`` is the [`UsageRole`][colorsense.UsageRole] this
    slot describes and ``property_family`` is its [`PropertyFamily`][colorsense.PropertyFamily]
    rollup (denormalized — always ``role.property_family`` — so consumers can group by family
    without recomputing). ``weight`` is this color's share of *its own* usages — the role's
    routed mass over the color's total routed mass, so a color's ``weight`` values sum to
    ~1.0. ``components`` is normalized evidence within this slot: which component types
    contributed the color to this role (e.g. ``{card_bg: 0.7, modal_bg: 0.3}``), summing to
    ~1.0 when non-empty.
    """

    model_config = ConfigDict(frozen=True)

    role: UsageRole = Field(
        description="The usage role this slot describes.",
    )
    property_family: PropertyFamily = Field(
        description=(
            "Rollup family for ``role`` (denormalized — always ``role.property_family``)."
        ),
    )
    weight: float = Field(
        description=(
            "This role's share of the color's total routed mass; a color's ``weight`` values "
            "sum to ~1.0."
        ),
        ge=0.0,
        le=1.0,
        examples=[0.7],
    )
    components: dict[ComponentType, float] = Field(
        default_factory=dict,
        description=(
            "Normalized evidence: which component types contributed the color to this role "
            "(each value in ``[0, 1]``, summing to ~1.0 when non-empty)."
        ),
        examples=[{"card_bg": 0.7, "modal_bg": 0.3}],
    )


class ColorUsage(BaseModel):
    """A measured color and where it is used — one entry of the color-keyed canonical inventory.

    ``prominence`` is the overall ranking signal blending area-truth (primary) with routed
    vote mass (secondary), so dominant backgrounds rank high while zero-area brand accents
    (CTA/link colors) are not buried; the ``colors`` tuple is sorted by it, descending,
    with a ``hex`` tiebreak. ``area`` is the raw screenshot area fraction the color's
    cluster covers (an auditable signal, not a probability). ``usages`` lists every
    [`UsageRole`][colorsense.UsageRole] the color appears in, most-used first
    (``weight`` descending, ``hex`` tiebreak).
    """

    model_config = ConfigDict(frozen=True)

    color: Color = Field(
        description="The measured color this inventory entry describes.",
    )
    prominence: float = Field(
        description=(
            "Overall ranking signal blending area-truth (primary) with routed vote mass "
            "(secondary); the ``colors`` tuple is sorted by it, descending."
        ),
        ge=0.0,
        le=1.0,
        examples=[0.83],
    )
    area: float = Field(
        description=(
            "Raw screenshot area fraction the color's cluster covers "
            "(auditable, not a probability)."
        ),
        ge=0.0,
        le=1.0,
        examples=[0.31],
    )
    usages: tuple[Usage, ...] = Field(
        default_factory=tuple,
        description=(
            "Every usage role the color appears in, most-used first (``weight`` descending)."
        ),
    )


class UsageEntry(BaseModel):
    """One color's standing within a usage role (role-keyed projection).

    ``probability`` is the posterior prominence of this color *within its role*
    (entries of one role sum to ~1.0). ``area`` is the raw screenshot area fraction
    the color's cluster covers — an auditable signal, not a probability. ``components``
    is normalized evidence: which component types contributed this color to this
    role (e.g. ``{card_bg: 0.7, modal_bg: 0.3}``), summing to ~1.0 when non-empty.
    """

    model_config = ConfigDict(frozen=True)

    color: Color = Field(
        description="The color this role entry describes.",
    )
    probability: float = Field(
        description=(
            "Posterior prominence of this color within its role; entries of one role sum to ~1.0."
        ),
        ge=0.0,
        le=1.0,
        examples=[0.55],
    )
    area: float = Field(
        description=(
            "Raw screenshot area fraction the color's cluster covers "
            "(auditable, not a probability)."
        ),
        ge=0.0,
        le=1.0,
        examples=[0.31],
    )
    components: dict[ComponentType, float] = Field(
        default_factory=dict,
        description=(
            "Normalized evidence: which component types contributed this color to this role "
            "(each value in ``[0, 1]``, summing to ~1.0 when non-empty)."
        ),
        examples=[{"card_bg": 0.7, "modal_bg": 0.3}],
    )


class UsagePalette(BaseModel):
    """The role-keyed palette projection: which colors paint each usage role.

    ``mapping`` is guaranteed to contain every [`UsageRole`][colorsense.UsageRole]; a
    role with no detected entries maps to an empty tuple. This invariant is enforced by an
    after-validator that backfills any missing roles, so even ``UsagePalette()`` and
    the empty-input path expose all eight keys.
    """

    model_config = ConfigDict(frozen=True)

    mapping: dict[UsageRole, tuple[UsageEntry, ...]] = Field(
        default_factory=dict,
        description=(
            "Colors painting each [`UsageRole`][colorsense.UsageRole]; guaranteed to contain "
            "every role (an unused role maps to ``()``)."
        ),
    )

    @model_validator(mode="after")
    def _backfill_roles(self) -> UsagePalette:
        """Ensure every [`UsageRole`][colorsense.UsageRole] is present (``()`` if absent).

        Returns:
            This model, with ``mapping`` backfilled so every role is a key.

        """
        # ``frozen=True`` blocks reassigning ``mapping`` itself, but the dict it points to
        # is a regular (non-deep-frozen) dict, so in-place backfill is sound.
        for role in UsageRole:
            if role not in self.mapping:
                self.mapping[role] = ()
        return self


class DesignToken(BaseModel):
    """A declared design token (CSS custom property) in the public result.

    ``name`` is the declared property name (e.g. ``--fgColor-default``); ``color`` is its
    value resolved in the rendered theme; ``semantic_role`` is the inferred semantic role.
    """

    model_config = ConfigDict(frozen=True)

    name: str = Field(
        description="Declared property name, including the leading ``--``.",
        examples=["--fgColor-default"],
    )
    color: Color = Field(
        description="The token's value resolved in the rendered theme.",
    )
    semantic_role: TokenSemanticRole = Field(
        description="The inferred semantic role for the token.",
    )


class DivergenceItem(BaseModel):
    """A declared-but-unused or used-but-undeclared palette discrepancy.

    Keyed by [`UsageRole`][colorsense.UsageRole] — the role-keyed usage view is where
    declared token intent is reconciled against measured usage.
    """

    model_config = ConfigDict(frozen=True)

    role: UsageRole = Field(
        description="The usage role the discrepancy was found under.",
    )
    color: Color = Field(
        description="The declared-but-unused or used-but-undeclared color.",
    )
    note: str = Field(
        description="Human-readable explanation of the discrepancy.",
        examples=["declared as brand_primary but not measured in use"],
    )


# ---------------------------------------------------------------------------
# Output models
# ---------------------------------------------------------------------------


class ThemePalette(BaseModel):
    """Everything derived for a single rendered theme.

    * ``colors`` — the **canonical, color-keyed index**: every measured color
      ([`ColorUsage`][colorsense.ColorUsage]) with where it is used and an overall
      ``prominence`` ranking. Answers "how is each color used?". Third-party-dominated
      colors are excluded (they live on ``AnalysisResult.third_party_colors``).
    * ``usage`` — the **role-keyed projection** ([`UsagePalette`][colorsense.UsagePalette]):
      the reconciled posterior over usage roles (measured usage pooled with declared token
      intent). Answers "which colors paint each role?".
    * ``divergence`` — declared-vs-measured discrepancies, keyed by usage role.
    * ``tokens`` — declared design tokens, opt-in: ``None`` means tokens were **not
      requested** (``include_tokens=False``, the default); ``()`` means tokens were
      requested but no usable color tokens were found — the page declares no custom
      properties at all, or every declaration was filtered as non-meaningful (no
      resolvable color, ``ignore`` semantic role, or zero classification weight).
    """

    model_config = ConfigDict(frozen=True)

    theme: Theme = Field(
        description="The theme this palette was derived for.",
    )
    colors: tuple[ColorUsage, ...] = Field(
        default_factory=tuple,
        description=(
            "Canonical color-keyed index: every measured color with where it is used, ranked "
            "by ``prominence`` (third-party-dominated colors excluded)."
        ),
    )
    usage: UsagePalette = Field(
        description=(
            "Role-keyed projection: which colors paint each usage role "
            "(measured pooled with token intent)."
        ),
    )
    divergence: tuple[DivergenceItem, ...] = Field(
        default_factory=tuple,
        description="Declared-vs-measured discrepancies, keyed by usage role.",
    )
    tokens: tuple[DesignToken, ...] | None = Field(
        default=None,
        description=(
            "Declared design tokens, opt-in: ``None`` means tokens were not requested; ``()`` "
            "means requested but none were usable."
        ),
    )


class RunMetadata(BaseModel):
    """Provenance for a single ``analyze`` run.

    Records which themes were requested versus actually analyzed (later themes whose
    render is perceptually identical to the primary are collapsed away) and the fetch
    policy in effect. A run reduced to a single theme iff ``len(themes_analyzed) == 1``.
    """

    model_config = ConfigDict(frozen=True)

    themes_requested: tuple[Theme, ...] = Field(
        default_factory=tuple,
        description="Themes the caller requested.",
        examples=[("light", "dark")],
    )
    themes_analyzed: tuple[Theme, ...] = Field(
        default_factory=tuple,
        description=(
            "Themes actually analyzed; a run reduced to a single theme iff this has length 1 "
            "(perceptually-identical later themes are collapsed away)."
        ),
        examples=[("light",)],
    )
    user_agent: str = Field(
        default="",
        description="User-Agent string used for fetches.",
    )
    respect_robots: bool = Field(
        default=True,
        description="Whether robots.txt was honored for this run.",
    )


class AnalysisResult(BaseModel):
    """The top-level typed result returned by ``analyze``.

    Per-theme analysis (the color-keyed index, the role-keyed usage view, divergence, and
    opt-in tokens) lives on each [`ThemePalette`][colorsense.ThemePalette] in ``themes``.

    This aggregate is **immutable**: it is ``frozen=True`` and its sequence field
    (``third_party_colors``) is a tuple, so neither reassigning an attribute
    (``result.url = ...``) nor mutating a sequence in place is possible. The ``themes``
    dict is protected from reassignment by ``frozen`` but its contents are not
    deep-frozen.
    """

    model_config = ConfigDict(frozen=True)

    url: str = Field(
        description="The analyzed page URL.",
        examples=["https://example.com/"],
    )
    viewport: Viewport = Field(
        description="Viewport the page was rendered at.",
    )
    themes: dict[Theme, ThemePalette] = Field(
        default_factory=dict,
        description="Per-theme analysis, keyed by [`Theme`][colorsense.Theme].",
    )
    third_party_colors: tuple[Color, ...] = Field(
        default_factory=tuple,
        description=(
            "Colors attributed to third-party/vendor widgets, held out of the per-theme index."
        ),
    )
    metadata: RunMetadata = Field(
        default_factory=RunMetadata,
        description="Provenance for the run (themes requested/analyzed, fetch policy).",
    )
