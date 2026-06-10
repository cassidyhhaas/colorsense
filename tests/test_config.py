"""Unit tests for the config loader."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from colorsense.config import (
    ChannelPrior,
    Config,
    DistributionPrior,
    RelationalInfo,
    ScaleInfo,
    load_config,
    load_default_config,
)
from colorsense.models import TokenSemanticRole


@pytest.fixture
def config() -> Config:
    """The real, loaded palette config (the one bundled with the package)."""
    return load_default_config()


def test_load_returns_typed_config(config: Config) -> None:
    assert isinstance(config, Config)
    assert config.token_vocabulary.namespace_prefixes
    assert config.component_classifier.component_types


def test_distribution_rows_normalized_and_channels_recognized(config: Config) -> None:
    priors = config.token_vocabulary.role_to_palette_prior

    channel_roles = {
        TokenSemanticRole.text_on,
        TokenSemanticRole.status,
        TokenSemanticRole.ignore,
    }
    for role, row in priors.items():
        if role in channel_roles:
            assert isinstance(row, ChannelPrior)
            assert row.channel == role.value
        else:
            assert isinstance(row, DistributionPrior)
            total = sum(row.distribution.values())
            assert total == pytest.approx(1.0, abs=1e-6)


def test_match_name_rule_applies_known_system_boost(config: Config) -> None:
    result = config.match_name_rule("--bs-primary")
    assert result is not None
    role, weight = result
    assert role is TokenSemanticRole.brand_primary
    assert weight == pytest.approx(5.0 * 1.25)


def test_match_name_rule_no_boost_without_system_prefix(config: Config) -> None:
    result = config.match_name_rule("--primary")
    assert result is not None
    role, weight = result
    assert role is TokenSemanticRole.brand_primary
    assert weight == pytest.approx(5.0)


def test_match_relational_foreground(config: Config) -> None:
    result = config.match_relational("--primary-foreground")
    assert isinstance(result, RelationalInfo)
    assert result.type == "text_on"
    assert result.base == "primary"


def test_match_relational_none_when_no_modifier(config: Config) -> None:
    assert config.match_relational("--primary") is None


def test_detect_scale_chromatic(config: Config) -> None:
    info = config.detect_scale("--blue-500")
    assert isinstance(info, ScaleInfo)
    assert info.family == "blue"
    assert info.number == 500
    assert info.is_chromatic is True
    assert info.is_anchor is True


def test_detect_scale_neutral(config: Config) -> None:
    info = config.detect_scale("--gray-100")
    assert isinstance(info, ScaleInfo)
    assert info.family == "gray"
    assert info.number == 100
    assert info.is_chromatic is False


def test_detect_scale_none_without_number(config: Config) -> None:
    assert config.detect_scale("--blue") is None


def test_detect_scale_anchor_any_convention(config: Config) -> None:
    """A number is an anchor if it falls within ANY convention's range."""
    radix = config.detect_scale("--blue-9")
    assert isinstance(radix, ScaleInfo)
    assert radix.is_anchor is True  # radix [9, 10]

    tailwind = config.detect_scale("--blue-500")
    assert isinstance(tailwind, ScaleInfo)
    assert tailwind.is_anchor is True  # tailwind [500, 700]

    neither = config.detect_scale("--blue-50")
    assert isinstance(neither, ScaleInfo)
    assert neither.is_anchor is False

    neutral_radix = config.detect_scale("--gray-10")
    assert isinstance(neutral_radix, ScaleInfo)
    assert neutral_radix.is_anchor is True  # radix range applies to neutrals too


def test_detect_scale_unknown_family_never_anchor(config: Config) -> None:
    """An unknown family gets a scale number but is never an anchor."""
    info = config.detect_scale("--foo-500")
    assert isinstance(info, ScaleInfo)
    assert info.family == "foo"
    assert info.is_chromatic is False
    assert info.is_anchor is False


def test_strip_namespace(config: Config) -> None:
    assert config.strip_namespace("--bs-primary") == "primary"
    assert config.strip_namespace("--theme-color") == "theme"


def test_layout_noise_class_tokens_present(config: Config) -> None:
    rules = config.component_classifier.class_tokens
    container = next((r for r in rules if r.match == "container"), None)
    assert container is not None
    assert container.votes.get("page_bg") == pytest.approx(0.5)


def test_default_config_loads_from_package() -> None:
    """The bundled config resolves via the package, independent of the working dir."""
    config = load_default_config()
    assert isinstance(config, Config)
    # A representative value proves the YAML was actually parsed, not just located.
    assert config.token_vocabulary.name_rules


def test_invalid_config_raises_clear_error(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "token_vocabulary:\n"
        "  namespace_prefixes: ['c-']\n"
        "# missing component_classifier and most required keys\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError) as excinfo:
        load_config(bad)
    # Must be a clear validation error, never a bare KeyError.
    assert not isinstance(excinfo.value, KeyError)
    assert "component_classifier" in str(excinfo.value)
