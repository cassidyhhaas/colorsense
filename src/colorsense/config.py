"""Typed loader for ``config/palette_config.yaml``.

This module mirrors the palette configuration YAML into fully-typed Pydantic
models and exposes the token-name matching helpers consumed by WP5/WP6. The
single source of truth for every value is the YAML file itself — this module
*models* and *loads* it, it does not hard-code config values.

Public interface
----------------
* :class:`Config` — top-level model + the four token helpers
  (:meth:`Config.strip_namespace`, :meth:`Config.match_name_rule`,
  :meth:`Config.detect_scale`, :meth:`Config.match_relational`).
* :func:`load_config` — read, validate, and normalize the YAML.
* :class:`ScaleInfo`, :class:`RelationalInfo` — small result models.
* :class:`TokenVocabularyConfig`, :class:`ComponentClassifierConfig` — domain models.
"""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, model_validator

from colorsense.models import PaletteRole, TokenSemanticRole

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
]


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
    weight: float
    match_type: MatchType = MatchType.substring


class RelationalModifier(BaseModel):
    """A regex modifier that reroutes a token to ``text-on-<base>``.

    The ``pattern`` must contain a named capture group ``base``.
    """

    model_config = ConfigDict(frozen=True)

    pattern: str
    type: str
    weight: float


class StateModifier(BaseModel):
    """An interaction-state shade modifier (hover/active/focus)."""

    model_config = ConfigDict(frozen=True)

    pattern: str
    type: str


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
    scale_present_confidence_boost: float


class ChannelPrior(BaseModel):
    """A ``role_to_palette_prior`` row that names a non-palette channel."""

    model_config = ConfigDict(frozen=True)

    channel: str


class DistributionPrior(BaseModel):
    """A ``role_to_palette_prior`` row: a normalized distribution over palette roles."""

    model_config = ConfigDict(frozen=True)

    distribution: dict[PaletteRole, float]


# A prior row is either a normalized distribution or a tagged channel.
PriorRow = DistributionPrior | ChannelPrior


class TokenVocabularyConfig(BaseModel):
    """The ``token_vocabulary`` config domain."""

    model_config = ConfigDict(frozen=True)

    namespace_prefixes: tuple[str, ...]
    strip_trailing: tuple[str, ...]
    known_system_confidence_boost: float
    name_rules: tuple[NameRule, ...]
    relational_modifiers: tuple[RelationalModifier, ...]
    state_modifiers: tuple[StateModifier, ...]
    scale_detection: ScaleDetectionConfig
    role_to_palette_prior: dict[TokenSemanticRole, PriorRow]
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
        raw_priors = data.get("role_to_palette_prior")
        if not isinstance(raw_priors, dict):
            return data

        normalized: dict[object, object] = {}
        for role, row in raw_priors.items():
            if not isinstance(row, dict):
                raise ValueError(f"role_to_palette_prior[{role!r}] must be a mapping")
            if "channel" in row:
                normalized[role] = {"channel": row["channel"]}
                continue
            total = 0.0
            for weight in row.values():
                total += float(weight)
            if total <= 0.0:
                raise ValueError(
                    f"role_to_palette_prior[{role!r}] distribution must sum to a positive value"
                )
            normalized[role] = {
                "distribution": {key: float(weight) / total for key, weight in row.items()}
            }
        new_data = dict(data)
        new_data["role_to_palette_prior"] = normalized
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


class GeometryConfig(BaseModel):
    """The geometry feature family: thresholds plus positional rules."""

    model_config = ConfigDict(frozen=True)

    thresholds: GeometryThresholds
    rules: tuple[WhenRule, ...]


class RepetitionConfig(BaseModel):
    """The repetition (card detector) feature family."""

    model_config = ConfigDict(frozen=True)

    min_siblings: int
    structural_similarity: float
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
    """A multiplicative veto applied after vote summation."""

    model_config = ConfigDict(frozen=True)

    factor: float
    applies_to: str


class ComponentClassifierConfig(BaseModel):
    """The ``component_classifier`` config domain."""

    model_config = ConfigDict(frozen=True)

    component_types: tuple[str, ...]
    softmax_temperature: float
    min_component_prob: float
    channel_routing: dict[str, str]
    semantic_tags: tuple[VoteRule, ...]
    geometry: GeometryConfig
    class_tokens: tuple[VoteRule, ...]
    interactivity: tuple[WhenRule, ...]
    repetition: RepetitionConfig
    third_party: ThirdPartyConfig
    suppressors: dict[str, Suppressor]
    brand_components: tuple[str, ...]


# ---------------------------------------------------------------------------
# Helper result models
# ---------------------------------------------------------------------------


class ScaleInfo(BaseModel):
    """Result of :meth:`Config.detect_scale`."""

    model_config = ConfigDict(frozen=True)

    family: str
    number: int
    is_chromatic: bool
    is_anchor: bool


class RelationalInfo(BaseModel):
    """Result of :meth:`Config.match_relational`."""

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

        is_anchor = False
        for fam_set, convention in (
            (chromatic_families, "tailwind"),
            (neutral_families, "tailwind"),
        ):
            if family in fam_set:
                anchor = scale.anchor_ranges.get(convention)
                if anchor is not None and anchor.contains(number):
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


def load_config(path: str | Path) -> Config:
    """Read, validate, and normalize the palette config YAML at ``path``.

    Raises a clear :class:`pydantic.ValidationError` (or a wrapping
    :class:`ValueError`) on malformed YAML or schema violations — never a bare
    ``KeyError``.
    """
    config_path = Path(path)
    try:
        raw_text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"could not read config file {config_path!r}: {exc}") from exc

    try:
        data = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML in config file {config_path!r}: {exc}") from exc

    if not isinstance(data, dict):
        kind = type(data).__name__
        raise ValueError(
            f"config file {config_path!r} must contain a top-level mapping, got {kind}"
        )

    return Config.model_validate(data)
