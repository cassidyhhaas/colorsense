"""Typed loader for the palette configuration YAML.

This module mirrors the palette configuration YAML into fully-typed Pydantic
models and exposes the token-name matching helpers consumed by the token and
component classifiers. The
single source of truth for every value is the YAML file itself — this module
*models* and *loads* it, it does not hard-code config values.

The default configuration ships with the package as ``data/palette_config.yaml``
and is loaded by [`load_default_config`][colorsense.load_default_config]; callers can
supply their own copy via [`load_config`][colorsense.load_config].

Public interface
----------------
* [`Config`][colorsense.Config] — top-level model + the four token helpers
  ([`Config.strip_namespace`][colorsense.Config.strip_namespace],
  [`Config.match_name_rule`][colorsense.Config.match_name_rule],
  [`Config.detect_scale`][colorsense.Config.detect_scale],
  [`Config.match_relational`][colorsense.Config.match_relational]).
* [`load_default_config`][colorsense.load_default_config] — load the configuration bundled
  with the package.
* [`load_config`][colorsense.load_config] — read, validate, and normalize a YAML file from a
  path.
* `ScaleInfo`, `RelationalInfo` — small result models.
* `TokenVocabularyConfig`, `ComponentClassifierConfig` — domain models.
"""

from __future__ import annotations

import re
from enum import StrEnum
from importlib import resources
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from colorsense.models import TokenSemanticRole, UsageRole

__all__ = [
    "ChannelPrior",
    "ComponentClassifierConfig",
    "Config",
    "DistributionPrior",
    "MatchType",
    "RelationalInfo",
    "ScaleInfo",
    "TokenVocabularyConfig",
    "load_config",
    "load_default_config",
]

# The configuration bundled inside the package (importable resource location).
_DATA_PACKAGE = "colorsense"
_BUNDLED_CONFIG = "data/palette_config.yaml"

# Component-classifier dispatch names implemented in ``classify.components``. The
# ``when`` predicates and suppressor keys are matched by string there, so any name
# outside these closed sets would be a silent no-op; the config model rejects them
# at load time instead, making a stale custom YAML fail loudly.
_KNOWN_INTERACTIVITY_PREDICATES = frozenset(
    {
        "clickable",
        "input[submit|button]",
        "has_hover_color_change",
        "a & button_surface",
        "a & !button_surface",
    }
)
_KNOWN_GEOMETRY_PREDICATES = frozenset(
    {
        "full_width & top<top_band & h<short_h",
        "position in (fixed,sticky) & top<sticky_top_px",
        "full_width & top<top_band & h>=hero_min_h",
        "top>=bottom_band & full_width",
        "area<=small_area & clickable",
        "pill & paints_fill & has_text & h<=badge_max_h_px",
    }
)
_KNOWN_SUPPRESSORS = frozenset(
    {
        "third_party_present",
        "aria_hidden",
        "zero_area_or_hidden",
    }
)


# ---------------------------------------------------------------------------
# Small enums / leaf types
# ---------------------------------------------------------------------------


class MatchType(StrEnum):
    """How a name rule's ``match`` string is compared against a token name."""

    substring = "substring"
    exact = "exact"
    regex = "regex"


# ---------------------------------------------------------------------------
# token_vocabulary leaf models
# ---------------------------------------------------------------------------


class NameRule(BaseModel):
    """A single token-name -> semantic-role rule."""

    model_config = ConfigDict(frozen=True)

    match: str
    role: TokenSemanticRole
    weight: float = Field(ge=0.0)
    match_type: MatchType = MatchType.substring


class RelationalModifier(BaseModel):
    """A regex modifier that reroutes a token to ``text-on-<base>``.

    The ``pattern`` must compile and contain a named capture group ``base`` — enforced at
    load so a typo'd pattern fails loudly instead of becoming a rule that silently never
    reroutes anything (``match_relational`` skips matches without a ``base`` group).
    """

    model_config = ConfigDict(frozen=True)

    pattern: str
    type: str
    weight: float = Field(ge=0.0)

    @model_validator(mode="after")
    def _validate_pattern(self) -> RelationalModifier:
        try:
            compiled = re.compile(self.pattern)
        except re.error as exc:
            raise ValueError(f"relational_modifiers pattern {self.pattern!r}: {exc}") from exc
        if "base" not in compiled.groupindex:
            raise ValueError(
                f"relational_modifiers pattern {self.pattern!r} must contain a "
                "named capture group 'base' (e.g. '(?P<base>...)')"
            )
        return self


class AnchorRange(BaseModel):
    """Inclusive ``[low, high]`` anchor-step range for a scale convention."""

    model_config = ConfigDict(frozen=True)

    low: int
    high: int

    @model_validator(mode="before")
    @classmethod
    def _from_pair(cls, value: object) -> object:
        """Accept the YAML two-element ``[low, high]`` list form."""
        if isinstance(value, (list, tuple)):
            if len(value) != 2:
                raise ValueError("anchor range must be a two-element [low, high] list")
            return {"low": value[0], "high": value[1]}
        return value

    def contains(self, number: int) -> bool:
        """Return whether ``number`` falls within ``[low, high]`` inclusive."""
        return self.low <= number <= self.high


class ScaleDetectionConfig(BaseModel):
    """Numbered-scale detection settings."""

    model_config = ConfigDict(frozen=True)

    enabled: bool
    number_pattern: str
    chromatic_families: tuple[str, ...]
    neutral_families: tuple[str, ...]
    anchor_ranges: dict[str, AnchorRange]
    base_weight: float = Field(gt=0.0)
    scale_present_confidence_boost: float = Field(ge=0.0)

    @model_validator(mode="after")
    def _validate_number_pattern(self) -> ScaleDetectionConfig:
        """The pattern must compile and capture the scale number as group 1.

        ``detect_scale`` reads ``match.group(1)``; without this check a groupless pattern
        validates fine and raises ``IndexError`` only when a numbered token first appears.
        """
        try:
            compiled = re.compile(self.number_pattern)
        except re.error as exc:
            raise ValueError(
                f"scale_detection.number_pattern {self.number_pattern!r}: {exc}"
            ) from exc
        if compiled.groups < 1:
            raise ValueError(
                f"scale_detection.number_pattern {self.number_pattern!r} must contain a "
                "capture group for the scale number (group 1)"
            )
        return self


class ChannelPrior(BaseModel):
    """A ``role_to_usage_prior`` row that names a non-palette channel."""

    model_config = ConfigDict(frozen=True)

    channel: str


class DistributionPrior(BaseModel):
    """A ``role_to_usage_prior`` row: a normalized distribution over usage roles."""

    model_config = ConfigDict(frozen=True)

    distribution: dict[UsageRole, float]


# A prior row is either a normalized distribution or a tagged channel.
PriorRow = DistributionPrior | ChannelPrior


class TokenVocabularyConfig(BaseModel):
    """The ``token_vocabulary`` config domain."""

    model_config = ConfigDict(frozen=True)

    namespace_prefixes: tuple[str, ...]
    strip_trailing: tuple[str, ...]
    known_system_confidence_boost: float = Field(ge=0.0)
    name_rules: tuple[NameRule, ...]
    relational_modifiers: tuple[RelationalModifier, ...]
    scale_detection: ScaleDetectionConfig
    role_to_usage_prior: dict[TokenSemanticRole, PriorRow]
    status_excluded_from_palette: bool

    @model_validator(mode="before")
    @classmethod
    def _normalize_priors(cls, data: object) -> object:
        """Coerce each prior row into a channel or a normalized distribution.

        Distribution rows are normalized to sum to 1.0; channel rows
        (``{channel: ...}``) are passed through untouched. Validation errors
        (e.g. an empty / all-zero distribution) surface as pydantic errors.
        """
        if not isinstance(data, dict):
            return data
        raw_priors = data.get("role_to_usage_prior")
        if not isinstance(raw_priors, dict):
            return data

        normalized: dict[object, object] = {}
        for role, row in raw_priors.items():
            if not isinstance(row, dict):
                raise ValueError(f"role_to_usage_prior[{role!r}] must be a mapping")
            if "channel" in row:
                normalized[role] = {"channel": row["channel"]}
                continue
            total = 0.0
            for key, weight in row.items():
                value = float(weight)
                if value < 0.0:
                    # A negative prior would survive normalization and reach reconcile's
                    # ``** alpha`` pooling, where a negative base under a fractional
                    # exponent yields a complex number — fail at load, not deep in math.
                    raise ValueError(
                        f"role_to_usage_prior[{role!r}][{key!r}] must be >= 0, got {value}"
                    )
                total += value
            if total <= 0.0:
                raise ValueError(
                    f"role_to_usage_prior[{role!r}] distribution must sum to a positive value"
                )
            normalized[role] = {
                "distribution": {key: float(weight) / total for key, weight in row.items()}
            }
        new_data = dict(data)
        new_data["role_to_usage_prior"] = normalized
        return new_data


# ---------------------------------------------------------------------------
# component_classifier leaf models
# ---------------------------------------------------------------------------


class VoteRule(BaseModel):
    """A ``{match: <token>, votes: {component: weight}}`` rule."""

    model_config = ConfigDict(frozen=True)

    match: str
    votes: dict[str, float]


class WhenRule(BaseModel):
    """A ``{when: <predicate>, votes: {component: weight}}`` rule."""

    model_config = ConfigDict(frozen=True)

    when: str
    votes: dict[str, float]


class PresenceRule(BaseModel):
    """A presence-gated feature family: ``{votes: {component: weight}}``.

    Applied when a structural fact holds for an element (e.g. it paints a border, or it
    has direct text content) — there is no ``match``/``when`` string because the gating
    predicate is the feature family itself (fixed in ``classify.components``).
    """

    model_config = ConfigDict(frozen=True)

    votes: dict[str, float]


class GeometryThresholds(BaseModel):
    """Geometry thresholds (fractions of the viewport, or pixels)."""

    model_config = ConfigDict(frozen=True)

    top_band: float
    bottom_band: float
    full_width: float
    short_h: float
    hero_min_h: float
    sticky_top_px: float
    small_area: float
    badge_max_h_px: float


class GeometryConfig(BaseModel):
    """The geometry feature family: thresholds plus positional rules."""

    model_config = ConfigDict(frozen=True)

    thresholds: GeometryThresholds
    rules: tuple[WhenRule, ...]


class RepetitionConfig(BaseModel):
    """The repetition (card detector) feature family."""

    model_config = ConfigDict(frozen=True)

    min_siblings: int
    requires_any: tuple[str, ...]
    votes: dict[str, float]


class ThirdPartyConfig(BaseModel):
    """The origin / third-party feature family."""

    model_config = ConfigDict(frozen=True)

    votes_iframe: dict[str, float]
    votes_cross_origin: dict[str, float]
    votes_shadow_host: dict[str, float]
    votes_vendor_match: dict[str, float]
    vendor_prefixes: tuple[str, ...]


class Suppressor(BaseModel):
    """A multiplicative veto applied after vote summation.

    ``applies_to`` is restricted to the two scopes the classifier implements:
    ``"all"`` (every accumulated vote) or ``"brand_components"`` (only the
    configured brand components, on third-party elements).
    """

    model_config = ConfigDict(frozen=True)

    factor: float = Field(ge=0.0)
    applies_to: Literal["all", "brand_components"]


class ComponentClassifierConfig(BaseModel):
    """The ``component_classifier`` config domain."""

    model_config = ConfigDict(frozen=True)

    component_types: tuple[str, ...]
    # gt=0: a zero temperature divides by zero at classify time, and a NEGATIVE one
    # silently *inverts* the component ranking — the worst kind of misconfiguration.
    softmax_temperature: float = Field(gt=0.0)
    min_component_prob: float = Field(ge=0.0, le=1.0)
    semantic_tags: tuple[VoteRule, ...]
    geometry: GeometryConfig
    class_tokens: tuple[VoteRule, ...]
    interactivity: tuple[WhenRule, ...]
    border_presence: PresenceRule
    text_presence: PresenceRule
    repetition: RepetitionConfig
    third_party: ThirdPartyConfig
    suppressors: dict[str, Suppressor]
    brand_components: tuple[str, ...]

    @model_validator(mode="after")
    def _validate_dispatch_names(self) -> ComponentClassifierConfig:
        """Reject ``when`` predicates and suppressor keys the classifier does not implement.

        ``classify.components`` dispatches these by string; an unknown name would
        otherwise be a knob that silently never fires.
        """
        for rule in self.interactivity:
            if rule.when not in _KNOWN_INTERACTIVITY_PREDICATES:
                raise ValueError(
                    f"unknown interactivity predicate {rule.when!r}; "
                    f"implemented predicates: {sorted(_KNOWN_INTERACTIVITY_PREDICATES)}"
                )
        for rule in self.geometry.rules:
            if rule.when not in _KNOWN_GEOMETRY_PREDICATES:
                raise ValueError(
                    f"unknown geometry predicate {rule.when!r}; "
                    f"implemented predicates: {sorted(_KNOWN_GEOMETRY_PREDICATES)}"
                )
        for key in self.suppressors:
            if key not in _KNOWN_SUPPRESSORS:
                raise ValueError(
                    f"unknown suppressor {key!r}; "
                    f"implemented suppressors: {sorted(_KNOWN_SUPPRESSORS)}"
                )
        return self


# ---------------------------------------------------------------------------
# Helper result models
# ---------------------------------------------------------------------------


class ScaleInfo(BaseModel):
    """Result of [`Config.detect_scale`][colorsense.Config.detect_scale]."""

    model_config = ConfigDict(frozen=True)

    family: str
    number: int
    is_chromatic: bool
    is_anchor: bool


class RelationalInfo(BaseModel):
    """Result of [`Config.match_relational`][colorsense.Config.match_relational]."""

    model_config = ConfigDict(frozen=True)

    base: str
    type: str
    weight: float


# ---------------------------------------------------------------------------
# Top-level config
# ---------------------------------------------------------------------------


class Config(BaseModel):
    """The fully-loaded palette configuration plus token helper methods."""

    model_config = ConfigDict(frozen=True)

    token_vocabulary: TokenVocabularyConfig
    component_classifier: ComponentClassifierConfig

    # -- helpers ----------------------------------------------------------

    def _matched_namespace_prefix(self, remainder: str) -> str | None:
        """Return the longest ``namespace_prefixes`` entry that prefixes ``remainder``.

        Comparison is case-insensitive. Returns the prefix as declared in the
        config (original casing), or ``None`` if no prefix matches.
        """
        lowered = remainder.lower()
        best: str | None = None
        for prefix in self.token_vocabulary.namespace_prefixes:
            if lowered.startswith(prefix.lower()) and (best is None or len(prefix) > len(best)):
                best = prefix
        return best

    def strip_namespace(self, name: str) -> str:
        """Strip a leading ``--``, the longest namespace prefix, then a trailing suffix.

        The returned remainder is lower-cased (matching is case-insensitive on
        the remainder).
        """
        remainder = name[2:] if name.startswith("--") else name

        prefix = self._matched_namespace_prefix(remainder)
        if prefix is not None:
            remainder = remainder[len(prefix) :]

        lowered = remainder.lower()
        for suffix in self.token_vocabulary.strip_trailing:
            if lowered.endswith(suffix.lower()):
                lowered = lowered[: len(lowered) - len(suffix)]
                break
        return lowered

    def match_name_rule(self, name: str) -> tuple[TokenSemanticRole, float] | None:
        """Match ``name`` against ``name_rules`` with exact > regex > substring precedence.

        If a known system namespace prefix was present, the returned weight is
        multiplied by ``known_system_confidence_boost``. Returns ``None`` when no
        rule matches.
        """
        remainder = name[2:] if name.startswith("--") else name

        prefix = self._matched_namespace_prefix(remainder)
        known_system = prefix is not None
        if prefix is not None:
            remainder = remainder[len(prefix) :]

        lowered = remainder.lower()
        for suffix in self.token_vocabulary.strip_trailing:
            if lowered.endswith(suffix.lower()):
                lowered = lowered[: len(lowered) - len(suffix)]
                break

        rules = self.token_vocabulary.name_rules
        matched: NameRule | None = None

        # 1. exact
        for rule in rules:
            if rule.match_type is MatchType.exact and rule.match.lower() == lowered:
                matched = rule
                break

        # 2. regex
        if matched is None:
            for rule in rules:
                if rule.match_type is MatchType.regex and re.search(
                    rule.match, lowered, re.IGNORECASE
                ):
                    matched = rule
                    break

        # 3. substring, longest match wins
        if matched is None:
            best_len = -1
            for rule in rules:
                if (
                    rule.match_type is MatchType.substring
                    and rule.match.lower() in lowered
                    and len(rule.match) > best_len
                ):
                    best_len = len(rule.match)
                    matched = rule

        if matched is None:
            return None

        weight = matched.weight
        if known_system:
            weight *= self.token_vocabulary.known_system_confidence_boost
        return matched.role, weight

    def detect_scale(self, name: str) -> ScaleInfo | None:
        """Detect a trailing scale number and identify its family.

        Returns ``None`` when no scale number is present. ``family`` is the
        remainder once the namespace and the trailing number are stripped.
        """
        scale = self.token_vocabulary.scale_detection
        if not scale.enabled:
            return None

        remainder = name[2:] if name.startswith("--") else name
        prefix = self._matched_namespace_prefix(remainder)
        if prefix is not None:
            remainder = remainder[len(prefix) :]
        lowered = remainder.lower()

        match = re.search(scale.number_pattern, lowered)
        if match is None:
            return None
        number = int(match.group(1))

        # The family is everything before the matched scale separator+number.
        family = lowered[: match.start()].strip("-_/.")

        chromatic_families = {f.lower() for f in scale.chromatic_families}
        neutral_families = {f.lower() for f in scale.neutral_families}
        is_chromatic = family in chromatic_families and family not in neutral_families

        # A number is an anchor when it falls within ANY configured convention's
        # range (tailwind, radix, ...), provided the family is a known chromatic
        # or neutral family. Conventions are checked in sorted-key order so the
        # result is deterministic regardless of YAML mapping order.
        is_anchor = False
        if family in chromatic_families or family in neutral_families:
            for convention in sorted(scale.anchor_ranges):
                if scale.anchor_ranges[convention].contains(number):
                    is_anchor = True
                    break

        return ScaleInfo(
            family=family,
            number=number,
            is_chromatic=is_chromatic,
            is_anchor=is_anchor,
        )

    def match_relational(self, name: str) -> RelationalInfo | None:
        """Match ``relational_modifiers`` patterns against the post-strip name.

        Returns ``RelationalInfo`` with the captured ``base`` token, the modifier
        ``type``, and its ``weight``. Returns ``None`` when nothing matches.
        """
        remainder = name[2:] if name.startswith("--") else name
        prefix = self._matched_namespace_prefix(remainder)
        if prefix is not None:
            remainder = remainder[len(prefix) :]
        lowered = remainder.lower()

        for modifier in self.token_vocabulary.relational_modifiers:
            match = re.search(modifier.pattern, lowered, re.IGNORECASE)
            if match is None:
                continue
            base = match.groupdict().get("base")
            if base is None:
                continue
            return RelationalInfo(base=base, type=modifier.type, weight=modifier.weight)
        return None


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def load_default_config() -> Config:
    """Load the palette configuration bundled with the installed package.

    Resolves ``data/palette_config.yaml`` from the package itself (via
    `importlib.resources`), so it works regardless of the current working
    directory and whether the package is installed editable, as a wheel, or
    zipped. This is what [`colorsense.analyze`][colorsense.analyze] uses when no ``config_path``
    is given.
    """
    raw_text = resources.files(_DATA_PACKAGE).joinpath(_BUNDLED_CONFIG).read_text(encoding="utf-8")
    return _build_config(raw_text, f"<bundled {_BUNDLED_CONFIG}>")


def load_config(path: str | Path) -> Config:
    """Read, validate, and normalize the palette config YAML at ``path``.

    For the configuration shipped with the package, prefer
    [`load_default_config`][colorsense.load_default_config]. Raises a clear
    `pydantic.ValidationError` (or a wrapping `ValueError`) on malformed YAML or schema violations —
    never a bare ``KeyError``.
    """
    config_path = Path(path)
    try:
        raw_text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"could not read config file {config_path!r}: {exc}") from exc
    return _build_config(raw_text, repr(config_path))


def _build_config(raw_text: str, source: str) -> Config:
    """Parse and validate raw YAML text into a [`Config`][colorsense.Config].

    ``source`` is a human-readable label for the origin of ``raw_text`` used in
    error messages (a file path or a bundled-resource marker).
    """
    try:
        data = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML in config {source}: {exc}") from exc

    if not isinstance(data, dict):
        kind = type(data).__name__
        raise ValueError(f"config {source} must contain a top-level mapping, got {kind}")

    return Config.model_validate(data)
