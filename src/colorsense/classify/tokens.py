"""Token classifier.

Classify declared design tokens (CSS custom properties) into semantic roles and
produce, for each token, its usage intent: a distribution over the usage roles
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

from pydantic import BaseModel

from colorsense.config import ChannelRoute, Config, UsageIntent
from colorsense.models import (
    ClassifiedToken,
    TokenOrigin,
    TokenRecord,
    TokenSemanticRole,
    UsageRole,
)

__all__ = ["classify_tokens"]


class _TokenRoleClassification(BaseModel):
    """Internal: one token's classified semantic role, scoring weight, and origin path.

    These fields flow into a `ClassifiedToken` once alias inheritance and role
    distributions are resolved.

    Attributes:
        semantic_role: The semantic role inferred for the token.
        weight: The classification scoring weight (relative confidence/intent mass).
        origin: Which classification path produced this role.
    """

    semantic_role: TokenSemanticRole
    weight: float
    origin: TokenOrigin


def _classify_role_without_alias_inheritance(
    record: TokenRecord, config: Config
) -> _TokenRoleClassification:
    """Classify a single record's role on its own merits (no alias inheritance).

    Applies the precedence relational → name rule → scale detection → fallback, returning
    the first match.

    Args:
        record: The declared token to classify.
        config: The loaded configuration supplying the matching vocabulary and weights.

    Returns:
        The token's semantic role, scoring weight, and origin path.
    """
    name = record.name

    # 1. Relational (text-on-<base>) takes precedence.
    relational = config.match_relational(name)
    if relational is not None:
        return _TokenRoleClassification(
            semantic_role=TokenSemanticRole.TEXT_ON,
            weight=relational.weight,
            origin=TokenOrigin.RELATIONAL,
        )

    # 2. Direct name rule.
    name_match = config.match_name_rule(name)
    if name_match is not None:
        role, weight = name_match
        return _TokenRoleClassification(
            semantic_role=role, weight=weight, origin=TokenOrigin.NAME_RULE
        )

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
            return _TokenRoleClassification(
                semantic_role=TokenSemanticRole.BRAND_ACCENT,
                weight=weight,
                origin=TokenOrigin.SCALE,
            )
        return _TokenRoleClassification(
            semantic_role=TokenSemanticRole.NEUTRAL,
            weight=scale_config.base_weight,
            origin=TokenOrigin.SCALE,
        )

    # 4. Fallback.
    return _TokenRoleClassification(
        semantic_role=TokenSemanticRole.IGNORE, weight=0.0, origin=TokenOrigin.FALLBACK
    )


def _usage_intent_for_role(role: TokenSemanticRole, config: Config) -> dict[UsageRole, float]:
    """Build the usage intent for a classified token.

    Usage-intent distributions are copied verbatim (already normalized at load).
    Channel routes carry no usage weight.

    Args:
        role: The semantic role to look up a usage-intent distribution for.
        config: The loaded configuration supplying the role-to-usage-intent mapping.

    Returns:
        The per-[`UsageRole`][colorsense.UsageRole] intent distribution, or ``{}`` when the
        role has no usage intent (unmapped or a channel route).
    """
    row = config.token_vocabulary.semantic_role_to_usage_intent_or_channel.get(role)
    if row is None:
        return {}

    if isinstance(row, ChannelRoute):
        return {}

    if not isinstance(row, UsageIntent):  # pragma: no cover - defensive
        return {}

    return dict(row.distribution)


def _resolve_alias_role(
    record: TokenRecord,
    index: dict[str, TokenRecord],
    pre_alias_role_classifications: dict[str, _TokenRoleClassification],
) -> _TokenRoleClassification | None:
    """Follow ``alias_target`` links until a non-``ignore`` classification is found.

    Args:
        record: The ``ignore`` token whose alias chain is being followed.
        index: All token records keyed by name, for resolving ``alias_target`` links.
        pre_alias_role_classifications: Each token's own-merits classification, keyed by
            name (the first-pass result before alias inheritance).

    Returns:
        The inherited classification with origin rewritten to `TokenOrigin.ALIAS` (the alias
        itself was not matched, its target was), or ``None`` when the chain dead-ends
        (missing target, cycle, or all targets are ``ignore``).
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
        target_classification = pre_alias_role_classifications[target.name]
        if target_classification.semantic_role is not TokenSemanticRole.IGNORE:
            return _TokenRoleClassification(
                semantic_role=target_classification.semantic_role,
                weight=target_classification.weight,
                origin=TokenOrigin.ALIAS,
            )
        target_name = target.alias_target
    return None


def classify_tokens(tokens: list[TokenRecord], config: Config) -> list[ClassifiedToken]:
    """Classify ``tokens`` into semantic roles and usage intent.

    ``status`` tokens get an empty usage intent when ``status_excluded_from_palette`` is
    set — they still surface to consumers as [`DesignToken`][colorsense.DesignToken] entries
    with ``semantic_role=status`` when tokens are requested.

    Args:
        tokens: The declared token records harvested from the page.
        config: The loaded configuration supplying the classification vocabulary and weights.

    Returns:
        One `ClassifiedToken` per input record, in the same order.
    """
    # Index by name; later declarations win for duplicate names.
    index: dict[str, TokenRecord] = {record.name: record for record in tokens}

    # First pass: classify every record on its own merits.
    pre_alias_role_classifications: dict[str, _TokenRoleClassification] = {}
    for record in tokens:
        pre_alias_role_classifications[record.name] = _classify_role_without_alias_inheritance(
            record, config
        )

    classified_tokens: list[ClassifiedToken] = []

    for record in tokens:
        role_classification = pre_alias_role_classifications[record.name]

        # Alias inheritance: an `ignore` token may adopt its target's role.
        is_ignore = role_classification.semantic_role is TokenSemanticRole.IGNORE
        if is_ignore and record.alias_target is not None:
            inherited = _resolve_alias_role(record, index, pre_alias_role_classifications)
            if inherited is not None:
                role_classification = inherited

        role = role_classification.semantic_role
        usage_intent = _usage_intent_for_role(role, config)

        if (
            role is TokenSemanticRole.STATUS
            and config.token_vocabulary.status_excluded_from_palette
        ):
            usage_intent = {}

        classified_tokens.append(
            ClassifiedToken(
                record=record,
                semantic_role=role,
                weight=role_classification.weight,
                usage_intent=usage_intent,
                origin=role_classification.origin,
            )
        )

    return classified_tokens
