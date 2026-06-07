"""WP5 — Token classifier.

Classify declared design tokens (CSS custom properties) into semantic roles and
produce, for each token, a prior distribution over the 60/30/10 palette roles.

The classification precedence for a single :class:`TokenRecord` is:

1. **Relational** (``--on-primary`` / ``--card-foreground`` …): the token is a
   text/foreground color routed to the ``text_on`` channel.
2. **Name rule**: a direct semantic-role match on the (namespace-stripped) name.
3. **Scale detection**: a numbered family (``blue-500`` → brand/accent,
   ``gray-100`` → neutral).
4. **Fallback**: ``ignore`` with zero weight.

A final **alias-inheritance** pass lets a token that resolved to ``ignore`` adopt
the role/weight of the token its ``alias_target`` points at (transitively, with
cycle protection).

The public entry point is :func:`classify_tokens`.
"""

from __future__ import annotations

from colorsense.config import ChannelPrior, Config, DistributionPrior
from colorsense.models import (
    ClassifiedToken,
    Color,
    PaletteRole,
    TokenRecord,
    TokenSemanticRole,
)

__all__ = ["classify_tokens"]

# Base weight for a numbered-scale match before any anchor boost is applied.
_SCALE_BASE_WEIGHT: float = 3.0

# OKLCH lightness midpoint splitting neutral_light from neutral_dark.
_L_MIDPOINT: float = 0.5


def _classify_self(
    record: TokenRecord, config: Config
) -> tuple[TokenSemanticRole, float, TokenSemanticRole | None]:
    """Classify a single record on its own merits (no alias inheritance).

    Returns ``(semantic_role, weight, text_on_base)``. ``text_on_base`` is only
    populated for relational (``text_on``) tokens; it is ``None`` otherwise.
    """
    name = record.name

    # 1. Relational (text-on-<base>) takes precedence.
    relational = config.match_relational(name)
    if relational is not None:
        base_match = config.match_name_rule("--" + relational.base)
        text_on_base = base_match[0] if base_match is not None else None
        return TokenSemanticRole.text_on, relational.weight, text_on_base

    # 2. Direct name rule.
    name_match = config.match_name_rule(name)
    if name_match is not None:
        role, weight = name_match
        return role, weight, None

    # 3. Numbered-scale detection.
    scale = config.detect_scale(name)
    if scale is not None:
        if scale.is_chromatic:
            weight = _SCALE_BASE_WEIGHT
            if scale.is_anchor:
                boost = config.token_vocabulary.scale_detection.scale_present_confidence_boost
                weight *= boost
            return TokenSemanticRole.brand_accent, weight, None
        return TokenSemanticRole.neutral, _SCALE_BASE_WEIGHT, None

    # 4. Fallback.
    return TokenSemanticRole.ignore, 0.0, None


def _palette_prior(
    role: TokenSemanticRole, record: TokenRecord, config: Config
) -> dict[PaletteRole, float]:
    """Build the palette-role prior for a classified token.

    Distribution priors are copied verbatim (already normalized at load). Channel
    priors carry no palette weight. The ``neutral`` role is special-cased to use
    the measured OKLCH lightness of the resolved color when available.
    """
    prior_row = config.token_vocabulary.role_to_palette_prior.get(role)
    if prior_row is None:
        return {}

    if isinstance(prior_row, ChannelPrior):
        return {}

    if not isinstance(prior_row, DistributionPrior):  # pragma: no cover - defensive
        return {}

    if role is TokenSemanticRole.neutral and record.resolved is not None:
        if record.resolved.lightness >= _L_MIDPOINT:
            return {PaletteRole.neutral_light: 1.0}
        return {PaletteRole.neutral_dark: 1.0}

    return dict(prior_row.distribution)


def _resolve_alias_role(
    record: TokenRecord,
    index: dict[str, TokenRecord],
    self_class: dict[str, tuple[TokenSemanticRole, float, TokenSemanticRole | None]],
) -> tuple[TokenSemanticRole, float, TokenSemanticRole | None] | None:
    """Follow ``alias_target`` links until a non-``ignore`` classification is found.

    Returns the inherited ``(role, weight, text_on_base)`` triple, or ``None`` when
    the chain dead-ends (missing target, cycle, or all targets are ``ignore``).
    """
    seen: set[str] = {record.name}
    target_name = record.alias_target
    while target_name is not None:
        if target_name in seen:
            return None  # cycle guard
        seen.add(target_name)
        target = index.get(target_name)
        if target is None:
            return None
        role, weight, text_on_base = self_class[target.name]
        if role is not TokenSemanticRole.ignore:
            return role, weight, text_on_base
        target_name = target.alias_target
    return None


def classify_tokens(
    tokens: list[TokenRecord], config: Config
) -> tuple[list[ClassifiedToken], list[Color]]:
    """Classify ``tokens`` into semantic roles and palette priors.

    Returns ``(classified, status_colors)`` where ``classified`` is one
    :class:`ClassifiedToken` per input record (order preserved) and
    ``status_colors`` is the list of resolved colors of ``status`` tokens (records
    whose resolved color is ``None`` are skipped).
    """
    # Index by name; later declarations win for duplicate names.
    index: dict[str, TokenRecord] = {record.name: record for record in tokens}

    # First pass: classify every record on its own merits.
    self_class: dict[str, tuple[TokenSemanticRole, float, TokenSemanticRole | None]] = {}
    for record in tokens:
        self_class[record.name] = _classify_self(record, config)

    status_excluded = config.token_vocabulary.status_excluded_from_palette

    classified: list[ClassifiedToken] = []
    status_colors: list[Color] = []

    for record in tokens:
        role, weight, text_on_base = self_class[record.name]

        # Alias inheritance: an `ignore` token may adopt its target's role.
        if role is TokenSemanticRole.ignore and record.alias_target is not None:
            inherited = _resolve_alias_role(record, index, self_class)
            if inherited is not None:
                role, weight, text_on_base = inherited

        prior = _palette_prior(role, record, config)

        if role is TokenSemanticRole.status and status_excluded:
            prior = {}
            if record.resolved is not None:
                status_colors.append(record.resolved)

        classified.append(
            ClassifiedToken(
                record=record,
                semantic_role=role,
                weight=weight,
                palette_prior=prior,
                text_on_base=text_on_base,
            )
        )

    return classified, status_colors
