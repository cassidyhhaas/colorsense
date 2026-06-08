"""End-to-end integration tests over representative fixture sites.

Three saved HTML pages stand in for real-world archetypes (no public network):

* ``ds_site.html``     — a token-driven design system (light + dark).
* ``legacy_site.html`` — a no-token legacy site (usage-only, single theme).
* ``cards_site.html``  — a card-heavy catalog with a third-party chat widget.

Each test makes two kinds of assertion. **Invariants** are hand-checked claims about
what the analysis *must* say (a CTA token classifies as interactive, status colors are
segregated, a vendor widget is tagged third-party, …). **Golden snapshots** pin a digest
of the full result so accidental regressions surface; structural fields and color hexes are
compared exactly while probabilities use a tolerance (the spec calls for ordering/dominance,
not exact floats). Regenerate goldens with ``UPDATE_GOLDEN=1 uv run pytest``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from colorsense import analyze
from colorsense.color.primitives import delta_e, parse_css_color
from colorsense.models import (
    AnalysisResult,
    Color,
    PaletteRole,
    RoleResults,
    Theme,
    TokenSemanticRole,
)

CONFIG_PATH = str(Path(__file__).resolve().parents[1] / "config" / "palette_config.yaml")
GOLDEN_DIR = Path(__file__).parent / "golden"
CONTRAST_EPS = 1e-6
FIT_SCORE_TOL = 0.05
# OKLab ΔE tolerance for rendered/screenshot-derived colors. Anti-aliasing and gamma
# differ across OSes (≈0.06 observed between macOS and Linux Chromium), so these colors
# are matched perceptually, never by exact hex. Well below cross-hue distances (~0.37).
COLOR_MATCH_TOL = 0.10

# Every test here drives a real Chromium render; skip in browserless CI.
pytestmark = pytest.mark.browser


def _analyze(fixture: Path) -> AnalysisResult:
    return analyze(fixture.as_uri(), config_path=CONFIG_PATH)


# ---------------------------------------------------------------------------
# Golden-snapshot machinery
# ---------------------------------------------------------------------------


def _dominant_role_colors(roles: RoleResults) -> list[Color]:
    """The argmax (dominant) candidate color per role."""
    return [candidates[0].color for candidates in roles.mapping.values() if candidates]


def _color_near(colors: list[Color], target_hex: str) -> bool:
    """Whether any of ``colors`` is within :data:`COLOR_MATCH_TOL` ΔE of ``target_hex``."""
    target = parse_css_color(target_hex)
    assert target is not None, target_hex
    return any(delta_e(color, target) <= COLOR_MATCH_TOL for color in colors)


def _digest(result: AnalysisResult) -> dict[str, Any]:
    """A deterministic, *platform-stable* summary of an AnalysisResult.

    Only fields that derive from computed style or structure are captured: token
    classifications, token-resolved status colors, theme set, and fit_score. Rendered
    (screenshot-derived) colors — role candidates and recommendations — are NOT portable
    across OSes, so they are excluded here and asserted perceptually in each test instead.
    """
    return {
        "themes": sorted(str(theme) for theme in result.themes),
        "single_theme": result.metadata["single_theme"],
        "tokens": {ct.record.name: str(ct.semantic_role) for ct in result.tokens},
        "status_colors": sorted(c.hex for c in result.status_colors),
        "fit_score": round(result.fit_score, 4),
    }


def _check_golden(name: str, digest: dict[str, Any]) -> None:
    """Assert ``digest`` matches the stored golden, regenerating it on demand.

    ``fit_score`` is compared within :data:`FIT_SCORE_TOL`; the remaining fields (themes,
    token classifications, token-resolved status colors) are computed-style/structural and
    compared exactly.
    """
    path = GOLDEN_DIR / f"{name}.json"
    if os.environ.get("UPDATE_GOLDEN") or not path.exists():
        GOLDEN_DIR.mkdir(exist_ok=True)
        path.write_text(json.dumps(digest, indent=2, sort_keys=True) + "\n")
        return

    expected = json.loads(path.read_text())

    actual_fit = digest.pop("fit_score")
    expected_fit = expected.pop("fit_score")
    assert actual_fit == pytest.approx(expected_fit, abs=FIT_SCORE_TOL), (
        f"{name}: fit_score {actual_fit} drifted from golden {expected_fit}"
    )
    assert digest == expected, f"{name}: digest diverged from golden (run UPDATE_GOLDEN=1)"


def _assert_recommendation_safe(result: AnalysisResult) -> None:
    """Every theme's recommendation meets the recommendation engine's WCAG guarantees."""
    for palette in result.themes.values():
        contrast = palette.recommendation.contrast
        assert contrast["heading_text_on_heading_bg"] >= 4.5 - CONTRAST_EPS
        assert contrast["cta_text_on_cta_bg"] >= 4.5 - CONTRAST_EPS
        assert contrast["heading_bg_on_page"] >= 3.0 - CONTRAST_EPS
        assert contrast["cta_bg_on_page"] >= 3.0 - CONTRAST_EPS


# ---------------------------------------------------------------------------
# Fixture 1 — token-driven design system (light + dark)
# ---------------------------------------------------------------------------


def test_design_system_site(fixtures_dir: Path) -> None:
    result = _analyze(fixtures_dir / "ds_site.html")

    # A real dark-mode block: both themes survive (no collapse).
    assert {str(t) for t in result.themes} == {"light", "dark"}
    assert result.metadata["single_theme"] == "false"

    # Tokens classify by name, with the `--color-` namespace stripped first.
    semantic = {ct.record.name: ct.semantic_role for ct in result.tokens}
    assert semantic["--color-primary"] is TokenSemanticRole.brand_primary
    assert semantic["--color-secondary"] is TokenSemanticRole.brand_secondary
    assert semantic["--color-accent"] is TokenSemanticRole.brand_accent
    assert semantic["--color-on-primary"] is TokenSemanticRole.text_on
    assert semantic["--color-bg"] is TokenSemanticRole.surface_base
    assert semantic["--color-border"] is TokenSemanticRole.border

    # Status colors are segregated out of the palette and reported separately.
    assert {c.hex for c in result.status_colors} == {"#16a34a", "#dc2626"}
    assert semantic["--color-success"] is TokenSemanticRole.status
    assert not result.third_party_colors

    # A token-driven site agrees well between declared intent and measured usage.
    assert result.fit_score > 0.6

    # Dominance (perceptual, platform-robust): the accent role is led by a declared
    # brand color, and the recommended CTA is chromatic rather than a neutral.
    accent = result.themes[Theme.light].roles.mapping.get(PaletteRole.accent, [])
    assert accent, "expected accent candidates"
    brand_hexes = ("#2563eb", "#7c3aed", "#f59e0b")
    assert any(_color_near([accent[0].color], h) for h in brand_hexes)
    assert result.themes[Theme.light].recommendation.cta_bg.chroma > 0.05

    _assert_recommendation_safe(result)

    # The result is a clean Pydantic round-trip.
    assert AnalysisResult.model_validate_json(result.model_dump_json()) == result

    _check_golden("ds_site", _digest(result))


# ---------------------------------------------------------------------------
# Fixture 2 — no-token legacy site (usage-only, collapses to one theme)
# ---------------------------------------------------------------------------


def test_legacy_site(fixtures_dir: Path) -> None:
    result = _analyze(fixtures_dir / "legacy_site.html")

    # No dark-mode block -> identical renders -> single theme.
    assert len(result.themes) == 1
    assert result.metadata["single_theme"] == "true"

    # No custom properties: nothing to declare, so the palette is usage-driven and
    # every prominent color is "used but undeclared".
    assert result.tokens == []
    assert result.status_colors == []
    assert result.divergence  # used-but-undeclared discrepancies are reported
    assert all("undeclared" in item.note.lower() for item in result.divergence)

    _assert_recommendation_safe(result)
    _check_golden("legacy_site", _digest(result))


# ---------------------------------------------------------------------------
# Fixture 3 — card-heavy catalog with a third-party widget
# ---------------------------------------------------------------------------


def test_cards_site(fixtures_dir: Path) -> None:
    result = _analyze(fixtures_dir / "cards_site.html")

    assert len(result.themes) == 1

    # The vendor-prefixed `intercom-*` widget is tagged third-party and kept out of
    # the palette, but its color (~#1f8ded) is surfaced separately.
    assert _color_near(result.third_party_colors, "#1f8ded")

    # Six repeated `.product-card` siblings are detected and their shared surface
    # (~#f1f5f9) becomes a dominant palette color.
    (palette,) = result.themes.values()
    assert _color_near(_dominant_role_colors(palette.roles), "#f1f5f9")

    _assert_recommendation_safe(result)
    _check_golden("cards_site", _digest(result))
