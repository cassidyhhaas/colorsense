"""Shared contracts for the colorsense pipeline.

These models are the single shared-contract surface for the pipeline. This file is
**frozen** by design: downstream code must not modify it. A change to a contract here
must be made centrally and re-validated against every dependent module, never patched
locally by a consumer. (Most recent central change: ``HarvestedElement`` gained
``input_type: str | None = None`` — the lowercased ``type`` attribute of an ``<input>``
element, ``None`` for non-inputs and for inputs with no/empty ``type`` attribute; mirrors
the ``has_box_shadow``/``has_text`` precedent (defaulted, the DOM harvester always sets
it) — re-validated against ``harvest.dom``, ``classify.components``, and every test
constructing ``HarvestedElement``. Previous central change: ``HarvestedElement`` gained
``has_text: bool = False`` — true iff the element has a direct child text node with
non-whitespace content; mirrors the ``has_box_shadow`` precedent (default ``False``, the
DOM harvester always sets it) — re-validated against ``harvest.dom``,
``classify.components``, and every test constructing ``HarvestedElement``. Previous
central change: the usage-keyed palette redesign —
``UsageCategory``/``UsageEntry``/``UsagePalette``/``DesignToken`` added, ``ThemePalette``
gained ``usage``/``fit_score``/``divergence``/``tokens``, ``DivergenceItem`` re-keyed to
``UsageCategory``, ``PaletteCandidate.evidence`` and ``AnalysisResult``'s
``fit_score``/``divergence``/``tokens``/``status_colors`` removed,
``RunMetadata.single_theme`` removed, ``ClassifiedToken``/``TokenRecord`` made
internal-only and ``ComponentType`` made public — re-validated against ``pipeline``,
``cli``, ``classify.tokens``, ``classify.components``, ``palette.inventory``,
``palette.usage``, ``palette.roles``, ``palette.reconcile``, and the ``harvest``
package.)

Value objects (``Color``, ``Rect``, ``Viewport``) are immutable. The **public result tree**
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
public [`DesignToken`][colorsense.DesignToken]. Along with the internal value type ``Rect``,
they are deliberately excluded from ``__all__``. ``ComponentType`` is **public**: it keys the
[`UsageEntry`][colorsense.UsageEntry]``.components`` evidence in the result tree.

The **public contract** types are enumerated in ``__all__`` below and re-exported from the
top-level `colorsense` package, which is the canonical import path for consumers.
``colorsense.models`` is their documented home, but importing from the package root
(``from colorsense import ...``) is preferred.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "AnalysisResult",
    "Color",
    "ComponentType",
    "DesignToken",
    "DivergenceItem",
    "PaletteCandidate",
    "PaletteRole",
    "RoleResults",
    "RunMetadata",
    "Theme",
    "ThemePalette",
    "TokenSemanticRole",
    "UsageCategory",
    "UsageEntry",
    "UsagePalette",
    "Viewport",
]

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


class UsageCategory(StrEnum):
    """How a rendered color is used on the page.

    The usage taxonomy keys the primary palette view ([`UsagePalette`][colorsense.UsagePalette]):

    * ``surface`` — backgrounds at every layer: page, header/nav/footer, hero, card,
      modal, input.
    * ``text`` — typography at every layer (page/header/nav/footer/hero/card text).
    * ``interactive`` — links, CTA backgrounds and their text, secondary buttons, badges.
    * ``border`` — borders and dividers.
    """

    surface = "surface"
    text = "text"
    interactive = "interactive"
    border = "border"


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
    """Visual component a rendered element belongs to (source of a measured color).

    Public: keys the [`UsageEntry`][colorsense.UsageEntry]``.components`` evidence in the
    result tree, naming which component types contributed a color to a usage category.
    """

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

    ``hex`` is always the *opaque* normalized lowercase 7-char sRGB hex string
    (``#rrggbb``) — alpha is carried separately in ``alpha`` and never encoded in the
    hex (the invariant ``color/primitives.py`` establishes; fixed-length hexes are also
    what keeps lexicographic tie-breaks well-defined). ``lightness``/``chroma``/``hue``
    are the OKLCH coordinates of the (composited) color; ``alpha`` is the source alpha.
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
    """Internal: a declared CSS custom property and its resolved color (if any).

    Not part of the public contract — ``scope``/``media``/``alias_target`` are harvest and
    classification scratch detail. The public projection is [`DesignToken`][colorsense.DesignToken].
    """

    model_config = ConfigDict(frozen=True)

    name: str
    raw_value: str
    resolved: Color | None
    scope: str
    media: str | None = None
    alias_target: str | None = None


class HarvestedElement(BaseModel):
    """A rendered DOM element and its measured computed colors + structural flags.

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
    """

    tag: str
    role: str | None
    id: str | None
    class_tokens: list[str] = Field(default_factory=list)
    rect: Rect
    position: str
    bg: Color | None
    text: Color | None
    border: Color | None
    input_type: str | None = None
    has_box_shadow: bool = False
    has_text: bool = False
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


# ---------------------------------------------------------------------------
# Classification models
# ---------------------------------------------------------------------------


class TokenOrigin(StrEnum):
    """Internal: which classification path produced a `ClassifiedToken`.

    Mirrors the classifier precedence in ``classify.tokens`` (relational > name rule >
    scale > fallback, with alias inheritance). Reconciliation uses it to gate
    declared-but-unused divergence to high-intent tokens (``relational`` / ``name_rule``)
    only — scale members, alias followers, and fallbacks are not author intent signals.
    """

    relational = "relational"
    name_rule = "name_rule"
    scale = "scale"
    alias = "alias"
    fallback = "fallback"


class ClassifiedToken(BaseModel):
    """Internal: a token tagged with its semantic role and a prior over usage categories.

    Not part of the public contract — consumers see declared tokens only through
    [`DesignToken`][colorsense.DesignToken]. ``weight`` is an internal scoring
    input; ``origin`` records the classification path for divergence gating.
    """

    model_config = ConfigDict(frozen=True)

    record: TokenRecord
    semantic_role: TokenSemanticRole
    weight: float
    usage_prior: dict[UsageCategory, float] = Field(default_factory=dict)
    origin: TokenOrigin = TokenOrigin.fallback


class ClassifiedElement(BaseModel):
    """A harvested element with a probability distribution over component types."""

    element: HarvestedElement
    component_dist: dict[ComponentType, float] = Field(default_factory=dict)


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

    color: Color
    area_weight: float
    member_count: int
    component_mix: dict[ComponentType, float] = Field(default_factory=dict)
    component_mass: dict[ComponentType, float] = Field(default_factory=dict)


class PaletteCandidate(BaseModel):
    """A candidate color for a palette role with a probability and its area share."""

    model_config = ConfigDict(frozen=True)

    color: Color
    probability: float
    area: float


class RoleResults(BaseModel):
    """Per-role ranked candidate lists.

    ``mapping`` is guaranteed to contain every [`PaletteRole`][colorsense.PaletteRole]; a role with
    no detected candidates maps to an empty tuple. This invariant is enforced by an after-validator
    that backfills any missing roles, so even ``RoleResults()`` and the empty-input path expose all
    five keys.
    """

    model_config = ConfigDict(frozen=True)

    mapping: dict[PaletteRole, tuple[PaletteCandidate, ...]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _backfill_roles(self) -> RoleResults:
        """Ensure every [`PaletteRole`][colorsense.PaletteRole] is present (``()`` when absent)."""
        # ``frozen=True`` blocks reassigning ``mapping`` itself, but the dict it points to
        # is a regular (non-deep-frozen) dict, so in-place backfill is sound.
        for role in PaletteRole:
            if role not in self.mapping:
                self.mapping[role] = ()
        return self


class UsageEntry(BaseModel):
    """One color's standing within a usage category.

    ``probability`` is the posterior prominence of this color *within its category*
    (entries of one category sum to ~1.0). ``area`` is the raw screenshot area fraction
    the color's cluster covers — an auditable signal, not a probability. ``components``
    is normalized evidence: which component types contributed this color to this
    category (e.g. ``{card_bg: 0.7, modal_bg: 0.3}``), summing to ~1.0 when non-empty.
    """

    model_config = ConfigDict(frozen=True)

    color: Color
    probability: float
    area: float
    components: dict[ComponentType, float] = Field(default_factory=dict)


class UsagePalette(BaseModel):
    """The usage-keyed palette view: what colors paint each usage category.

    ``mapping`` is guaranteed to contain every [`UsageCategory`][colorsense.UsageCategory]; a
    category with no detected entries maps to an empty tuple. This invariant is enforced by an
    after-validator that backfills any missing categories, so even ``UsagePalette()`` and
    the empty-input path expose all four keys.
    """

    model_config = ConfigDict(frozen=True)

    mapping: dict[UsageCategory, tuple[UsageEntry, ...]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _backfill_categories(self) -> UsagePalette:
        """Ensure every [`UsageCategory`][colorsense.UsageCategory] is present (``()`` if
        absent)."""
        # ``frozen=True`` blocks reassigning ``mapping`` itself, but the dict it points to
        # is a regular (non-deep-frozen) dict, so in-place backfill is sound.
        for category in UsageCategory:
            if category not in self.mapping:
                self.mapping[category] = ()
        return self


class DesignToken(BaseModel):
    """A declared design token (CSS custom property) in the public result.

    ``name`` is the declared property name (e.g. ``--fgColor-default``); ``color`` is its
    value resolved in the rendered theme; ``semantic_role`` is the inferred semantic role.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    color: Color
    semantic_role: TokenSemanticRole


class DivergenceItem(BaseModel):
    """A declared-but-unused or used-but-undeclared palette discrepancy.

    Keyed by [`UsageCategory`][colorsense.UsageCategory] — the usage view is where declared
    token intent is reconciled against measured usage.
    """

    model_config = ConfigDict(frozen=True)

    category: UsageCategory
    color: Color
    note: str


# ---------------------------------------------------------------------------
# Output models
# ---------------------------------------------------------------------------


class ThemePalette(BaseModel):
    """Everything derived for a single rendered theme.

    * ``usage`` — the **primary view**: the reconciled posterior over usage categories
      (measured usage pooled with declared token intent).
    * ``roles`` — a derived, **measured-only** 60/30/10 interpretation of the same
      clusters; it is no longer reconciled against tokens.
    * ``fit_score`` — descriptive "how 60/30/10-like is this design" in ``[0, 1]``; not a
      quality score.
    * ``divergence`` — declared-vs-measured discrepancies, keyed by usage category.
    * ``tokens`` — declared design tokens, opt-in: ``None`` means tokens were **not
      requested** (``include_tokens=False``, the default); ``()`` means tokens were
      requested but the page declares none.
    """

    model_config = ConfigDict(frozen=True)

    theme: Theme
    usage: UsagePalette
    roles: RoleResults
    fit_score: float
    divergence: tuple[DivergenceItem, ...] = Field(default_factory=tuple)
    tokens: tuple[DesignToken, ...] | None = None


class RunMetadata(BaseModel):
    """Provenance for a single ``analyze`` run.

    Records which themes were requested versus actually analyzed (later themes whose
    render is perceptually identical to the primary are collapsed away) and the fetch
    policy in effect. A run reduced to a single theme iff ``len(themes_analyzed) == 1``.
    """

    model_config = ConfigDict(frozen=True)

    themes_requested: tuple[Theme, ...] = Field(default_factory=tuple)
    themes_analyzed: tuple[Theme, ...] = Field(default_factory=tuple)
    user_agent: str = ""
    respect_robots: bool = True


class AnalysisResult(BaseModel):
    """The top-level typed result returned by ``analyze``.

    Per-theme analysis (the usage view, the derived 60/30/10 roles view, fit score,
    divergence, and opt-in tokens) lives on each [`ThemePalette`][colorsense.ThemePalette] in
    ``themes``.

    This aggregate is **immutable**: it is ``frozen=True`` and its sequence field
    (``third_party_colors``) is a tuple, so neither reassigning an attribute
    (``result.url = ...``) nor mutating a sequence in place is possible. The ``themes``
    dict is protected from reassignment by ``frozen`` but its contents are not
    deep-frozen.
    """

    model_config = ConfigDict(frozen=True)

    url: str
    viewport: Viewport
    themes: dict[Theme, ThemePalette] = Field(default_factory=dict)
    third_party_colors: tuple[Color, ...] = Field(default_factory=tuple)
    metadata: RunMetadata = Field(default_factory=RunMetadata)
