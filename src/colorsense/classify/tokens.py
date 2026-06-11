"""Token classifier.

Classify declared design tokens (CSS custom properties) into semantic roles and
produce, for each token, a prior distribution over the usage categories
(:class:`~colorsense.models.UsageCategory`) — how the token's color is expected to be
used when rendered.

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

Every classified token records its :class:`~colorsense.models.TokenOrigin` — which of
the paths above produced it (alias-inherited classifications carry ``alias``).
Reconciliation uses the origin to gate declared-but-unused divergence to high-intent
tokens only.

The public entry point is :func:`classify_tokens`.
"""

from __future__ import annotations

from colorsense.config import ChannelPrior, Config, DistributionPrior
from colorsense.models import (
    ClassifiedToken,
    TokenOrigin,
    TokenRecord,
    TokenSemanticRole,
    UsageCategory,
)

__all__ = ["classify_tokens"]

# Base weight for a numbered-scale match before any anchor boost is applied.
_SCALE_BASE_WEIGHT: float = 3.0

# The (role, weight, text_on_base, origin) classification quadruple.
_SelfClass = tuple[TokenSemanticRole, float, TokenSemanticRole | None, TokenOrigin]


def _classify_self(record: TokenRecord, config: Config) -> _SelfClass:
    """Classify a single record on its own merits (no alias inheritance).

    Returns ``(semantic_role, weight, text_on_base, origin)``. ``text_on_base`` is only
    populated for relational (``text_on``) tokens; it is ``None`` otherwise.
    """
    name = record.name

    # 1. Relational (text-on-<base>) takes precedence.
    relational = config.match_relational(name)
    if relational is not None:
        base_match = config.match_name_rule("--" + relational.base)
        text_on_base = base_match[0] if base_match is not None else None
        return TokenSemanticRole.text_on, relational.weight, text_on_base, TokenOrigin.relational

    # 2. Direct name rule.
    name_match = config.match_name_rule(name)
    if name_match is not None:
        role, weight = name_match
        return role, weight, None, TokenOrigin.name_rule

    # 3. Numbered-scale detection.
    scale = config.detect_scale(name)
    if scale is not None:
        if scale.is_chromatic:
            weight = _SCALE_BASE_WEIGHT
            if scale.is_anchor:
                boost = config.token_vocabulary.scale_detection.scale_present_confidence_boost
                weight *= boost
            return TokenSemanticRole.brand_accent, weight, None, TokenOrigin.scale
        return TokenSemanticRole.neutral, _SCALE_BASE_WEIGHT, None, TokenOrigin.scale

    # 4. Fallback.
    return TokenSemanticRole.ignore, 0.0, None, TokenOrigin.fallback


def _usage_prior(role: TokenSemanticRole, config: Config) -> dict[UsageCategory, float]:
    """Build the usage-category prior for a classified token.

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
    self_class: dict[str, _SelfClass],
) -> _SelfClass | None:
    """Follow ``alias_target`` links until a non-``ignore`` classification is found.

    Returns the inherited classification with origin rewritten to
    :attr:`TokenOrigin.alias` (the alias itself was not matched, its target was), or
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
        role, weight, text_on_base, _origin = self_class[target.name]
        if role is not TokenSemanticRole.ignore:
            return role, weight, text_on_base, TokenOrigin.alias
        target_name = target.alias_target
    return None


def classify_tokens(tokens: list[TokenRecord], config: Config) -> list[ClassifiedToken]:
    """Classify ``tokens`` into semantic roles and usage-category priors.

    Returns one :class:`ClassifiedToken` per input record (order preserved). ``status``
    tokens get an empty prior when ``status_excluded_from_palette`` is set — they still
    surface to consumers as :class:`~colorsense.models.DesignToken` entries with
    ``semantic_role=status`` when tokens are requested.
    """
    # Index by name; later declarations win for duplicate names.
    index: dict[str, TokenRecord] = {record.name: record for record in tokens}

    # First pass: classify every record on its own merits.
    self_class: dict[str, _SelfClass] = {}
    for record in tokens:
        self_class[record.name] = _classify_self(record, config)

    status_excluded = config.token_vocabulary.status_excluded_from_palette

    classified: list[ClassifiedToken] = []

    for record in tokens:
        role, weight, text_on_base, origin = self_class[record.name]

        # Alias inheritance: an `ignore` token may adopt its target's role.
        if role is TokenSemanticRole.ignore and record.alias_target is not None:
            inherited = _resolve_alias_role(record, index, self_class)
            if inherited is not None:
                role, weight, text_on_base, origin = inherited

        prior = _usage_prior(role, config)

        if role is TokenSemanticRole.status and status_excluded:
            prior = {}

        classified.append(
            ClassifiedToken(
                record=record,
                semantic_role=role,
                weight=weight,
                usage_prior=prior,
                text_on_base=text_on_base,
                origin=origin,
            )
        )

    return classified
