"""Unit tests for the token classifier."""

from __future__ import annotations

import math

from colorsense.classify.tokens import classify_tokens
from colorsense.color.primitives import parse_css_color
from colorsense.config import load_default_config
from colorsense.models import (
    ClassifiedToken,
    Color,
    TokenOrigin,
    TokenRecord,
    TokenSemanticRole,
    UsageRole,
)

CONFIG = load_default_config()


def _record(
    name: str,
    raw_value: str = "#000000",
    *,
    resolved: Color | None = None,
    alias_target: str | None = None,
) -> TokenRecord:
    """Build a TokenRecord with sensible defaults for the classifier tests."""
    return TokenRecord(
        name=name,
        raw_value=raw_value,
        resolved=resolved,
        scope=":root",
        alias_target=alias_target,
    )


def _by_name(classified: list[ClassifiedToken], name: str) -> ClassifiedToken:
    """Return the single ClassifiedToken whose record has the given name."""
    matches = [c for c in classified if c.record.name == name]
    assert len(matches) == 1, f"expected exactly one token named {name!r}"
    return matches[0]


def _get_usage_role_with_largest_probability_mass(
    usage_intent: dict[UsageRole, float],
) -> UsageRole:
    assert usage_intent, "usage_intent is empty"
    return max(usage_intent, key=lambda role: usage_intent[role])


def test_color_primary_is_cta_dominant() -> None:
    """--color-primary strips to 'primary' -> brand_primary -> cta-dominant usage intent."""
    classified = classify_tokens([_record("--color-primary")], CONFIG)
    token = _by_name(classified, "--color-primary")
    assert token.semantic_role is TokenSemanticRole.brand_primary
    assert token.origin is TokenOrigin.name_rule
    assert _get_usage_role_with_largest_probability_mass(token.usage_intent) is UsageRole.cta


def test_gray_scale_gets_plain_neutral_usage_intent() -> None:
    """A gray scale token is neutral with the plain YAML usage_intent
    (no lightness special-case).

    The neutral usage_intent spans page/surface/banner/text/border (the
    old surface mass split across the three background roles) — the resolved
    lightness no longer reroutes it.
    """
    light = parse_css_color("#f3f4f6")
    dark = parse_css_color("#111827")
    assert light is not None and dark is not None
    classified_tokens = classify_tokens(
        [
            _record("--gray-100", "#f3f4f6", resolved=light),
            _record("--gray-900", "#111827", resolved=dark),
        ],
        CONFIG,
    )
    for name in ("--gray-100", "--gray-900"):
        token = _by_name(classified_tokens, name)
        assert token.semantic_role is TokenSemanticRole.neutral
        # "gray" is a name rule, which outranks scale detection in the precedence.
        assert token.origin is TokenOrigin.name_rule
        assert set(token.usage_intent) == {
            UsageRole.page,
            UsageRole.surface,
            UsageRole.banner,
            UsageRole.text,
            UsageRole.border,
        }
        # Text carries the most neutral mass in the remapped usage intent
        assert _get_usage_role_with_largest_probability_mass(token.usage_intent) is UsageRole.text
    # Light and dark resolve to the SAME usage intent now: no measured-lightness rerouting.
    assert (
        _by_name(classified_tokens, "--gray-100").usage_intent
        == _by_name(classified_tokens, "--gray-900").usage_intent
    )


def test_destructive_is_status_with_empty_usage_intent() -> None:
    """--destructive -> status: empty usage intent (status_excluded_from_palette)."""
    red = parse_css_color("#ef4444")
    assert red is not None
    classified = classify_tokens([_record("--destructive", "#ef4444", resolved=red)], CONFIG)
    token = _by_name(classified, "--destructive")
    assert token.semantic_role is TokenSemanticRole.status
    assert token.usage_intent == {}
    # Still classified (not dropped): it surfaces to consumers via DesignToken.
    assert token.record.resolved == red


def test_alias_inherits_brand_accent_with_alias_origin() -> None:
    """A token that self-classifies as ignore inherits its alias target's role.

    The aliasing token must NOT match a name rule on its own (otherwise that rule
    wins per the spec precedence), so we use an opaque name that self-classifies to
    ignore; it then inherits brand_accent from --accent and an interactive-dominant
    usage intent — but carries origin ``alias`` (the alias itself was never matched).
    """
    # Sanity: the aliasing name self-classifies to ignore on its own.
    solo = classify_tokens([_record("--zxqw")], CONFIG)
    assert solo[0].semantic_role is TokenSemanticRole.ignore

    tokens = [
        _record("--accent"),
        _record("--zxqw", "var(--accent)", alias_target="--accent"),
    ]
    classified = classify_tokens(tokens, CONFIG)
    aliased = _by_name(classified, "--zxqw")
    assert aliased.semantic_role is TokenSemanticRole.brand_accent
    assert aliased.origin is TokenOrigin.alias
    assert _get_usage_role_with_largest_probability_mass(aliased.usage_intent) is UsageRole.cta
    # The target itself keeps its own (name_rule) origin.
    assert _by_name(classified, "--accent").origin is TokenOrigin.name_rule


def test_relational_text_on_classification() -> None:
    """--on-primary routes to text_on with an empty usage intent and relational origin."""
    classified = classify_tokens([_record("--on-primary")], CONFIG)
    token = _by_name(classified, "--on-primary")
    assert token.semantic_role is TokenSemanticRole.text_on
    assert token.origin is TokenOrigin.relational
    assert token.usage_intent == {}


def test_chromatic_scale_origin_is_scale() -> None:
    """--blue-500 -> brand_accent via the scale detector, origin ``scale``."""
    classified = classify_tokens([_record("--blue-500")], CONFIG)
    token = _by_name(classified, "--blue-500")
    assert token.semantic_role is TokenSemanticRole.brand_accent
    assert token.origin is TokenOrigin.scale
    assert _get_usage_role_with_largest_probability_mass(token.usage_intent) is UsageRole.cta


def test_neutral_scale_family_origin_is_scale() -> None:
    """--sand-100: a neutral scale family with no name rule -> neutral via scale."""
    classified = classify_tokens([_record("--sand-100")], CONFIG)
    token = _by_name(classified, "--sand-100")
    assert token.semantic_role is TokenSemanticRole.neutral
    assert token.origin is TokenOrigin.scale


def test_unmatched_token_is_ignored_with_fallback_origin() -> None:
    """A name with no rule/scale/relational match falls back to ignore."""
    classified = classify_tokens([_record("--zxqw")], CONFIG)
    token = _by_name(classified, "--zxqw")
    assert token.semantic_role is TokenSemanticRole.ignore
    assert token.origin is TokenOrigin.fallback
    assert token.weight == 0.0
    assert token.usage_intent == {}


def test_alias_cycle_does_not_hang() -> None:
    """A two-token alias cycle resolves to ignore without infinite recursion."""
    tokens = [
        _record("--a", alias_target="--b"),
        _record("--b", alias_target="--a"),
    ]
    classified = classify_tokens(tokens, CONFIG)
    assert _by_name(classified, "--a").semantic_role is TokenSemanticRole.ignore
    assert _by_name(classified, "--b").semantic_role is TokenSemanticRole.ignore
    # The chain dead-ended: the classification stays the fallback, origin included.
    assert _by_name(classified, "--a").origin is TokenOrigin.fallback


def test_usage_intent_table_sanity() -> None:
    """Spot-check the role -> usage-intent table through real classifications."""
    classified_tokens = classify_tokens(
        [
            _record("--background"),  # surface_base
            _record("--text"),  # text_body
            _record("--border"),  # border
            _record("--link"),  # interactive
        ],
        CONFIG,
    )
    # surface_base now leans the page canvas (the old surface mass split across roles).
    background_usage_intent = _by_name(classified_tokens, "--background").usage_intent
    assert set(background_usage_intent) == {
        UsageRole.page,
        UsageRole.surface,
        UsageRole.banner,
    }
    assert _get_usage_role_with_largest_probability_mass(background_usage_intent) is UsageRole.page
    assert _by_name(classified_tokens, "--text").usage_intent == {UsageRole.text: 1.0}
    assert _by_name(classified_tokens, "--border").usage_intent == {UsageRole.border: 1.0}
    # interactive (--link) splits across cta/link/action, cta-dominant.
    link_usage_intent = _by_name(classified_tokens, "--link").usage_intent
    assert set(link_usage_intent) == {UsageRole.cta, UsageRole.link, UsageRole.action}
    assert _get_usage_role_with_largest_probability_mass(link_usage_intent) is UsageRole.cta


def test_all_nonempty_distributions_sum_to_one() -> None:
    """Every non-empty usage intent must sum to ~1.0 (abs tol 1e-6)."""
    light = parse_css_color("#f3f4f6")
    dark = parse_css_color("#111827")
    red = parse_css_color("#ef4444")
    assert light is not None and dark is not None and red is not None
    tokens = [
        _record("--color-primary"),
        _record("--color-secondary"),
        _record("--accent"),
        _record("--link"),
        _record("--background"),
        _record("--card"),
        _record("--text"),
        _record("--border"),
        _record("--gray-100", resolved=light),
        _record("--gray-900", resolved=dark),
        _record("--blue-500"),
        _record("--destructive", resolved=red),
        _record("--on-primary"),
        _record("--zxqw"),
    ]
    classified = classify_tokens(tokens, CONFIG)
    for token in classified:
        if token.usage_intent:
            total = math.fsum(token.usage_intent.values())
            assert math.isclose(total, 1.0, abs_tol=1e-6), (
                f"{token.record.name} usage intent sums to {total}"
            )
