"""Shared contracts for the colorsense pipeline.

These models are the single shared-contract surface for the pipeline. This file is
**frozen** by design: downstream code must not modify it. A change to a contract here
must be made centrally and re-validated against every dependent module, never patched
locally by a consumer.

Value objects (``Color``, ``Rect``, ``Viewport``) are immutable. The **public result
tree** reachable from :class:`AnalysisResult` is also immutable: every output model is
``frozen=True`` and its sequence fields are ``tuple`` (not ``list``), so neither attribute
reassignment nor in-place ``.append()`` works. ``dict`` fields stay regular dicts —
``frozen`` blocks reassigning them, but their contents are intentionally not deep-frozen
(deep-freezing needs exotic types and breaks JSON round-trip). Tuples serialize to JSON
arrays and re-parse into tuples, so ``model_dump_json()`` / ``model_validate_json()``
round-trips cleanly.

The **internal-only** assembly models (``Harvest``, ``HarvestedElement``,
``ScreenshotBin``, ``ClassifiedElement``, ``ColorCluster``) remain mutable: they are
scratch structures the pipeline mutates while building the result and never escape to the
caller.
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
    width: float
    height: float


class Viewport(BaseModel):
    """Rendering viewport."""

    model_config = ConfigDict(frozen=True)

    width: int
    height: int
    device_scale_factor: float


# ---------------------------------------------------------------------------
# Harvest models (produced by the harvest stage)
# ---------------------------------------------------------------------------


class TokenRecord(BaseModel):
    """A declared CSS custom property and its resolved color (if any)."""

    model_config = ConfigDict(frozen=True)

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

    model_config = ConfigDict(frozen=True)

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

    model_config = ConfigDict(frozen=True)

    color: Color
    probability: float
    area: float
    evidence: dict[str, float] = Field(default_factory=dict)


class RoleResults(BaseModel):
    """Per-role ranked candidate lists.

    ``mapping`` always contains every :class:`PaletteRole`; a role with no detected
    candidates maps to an empty tuple.
    """

    model_config = ConfigDict(frozen=True)

    mapping: dict[PaletteRole, tuple[PaletteCandidate, ...]] = Field(default_factory=dict)


class DivergenceItem(BaseModel):
    """A declared-but-unused or used-but-undeclared palette discrepancy."""

    model_config = ConfigDict(frozen=True)

    role: PaletteRole
    color: Color
    note: str


# ---------------------------------------------------------------------------
# Output models
# ---------------------------------------------------------------------------


class ThemePalette(BaseModel):
    """Reconciled palette roles for a single theme."""

    model_config = ConfigDict(frozen=True)

    theme: Theme
    roles: RoleResults


class RunMetadata(BaseModel):
    """Provenance for a single ``analyze`` run.

    Records which themes were requested versus actually analyzed (later themes whose
    render is perceptually identical to the primary are collapsed away), whether the run
    reduced to a single theme, and the fetch policy in effect.
    """

    model_config = ConfigDict(frozen=True)

    themes_requested: tuple[Theme, ...] = Field(default_factory=tuple)
    themes_analyzed: tuple[Theme, ...] = Field(default_factory=tuple)
    single_theme: bool = True
    user_agent: str = ""
    respect_robots: bool = True


class AnalysisResult(BaseModel):
    """The top-level typed result returned by ``analyze``.

    This aggregate is **immutable**: it is ``frozen=True`` and its sequence fields
    (``tokens``, ``third_party_colors``, ``status_colors``, ``divergence``) are tuples, so
    neither reassigning an attribute (``result.fit_score = ...``) nor mutating a sequence in
    place (``result.tokens.append(...)``) is possible. The ``themes`` dict is protected from
    reassignment by ``frozen`` but its contents are not deep-frozen.
    """

    model_config = ConfigDict(frozen=True)

    url: str
    viewport: Viewport
    themes: dict[Theme, ThemePalette] = Field(default_factory=dict)
    tokens: tuple[ClassifiedToken, ...] = Field(default_factory=tuple)
    third_party_colors: tuple[Color, ...] = Field(default_factory=tuple)
    status_colors: tuple[Color, ...] = Field(default_factory=tuple)
    divergence: tuple[DivergenceItem, ...] = Field(default_factory=tuple)
    fit_score: float = 0.0
    metadata: RunMetadata = Field(default_factory=RunMetadata)
