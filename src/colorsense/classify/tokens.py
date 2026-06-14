"""Token classifier.

Classify declared design tokens (CSS custom properties) into semantic roles and
produce, for each token, a prior distribution over the usage roles
([`UsageRole`][colorsense.UsageRole]) — how the token's color is expected to be
used when rendered.

The classification precedence for a single `TokenRecord` is:

1. **Relational** (``--on-primary`` / ``--card-foreground`` …): the token is a
   text/foreground color routed to the ``text_on`` channel.
2. **Name rule**: a direct semantic-role match on the (namespace-stripped) name.
3. **Scale detection**: a numbered family (``blue-500`` → brand/accent,
   ``gray-100`` → neutral).
4. **Fallback**: ``ignore`` with zero weight.

A final **alias-inheritance** pass lets a token that resolved to ``ignore`` adopt
the role/weight of the token its ``alias_target`` points at (transitively, with
cycle protection).

Every classified token records its `TokenOrigin` — which of
the paths above produced it (alias-inherited classifications carry ``alias``).
Reconciliation uses the origin to gate declared-but-unused divergence to high-intent
tokens only.

The public entry point is `classify_tokens`.
"""

from __future__ import annotations

from colorsense.config import ChannelPrior, Config, DistributionPrior
from colorsense.models import (
    ClassifiedToken,
    TokenOrigin,
    TokenRecord,
    TokenSemanticRole,
    UsageRole,
)

__all__ = ["classify_tokens"]

# The (role, weight, origin) classification triple.
_Classification = tuple[TokenSemanticRole, float, TokenOrigin]


def _classify_self(record: TokenRecord, config: Config) -> _Classification:
    """Classify a single record on its own merits (no alias inheritance).

    Returns ``(semantic_role, weight, origin)``.
    """
    name = record.name

    # 1. Relational (text-on-<base>) takes precedence.
    relational = config.match_relational(name)
    if relational is not None:
        return TokenSemanticRole.text_on, relational.weight, TokenOrigin.relational

    # 2. Direct name rule.
    name_match = config.match_name_rule(name)
    if name_match is not None:
        role, weight = name_match
        return role, weight, TokenOrigin.name_rule

    # 3. Numbered-scale detection. Like every other classifier weight, the scale family's
    # base weight comes from the YAML (``scale_detection.base_weight``), so consumers
    # recalibrating the vocabulary can retune it alongside the name-rule weights.
    scale = config.detect_scale(name)
    if scale is not None:
        scale_config = config.token_vocabulary.scale_detection
        if scale.is_chromatic:
            weight = scale_config.base_weight
            if scale.is_anchor:
                weight *= scale_config.scale_present_confidence_boost
            return TokenSemanticRole.brand_accent, weight, TokenOrigin.scale
        return TokenSemanticRole.neutral, scale_config.base_weight, TokenOrigin.scale

    # 4. Fallback.
    return TokenSemanticRole.ignore, 0.0, TokenOrigin.fallback


def _usage_prior(role: TokenSemanticRole, config: Config) -> dict[UsageRole, float]:
    """Build the usage-role prior for a classified token.

    Distribution priors are copied verbatim (already normalized at load). Channel
    priors carry no usage weight.
    """
    prior_row = config.token_vocabulary.role_to_usage_prior.get(role)
    if prior_row is None:
        return {}

    if isinstance(prior_row, ChannelPrior):
        return {}

    if not isinstance(prior_row, DistributionPrior):  # pragma: no cover - defensive
        return {}

    return dict(prior_row.distribution)


def _resolve_alias_role(
    record: TokenRecord,
    index: dict[str, TokenRecord],
    self_classifications: dict[str, _Classification],
) -> _Classification | None:
    """Follow ``alias_target`` links until a non-``ignore`` classification is found.

    Returns the inherited classification with origin rewritten to
    `TokenOrigin.alias` (the alias itself was not matched, its target was), or
    ``None`` when the chain dead-ends (missing target, cycle, or all targets are
    ``ignore``).
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
        role, weight, _origin = self_classifications[target.name]
        if role is not TokenSemanticRole.ignore:
            return role, weight, TokenOrigin.alias
        target_name = target.alias_target
    return None


def classify_tokens(tokens: list[TokenRecord], config: Config) -> list[ClassifiedToken]:
    """Classify ``tokens`` into semantic roles and usage-role priors.

    Returns one `ClassifiedToken` per input record (order preserved). ``status``
    tokens get an empty prior when ``status_excluded_from_palette`` is set — they still
    surface to consumers as [`DesignToken`][colorsense.DesignToken] entries with
    ``semantic_role=status`` when tokens are requested.
    """
    # Index by name; later declarations win for duplicate names.
    index: dict[str, TokenRecord] = {record.name: record for record in tokens}

    # First pass: classify every record on its own merits.
    self_classifications: dict[str, _Classification] = {}
    for record in tokens:
        self_classifications[record.name] = _classify_self(record, config)

    status_excluded = config.token_vocabulary.status_excluded_from_palette

    classified: list[ClassifiedToken] = []

    for record in tokens:
        role, weight, origin = self_classifications[record.name]

        # Alias inheritance: an `ignore` token may adopt its target's role.
        if role is TokenSemanticRole.ignore and record.alias_target is not None:
            inherited = _resolve_alias_role(record, index, self_classifications)
            if inherited is not None:
                role, weight, origin = inherited

        prior = _usage_prior(role, config)

        if role is TokenSemanticRole.status and status_excluded:
            prior = {}

        classified.append(
            ClassifiedToken(
                record=record,
                semantic_role=role,
                weight=weight,
                usage_prior=prior,
                origin=origin,
            )
        )

    return classified
