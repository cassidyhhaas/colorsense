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

from colorsense import LIGHT_AND_DARK, analyze
from colorsense.color.primitives import delta_e, parse_css_color
from colorsense.models import (
    AnalysisResult,
    Color,
    PaletteRole,
    RoleResults,
    Theme,
    ThemePalette,
    TokenSemanticRole,
)

GOLDEN_DIR = Path(__file__).parent / "golden"
FIT_SCORE_TOL = 0.05
# OKLab ΔE tolerance for rendered/screenshot-derived colors. Anti-aliasing and gamma
# differ across OSes (≈0.06 observed between macOS and Linux Chromium), so these colors
# are matched perceptually, never by exact hex. Well below cross-hue distances (~0.37).
COLOR_MATCH_TOL = 0.10

# Every test here drives a real Chromium render; skip in browserless CI.
pytestmark = pytest.mark.browser


async def _analyze(fixture: Path) -> AnalysisResult:
    # These fixtures exercise the full light+dark path (ds_site has a real dark-mode block;
    # the goldens pin both themes), so request dark explicitly — analyze defaults to light.
    return await analyze(fixture.as_uri(), themes=LIGHT_AND_DARK)


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


def _theme_structure(palette: ThemePalette) -> dict[str, Any]:
    """A compact, deterministic structural summary of one theme's reconciled palette.

    Captures, per theme:

    * ``populated_roles`` — the sorted set of :class:`PaletteRole`s that have >=1 candidate
      (purely structural: which slots the pipeline managed to fill).
    * ``top_role_colors`` — the dominant (argmax) candidate hex per populated role.

    The top-candidate hexes are screenshot-derived and so carry the usual cross-OS
    anti-aliasing/gamma drift; they are pinned here because these golden tests are
    ``browser``-marked and run against the locally installed Chromium (a fixed platform), and
    they make the goldens catch ordering/dominance regressions that the role *set* alone
    would miss. Roles are emitted in a stable, sorted order so the digest is deterministic.
    """
    mapping = palette.roles.mapping
    populated = sorted(str(role) for role, cands in mapping.items() if cands)
    top_colors = {
        str(role): cands[0].color.hex
        for role, cands in sorted(mapping.items(), key=lambda kv: str(kv[0]))
        if cands
    }
    return {"populated_roles": populated, "top_role_colors": top_colors}


def _digest(result: AnalysisResult) -> dict[str, Any]:
    """A deterministic summary of an AnalysisResult for golden comparison.

    Captures the computed-style/structural fields (token classifications, token-resolved
    status colors, theme set, fit_score) plus, for every kept theme, the populated palette
    roles and dominant role colors (see :func:`_theme_structure`) and the divergence count.
    Everything is emitted in a stable, sorted order so the digest is deterministic.
    """
    return {
        "themes": sorted(str(theme) for theme in result.themes),
        "single_theme": result.metadata.single_theme,
        "tokens": {ct.record.name: str(ct.semantic_role) for ct in result.tokens},
        "status_colors": sorted(c.hex for c in result.status_colors),
        "divergence_count": len(result.divergence),
        "theme_structure": {
            str(theme): _theme_structure(palette)
            for theme, palette in sorted(result.themes.items(), key=lambda kv: str(kv[0]))
        },
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


# ---------------------------------------------------------------------------
# Fixture 1 — token-driven design system (light + dark)
# ---------------------------------------------------------------------------


async def test_design_system_site(fixtures_dir: Path) -> None:
    result = await _analyze(fixtures_dir / "ds_site.html")

    # A real dark-mode block: both themes survive (no collapse).
    assert {str(t) for t in result.themes} == {"light", "dark"}
    assert result.metadata.single_theme is False

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
    # brand color.
    accent = result.themes[Theme.light].roles.mapping.get(PaletteRole.accent, [])
    assert accent, "expected accent candidates"
    brand_hexes = ("#2563eb", "#7c3aed", "#f59e0b")
    assert any(_color_near([accent[0].color], h) for h in brand_hexes)

    # The result is a clean Pydantic round-trip.
    assert AnalysisResult.model_validate_json(result.model_dump_json()) == result

    _check_golden("ds_site", _digest(result))


# ---------------------------------------------------------------------------
# Fixture 2 — no-token legacy site (usage-only, collapses to one theme)
# ---------------------------------------------------------------------------


async def test_legacy_site(fixtures_dir: Path) -> None:
    result = await _analyze(fixtures_dir / "legacy_site.html")

    # No dark-mode block -> identical renders -> single theme.
    assert len(result.themes) == 1
    assert result.metadata.single_theme is True

    # No custom properties: nothing to declare, so the palette is usage-driven and
    # every prominent color is "used but undeclared".
    assert result.tokens == []
    assert result.status_colors == []
    assert result.divergence  # used-but-undeclared discrepancies are reported
    assert all("undeclared" in item.note.lower() for item in result.divergence)

    _check_golden("legacy_site", _digest(result))


# ---------------------------------------------------------------------------
# Fixture 3 — card-heavy catalog with a third-party widget
# ---------------------------------------------------------------------------


async def test_cards_site(fixtures_dir: Path) -> None:
    result = await _analyze(fixtures_dir / "cards_site.html")

    assert len(result.themes) == 1

    # The vendor-prefixed `intercom-*` widget is tagged third-party and kept out of
    # the palette, but its color (~#1f8ded) is surfaced separately.
    assert _color_near(result.third_party_colors, "#1f8ded")

    # Six repeated `.product-card` siblings are detected and their shared surface
    # (~#f1f5f9) becomes a dominant palette color.
    (palette,) = result.themes.values()
    assert _color_near(_dominant_role_colors(palette.roles), "#f1f5f9")

    _check_golden("cards_site", _digest(result))
