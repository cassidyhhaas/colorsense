"""Shared contracts for the colorsense pipeline.

These models are the single shared-contract surface for the pipeline. This file is
**frozen** by design: downstream code must not modify it. A change to a contract here
must be made centrally and re-validated against every dependent module, never patched
locally by a consumer.

Value objects (``Color``, ``Rect``, ``Viewport``) are immutable. Aggregate records carry
lists/dicts and are kept mutable for ergonomic assembly during the pipeline.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class PaletteRole(StrEnum):
    """A slot in the 60/30/10 palette taxonomy."""

    primary = "primary"
    secondary = "secondary"
    accent = "accent"
    neutral_light = "neutral_light"
    neutral_dark = "neutral_dark"


class TokenSemanticRole(StrEnum):
    """Semantic role inferred for a declared design token (CSS custom property)."""

    brand_primary = "brand_primary"
    brand_secondary = "brand_secondary"
    brand_accent = "brand_accent"
    interactive = "interactive"
    surface_base = "surface_base"
    surface_raised = "surface_raised"
    text_body = "text_body"
    neutral = "neutral"
    border = "border"
    text_on = "text_on"
    status = "status"
    ignore = "ignore"


class ComponentType(StrEnum):
    """Visual component a rendered element belongs to (source of a measured color)."""

    page_bg = "page_bg"
    page_text = "page_text"
    header_bg = "header_bg"
    header_text = "header_text"
    nav_bg = "nav_bg"
    nav_text = "nav_text"
    footer_bg = "footer_bg"
    footer_text = "footer_text"
    hero_bg = "hero_bg"
    hero_text = "hero_text"
    card_bg = "card_bg"
    card_text = "card_text"
    cta_bg = "cta_bg"
    cta_text = "cta_text"
    link = "link"
    button_secondary = "button_secondary"
    modal_bg = "modal_bg"
    input_bg = "input_bg"
    border = "border"
    badge = "badge"
    third_party = "third_party"


class Theme(StrEnum):
    """Color scheme a site is rendered under."""

    light = "light"
    dark = "dark"


# ---------------------------------------------------------------------------
# Value models (immutable)
# ---------------------------------------------------------------------------


class Color(BaseModel):
    """An sRGB color with cached OKLCH coordinates.

    ``hex`` is the opaque (or alpha-bearing) sRGB hex string; ``lightness``/``chroma``/
    ``hue`` are the OKLCH coordinates of the (composited) color. ``alpha`` is the source
    alpha.
    """

    model_config = ConfigDict(frozen=True)

    hex: str
    lightness: float
    chroma: float
    hue: float
    alpha: float = 1.0


class Rect(BaseModel):
    """Axis-aligned bounding box in CSS pixels."""

    model_config = ConfigDict(frozen=True)

    x: float
    y: float
    w: float
    h: float


class Viewport(BaseModel):
    """Rendering viewport."""

    model_config = ConfigDict(frozen=True)

    w: int
    h: int
    device_scale_factor: float


# ---------------------------------------------------------------------------
# Harvest models (produced by the harvest stage)
# ---------------------------------------------------------------------------


class TokenRecord(BaseModel):
    """A declared CSS custom property and its resolved color (if any)."""

    name: str
    raw_value: str
    resolved: Color | None
    scope: str
    media: str | None = None
    alias_target: str | None = None


class HarvestedElement(BaseModel):
    """A rendered DOM element and its measured computed colors + structural flags."""

    tag: str
    role: str | None
    id: str | None
    class_tokens: list[str] = Field(default_factory=list)
    rect: Rect
    position: str
    bg: Color | None
    text: Color | None
    border: Color | None
    is_iframe: bool
    cross_origin: bool
    shadow_host: bool
    clickable: bool
    has_hover_color_change: bool
    hover_bg: Color | None
    vendor_match: bool
    visible: bool
    aria_hidden: bool


class ScreenshotBin(BaseModel):
    """A quantized screenshot color and the fraction of page area it covers."""

    color: Color
    area_fraction: float


class Harvest(BaseModel):
    """Everything extracted from a single rendered page under one theme."""

    url: str
    theme: Theme
    viewport: Viewport
    tokens: list[TokenRecord] = Field(default_factory=list)
    elements: list[HarvestedElement] = Field(default_factory=list)
    screenshot_bins: list[ScreenshotBin] = Field(default_factory=list)
    logo_colors: list[Color] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Classification models
# ---------------------------------------------------------------------------


class ClassifiedToken(BaseModel):
    """A token tagged with its semantic role and a prior over palette roles."""

    record: TokenRecord
    semantic_role: TokenSemanticRole
    weight: float
    palette_prior: dict[PaletteRole, float] = Field(default_factory=dict)
    text_on_base: TokenSemanticRole | None = None


class ClassifiedElement(BaseModel):
    """A harvested element with a probability distribution over component types."""

    element: HarvestedElement
    component_dist: dict[ComponentType, float] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Palette models
# ---------------------------------------------------------------------------


class ColorCluster(BaseModel):
    """An area-weighted cluster of perceptually-near colors with a component mix."""

    color: Color
    area_weight: float
    member_count: int
    component_mix: dict[ComponentType, float] = Field(default_factory=dict)


class PaletteCandidate(BaseModel):
    """A candidate color for a palette role with a probability and evidence trail."""

    color: Color
    probability: float
    area: float
    evidence: dict[str, float] = Field(default_factory=dict)


class RoleResults(BaseModel):
    """Per-role ranked candidate lists."""

    mapping: dict[PaletteRole, list[PaletteCandidate]] = Field(default_factory=dict)


class DivergenceItem(BaseModel):
    """A declared-but-unused or used-but-undeclared palette discrepancy."""

    role: PaletteRole
    color: Color
    note: str


# ---------------------------------------------------------------------------
# Output models
# ---------------------------------------------------------------------------


class ThemePalette(BaseModel):
    """Reconciled palette roles for a single theme."""

    theme: Theme
    roles: RoleResults


class AnalysisResult(BaseModel):
    """The top-level typed result returned by ``analyze``."""

    url: str
    viewport: Viewport
    themes: dict[Theme, ThemePalette] = Field(default_factory=dict)
    tokens: list[ClassifiedToken] = Field(default_factory=list)
    third_party_colors: list[Color] = Field(default_factory=list)
    status_colors: list[Color] = Field(default_factory=list)
    divergence: list[DivergenceItem] = Field(default_factory=list)
    fit_score: float = 0.0
    metadata: dict[str, str] = Field(default_factory=dict)
