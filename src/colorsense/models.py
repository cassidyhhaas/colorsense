"""Shared contracts for the colorsense pipeline.

These models are the single shared-contract surface for the pipeline. This file is
**frozen** by design: downstream code must not modify it. A change to a contract here
must be made centrally and re-validated against every dependent module, never patched
locally by a consumer. (Most recent central change:
``HarvestedElement`` gained ``effective_bg: Color | None = None`` and
``effective_bg_from_clickable: bool = False`` — the element's *composited* background
(the first fully-opaque ``background-color`` found walking the element and its ancestors
to the document root) and whether the ancestor that contributed it is itself
clickable/button-styled. They give downstream classification the theme/contrast-relative
context an inline element's own (usually transparent) ``bg`` lacks — distinguishing a
genuine inline link, whose text sits on a passive page/section surface, from a CTA-button
*label*, whose text sits on the button's own interactive fill. Both mirror the
``has_text``/``input_type`` precedent (defaulted, the DOM harvester always sets them;
``effective_bg`` is ``None`` only when no opaque background exists up the chain) and were
re-validated against ``harvest.dom``, ``classify.components``, and every test constructing
``HarvestedElement``. Previous central change: the **usage-role payload redesign** —
the old 4-value ``UsageCategory`` (surface/text/interactive/border) was deleted and
replaced by the 8-value developer-facing ``UsageRole``
(page/surface/banner/cta/action/text/link/border), plus a first-class ``PropertyFamily``
(background/text/border) rollup axis and the code-level ``family_of`` mapping. The result
tree gained a **color-keyed canonical index** (``ColorUsage`` carrying ``Usage`` slots and
an overall ``prominence``) alongside the re-keyed role-keyed projection (``UsagePalette``
of ``UsageEntry``). The legacy 60/30/10 view (``RoleResults``/``Composition`` plus its
``fit_score`` and the ``PaletteRole``/``PaletteCandidate`` taxonomy) was **dropped
entirely**: it is a consumer-side re-categorization, not the library's job, and keeping it
let 60/30/10-shaped logic leak into the primary views — so the response now focuses on the
color-keyed index and the role-keyed projection. ``ThemePalette`` now carries
``colors``/``usage``/``divergence``/``tokens``; ``DivergenceItem`` re-keyed its
``category: UsageCategory`` field to ``role: UsageRole`` — re-validated against
``pipeline``, ``cli``, ``classify.tokens``, ``config``, ``palette.usage``,
``palette.reconcile``, and the ``examples`` package.
Previous central change: ``HarvestedElement`` gained
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

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
    "family_of",
]

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class PropertyFamily(StrEnum):
    """Which family of CSS properties paints the color — the rollup axis over roles.

    Coarser than [`UsageRole`][colorsense.UsageRole]: every role belongs to exactly one
    family (the mapping is [`family_of`][colorsense.family_of]). ``background`` covers
    ``background-color`` / ``background-image``, ``text`` covers ``color``, and ``border``
    covers ``border-color``.
    """

    background = "background"
    text = "text"
    border = "border"


class UsageRole(StrEnum):
    """Developer-facing usage role — how a rendered color is used on the page.

    The role taxonomy keys the role-keyed palette projection
    ([`UsagePalette`][colorsense.UsagePalette]) and labels each
    [`Usage`][colorsense.Usage] slot of the color-keyed index. It splits the two axes the
    old 4-value usage taxonomy conflated — *which CSS property paints the color* (the
    [`PropertyFamily`][colorsense.PropertyFamily] rollup) versus *what kind of element it
    is* — so that, e.g., link text and CTA button backgrounds no longer share one slot:

    * ``page`` — the base canvas (the page background).
    * ``surface`` — raised content backgrounds: cards, modals, hero, inputs.
    * ``banner`` — chrome-bar backgrounds: header, nav, footer.
    * ``cta`` — the primary action background (CTA buttons).
    * ``action`` — secondary action backgrounds: secondary buttons, badges.
    * ``text`` — body/heading typography at every layer.
    * ``link`` — link color (typography of anchors).
    * ``border`` — borders and dividers.
    """

    page = "page"
    surface = "surface"
    banner = "banner"
    cta = "cta"
    action = "action"
    text = "text"
    link = "link"
    border = "border"


def family_of(role: UsageRole) -> PropertyFamily:
    """Return the [`PropertyFamily`][colorsense.PropertyFamily] a usage role rolls up to.

    A fixed code-level convention (mirroring ``channel_for`` for components): ``text``/``link``
    are painted by the element's ``color`` (the ``text``
    family), ``border`` by its ``border-color``, and every other role
    (``page``/``surface``/``banner``/``cta``/``action``) by a background property.
    """
    if role in (UsageRole.text, UsageRole.link):
        return PropertyFamily.text
    if role is UsageRole.border:
        return PropertyFamily.border
    return PropertyFamily.background


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

    Public: keys the fine-grained component evidence in the result tree — both
    [`Usage`][colorsense.Usage]``.components`` on the color-keyed index and
    [`UsageEntry`][colorsense.UsageEntry]``.components`` on the role-keyed projection —
    naming which component types contributed a color to a usage role.
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


def channel_for(component: ComponentType) -> str:
    """Return the color channel a component's vote mass routes to.

    The routing convention is fixed in code: components whose value ends with
    ``_text`` — plus ``link``, whose painted color is its typography color, not
    its (usually transparent) background — are painted by the element's
    ``color`` (its ``text`` channel), ``border`` by its ``border-color``, and
    everything else (including ``badge``, ``third_party`` and
    ``button_secondary``) by its ``background-color`` (its ``bg`` channel).

    This is the single source of truth for channel routing, shared by the
    component classifier's per-channel normalization
    (``classify/components.py``) and the inventory's per-channel attribution
    (``palette/inventory.py``). The two partitions MUST stay identical, so both
    call this one function — it lives here in the shared-contracts module so
    neither importer creates a cross-layer dependency.
    """
    if component.value.endswith("_text") or component is ComponentType.link:
        return "text"
    if component is ComponentType.border:
        return "border"
    return "bg"


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
    min_corner_radius: float = 0.0
    bg_gradient_stops: tuple[Color, ...] = ()
    effective_bg: Color | None = None
    effective_bg_from_clickable: bool = False
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
    """Internal: a token tagged with its semantic role and a prior over usage roles.

    Not part of the public contract — consumers see declared tokens only through
    [`DesignToken`][colorsense.DesignToken]. ``weight`` is an internal scoring
    input; ``origin`` records the classification path for divergence gating.
    """

    model_config = ConfigDict(frozen=True)

    record: TokenRecord
    semantic_role: TokenSemanticRole
    weight: float
    usage_prior: dict[UsageRole, float] = Field(default_factory=dict)
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


class Usage(BaseModel):
    """One usage slot of a color in the color-keyed index ([`ColorUsage`][colorsense.ColorUsage]).

    A color may be used in several roles (e.g. the same gray as ``text`` *and* ``border``);
    each gets its own ``Usage``. ``role`` is the [`UsageRole`][colorsense.UsageRole] this
    slot describes and ``property_family`` is its [`PropertyFamily`][colorsense.PropertyFamily]
    rollup (denormalized — always ``family_of(role)`` — so consumers can group by family
    without recomputing). ``weight`` is this color's share of *its own* usages — the role's
    routed mass over the color's total routed mass, so a color's ``weight`` values sum to
    ~1.0. ``components`` is normalized evidence within this slot: which component types
    contributed the color to this role (e.g. ``{card_bg: 0.7, modal_bg: 0.3}``), summing to
    ~1.0 when non-empty.
    """

    model_config = ConfigDict(frozen=True)

    role: UsageRole
    property_family: PropertyFamily
    weight: float
    components: dict[ComponentType, float] = Field(default_factory=dict)


class ColorUsage(BaseModel):
    """A measured color and where it is used — the color-keyed canonical inventory atom.

    ``prominence`` is the overall ranking signal blending area-truth (primary) with routed
    vote mass (secondary), so dominant backgrounds rank high while zero-area brand accents
    (CTA/link colors) are not buried; the ``colors`` tuple is sorted by it, descending,
    with a ``hex`` tiebreak. ``area`` is the raw screenshot area fraction the color's
    cluster covers (an auditable signal, not a probability). ``usages`` lists every
    [`UsageRole`][colorsense.UsageRole] the color appears in, most-used first
    (``weight`` descending, ``hex`` tiebreak).
    """

    model_config = ConfigDict(frozen=True)

    color: Color
    prominence: float
    area: float
    usages: tuple[Usage, ...] = Field(default_factory=tuple)


class UsageEntry(BaseModel):
    """One color's standing within a usage role (role-keyed projection).

    ``probability`` is the posterior prominence of this color *within its role*
    (entries of one role sum to ~1.0). ``area`` is the raw screenshot area fraction
    the color's cluster covers — an auditable signal, not a probability. ``components``
    is normalized evidence: which component types contributed this color to this
    role (e.g. ``{card_bg: 0.7, modal_bg: 0.3}``), summing to ~1.0 when non-empty.
    """

    model_config = ConfigDict(frozen=True)

    color: Color
    probability: float
    area: float
    components: dict[ComponentType, float] = Field(default_factory=dict)


class UsagePalette(BaseModel):
    """The role-keyed palette projection: which colors paint each usage role.

    ``mapping`` is guaranteed to contain every [`UsageRole`][colorsense.UsageRole]; a
    role with no detected entries maps to an empty tuple. This invariant is enforced by an
    after-validator that backfills any missing roles, so even ``UsagePalette()`` and
    the empty-input path expose all eight keys.
    """

    model_config = ConfigDict(frozen=True)

    mapping: dict[UsageRole, tuple[UsageEntry, ...]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _backfill_roles(self) -> UsagePalette:
        """Ensure every [`UsageRole`][colorsense.UsageRole] is present (``()`` if absent)."""
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

    name: str
    color: Color
    semantic_role: TokenSemanticRole


class DivergenceItem(BaseModel):
    """A declared-but-unused or used-but-undeclared palette discrepancy.

    Keyed by [`UsageRole`][colorsense.UsageRole] — the role-keyed usage view is where
    declared token intent is reconciled against measured usage.
    """

    model_config = ConfigDict(frozen=True)

    role: UsageRole
    color: Color
    note: str


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

    theme: Theme
    colors: tuple[ColorUsage, ...] = Field(default_factory=tuple)
    usage: UsagePalette
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

    Per-theme analysis (the color-keyed index, the role-keyed usage view, divergence, and
    opt-in tokens) lives on each [`ThemePalette`][colorsense.ThemePalette] in ``themes``.

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
