"""Unit tests for WP5 — the token classifier."""

from __future__ import annotations

import math
from pathlib import Path

from colorsense.classify.tokens import classify_tokens
from colorsense.color.primitives import parse_css_color
from colorsense.config import load_config
from colorsense.models import (
    ClassifiedToken,
    Color,
    PaletteRole,
    TokenRecord,
    TokenSemanticRole,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG = load_config(REPO_ROOT / "config" / "palette_config.yaml")


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


def _argmax_role(prior: dict[PaletteRole, float]) -> PaletteRole:
    """Return the palette role with the largest probability mass."""
    assert prior, "palette_prior is empty"
    return max(prior, key=lambda role: prior[role])


def test_color_primary_is_accent_dominant() -> None:
    """--color-primary strips to 'primary' -> brand_primary -> accent-dominant."""
    classified, _status = classify_tokens([_record("--color-primary")], CONFIG)
    token = _by_name(classified, "--color-primary")
    assert token.semantic_role is TokenSemanticRole.brand_primary
    assert _argmax_role(token.palette_prior) is PaletteRole.accent


def test_light_gray_scale_is_neutral_light() -> None:
    """A LIGHT resolved gray scale token splits to neutral_light dominance."""
    light = parse_css_color("#f3f4f6")
    assert light is not None
    assert light.lightness >= 0.5
    classified, _status = classify_tokens(
        [_record("--gray-100", "#f3f4f6", resolved=light)], CONFIG
    )
    token = _by_name(classified, "--gray-100")
    assert token.semantic_role is TokenSemanticRole.neutral
    assert _argmax_role(token.palette_prior) is PaletteRole.neutral_light


def test_dark_gray_scale_is_neutral_dark() -> None:
    """A DARK resolved gray scale token splits to neutral_dark dominance."""
    dark = parse_css_color("#111827")
    assert dark is not None
    assert dark.lightness < 0.5
    classified, _status = classify_tokens([_record("--gray-900", "#111827", resolved=dark)], CONFIG)
    token = _by_name(classified, "--gray-900")
    assert token.semantic_role is TokenSemanticRole.neutral
    assert _argmax_role(token.palette_prior) is PaletteRole.neutral_dark


def test_neutral_light_dark_ordering() -> None:
    """The light gray must be lighter than the dark gray (sanity on inputs)."""
    light = parse_css_color("#f3f4f6")
    dark = parse_css_color("#111827")
    assert light is not None and dark is not None
    assert light.lightness > dark.lightness


def test_destructive_is_status_and_excluded() -> None:
    """--destructive -> status: empty prior, color routed to status list."""
    red = parse_css_color("#ef4444")
    assert red is not None
    classified, status_colors = classify_tokens(
        [_record("--destructive", "#ef4444", resolved=red)], CONFIG
    )
    token = _by_name(classified, "--destructive")
    assert token.semantic_role is TokenSemanticRole.status
    assert token.palette_prior == {}
    assert red in status_colors


def test_status_with_no_resolved_color_skipped() -> None:
    """A status token without a resolved color contributes nothing to the list."""
    classified, status_colors = classify_tokens([_record("--error")], CONFIG)
    token = _by_name(classified, "--error")
    assert token.semantic_role is TokenSemanticRole.status
    assert status_colors == []


def test_alias_inherits_brand_accent() -> None:
    """A token that self-classifies as ignore inherits its alias target's role.

    The aliasing token must NOT match a name rule on its own (otherwise that rule
    wins per the spec precedence), so we use an opaque name that self-classifies to
    ignore; it then inherits brand_accent from --accent and an accent-dominant prior.
    """
    # Sanity: the aliasing name self-classifies to ignore on its own.
    solo, _ = classify_tokens([_record("--zxqw")], CONFIG)
    assert solo[0].semantic_role is TokenSemanticRole.ignore

    tokens = [
        _record("--accent"),
        _record("--zxqw", "var(--accent)", alias_target="--accent"),
    ]
    classified, _status = classify_tokens(tokens, CONFIG)
    aliased = _by_name(classified, "--zxqw")
    assert aliased.semantic_role is TokenSemanticRole.brand_accent
    assert _argmax_role(aliased.palette_prior) is PaletteRole.accent


def test_relational_text_on_carries_base_role() -> None:
    """--on-primary routes to text_on with empty prior and a base role."""
    classified, _status = classify_tokens([_record("--on-primary")], CONFIG)
    token = _by_name(classified, "--on-primary")
    assert token.semantic_role is TokenSemanticRole.text_on
    assert token.palette_prior == {}
    assert token.text_on_base is TokenSemanticRole.brand_primary


def test_unmatched_token_is_ignored() -> None:
    """A name with no rule/scale/relational match falls back to ignore."""
    classified, _status = classify_tokens([_record("--zxqw")], CONFIG)
    token = _by_name(classified, "--zxqw")
    assert token.semantic_role is TokenSemanticRole.ignore
    assert token.weight == 0.0
    assert token.palette_prior == {}


def test_alias_cycle_does_not_hang() -> None:
    """A two-token alias cycle resolves to ignore without infinite recursion."""
    tokens = [
        _record("--a", alias_target="--b"),
        _record("--b", alias_target="--a"),
    ]
    classified, _status = classify_tokens(tokens, CONFIG)
    assert _by_name(classified, "--a").semantic_role is TokenSemanticRole.ignore
    assert _by_name(classified, "--b").semantic_role is TokenSemanticRole.ignore


def test_all_nonempty_priors_sum_to_one() -> None:
    """Every non-empty palette_prior must sum to ~1.0 (abs tol 1e-6)."""
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
    classified, _status = classify_tokens(tokens, CONFIG)
    for token in classified:
        if token.palette_prior:
            total = math.fsum(token.palette_prior.values())
            assert math.isclose(total, 1.0, abs_tol=1e-6), (
                f"{token.record.name} prior sums to {total}"
            )
