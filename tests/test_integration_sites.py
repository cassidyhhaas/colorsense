"""End-to-end integration tests over representative fixture sites.

Three saved HTML pages stand in for real-world archetypes (no public network):

* ``ds_site.html``     — a token-driven design system (light + dark).
* ``legacy_site.html`` — a no-token legacy site (usage-only, single theme).
* ``cards_site.html``  — a card-heavy catalog with a third-party chat widget.

Each test makes two kinds of assertion. **Invariants** are hand-checked claims about
what the analysis *must* say (a CTA token classifies as interactive, status tokens are
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
from conftest import file_policy

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
    UsageCategory,
)

GOLDEN_DIR = Path(__file__).parent / "golden"
FIT_SCORE_TOL = 0.05
# OKLab ΔE tolerance for rendered/screenshot-derived colors. Anti-aliasing and gamma
# differ across OSes (≈0.06 observed between macOS and Linux Chromium), so these colors
# are matched perceptually, never by exact hex. Well below cross-hue distances (~0.37).
COLOR_MATCH_TOL = 0.10

# The site tests drive a real Chromium render and are marked ``browser`` individually
# (not via module-level ``pytestmark``) so the browserless unit tests of the golden
# helper at the bottom of this file still run under ``-m "not browser"``.


async def _analyze(fixture: Path) -> AnalysisResult:
    # These fixtures exercise the full light+dark path (ds_site has a real dark-mode block;
    # the goldens pin both themes), so request dark explicitly — analyze defaults to light.
    # file:// fixtures require the explicit allow_file_urls opt-in.
    return await analyze(
        fixture.as_uri(), themes=LIGHT_AND_DARK, politeness=file_policy(), include_tokens=True
    )


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
      (purely structural: which slots the pipeline managed to fill). Compared exactly.
    * ``top_role_colors`` — the dominant (argmax) candidate hex per populated role.

    * ``populated_usage`` / ``top_usage_colors`` — the same two signals for the primary,
      usage-keyed view (which usage categories were filled, and the dominant hex each).

    The top-entry hexes are screenshot-derived and carry cross-OS anti-aliasing/gamma
    drift (≈0.01 ΔE observed between macOS and Linux Chromium), so the golden stores a
    reference hex but :func:`_check_golden` compares them *perceptually* within
    :data:`COLOR_MATCH_TOL`, never by exact string — this catches dominance/color
    regressions without flaking across the OS the goldens were generated on. Roles and
    categories are emitted in a stable, sorted order so the digest is deterministic.
    """
    mapping = palette.roles.mapping
    populated = sorted(str(role) for role, cands in mapping.items() if cands)
    top_colors = {
        str(role): cands[0].color.hex
        for role, cands in sorted(mapping.items(), key=lambda kv: str(kv[0]))
        if cands
    }
    usage_mapping = palette.usage.mapping
    populated_usage = sorted(str(cat) for cat, entries in usage_mapping.items() if entries)
    top_usage_colors = {
        str(cat): entries[0].color.hex
        for cat, entries in sorted(usage_mapping.items(), key=lambda kv: str(kv[0]))
        if entries
    }
    return {
        "populated_roles": populated,
        "top_role_colors": top_colors,
        "populated_usage": populated_usage,
        "top_usage_colors": top_usage_colors,
    }


def _digest(result: AnalysisResult) -> dict[str, Any]:
    """A deterministic summary of an AnalysisResult for golden comparison.

    Captures the computed-style/structural fields (token classifications from the primary
    theme, theme set, the primary theme's fit_score) plus, for every kept theme, the
    populated usage categories / palette roles and their dominant colors (see
    :func:`_theme_structure`) and whether divergence was reported. Everything is emitted
    in a stable, sorted order so the digest is deterministic.

    ``has_divergence`` is a bool rather than an exact count: the count of used-but-undeclared
    discrepancies can shift by one across OSes when a borderline cluster crosses the
    threshold (4 vs 3 observed), so only the stable presence/absence signal is pinned.
    """
    primary = result.themes[result.metadata.themes_analyzed[0]]
    tokens = primary.tokens if primary.tokens is not None else ()
    return {
        "themes": sorted(str(theme) for theme in result.themes),
        "single_theme": len(result.metadata.themes_analyzed) == 1,
        "tokens": {t.name: str(t.semantic_role) for t in tokens},
        "has_divergence": any(bool(p.divergence) for p in result.themes.values()),
        "theme_structure": {
            str(theme): _theme_structure(palette)
            for theme, palette in sorted(result.themes.items(), key=lambda kv: str(kv[0]))
        },
        "fit_score": round(primary.fit_score, 4),
    }


def _check_golden(name: str, digest: dict[str, Any], golden_dir: Path = GOLDEN_DIR) -> None:
    """Assert ``digest`` matches the stored golden, regenerating it on demand.

    A golden is (re)written only under ``UPDATE_GOLDEN``; a *missing* golden without
    the env var fails loudly instead of silently self-creating-and-passing (which
    would let a renamed/deleted golden turn the test vacuous). ``golden_dir`` is
    parameterized (default: the real goldens dir) so the helper itself is unit-testable.

    Three comparison modes, by field stability:

    * ``fit_score`` — within :data:`FIT_SCORE_TOL`.
    * ``theme_structure`` — ``populated_roles`` / ``populated_usage`` exact;
      ``top_role_colors`` / ``top_usage_colors`` perceptually, within
      :data:`COLOR_MATCH_TOL` ΔE (screenshot-derived hexes drift across OSes).
    * everything else (themes, token classifications, ``single_theme``,
      ``has_divergence``) — computed-style/structural, compared exactly.
    """
    path = golden_dir / f"{name}.json"
    if os.environ.get("UPDATE_GOLDEN"):
        golden_dir.mkdir(exist_ok=True)
        path.write_text(json.dumps(digest, indent=2, sort_keys=True) + "\n")
        return
    if not path.exists():
        pytest.fail(
            f"golden snapshot {path} is missing; regenerate it with "
            f"`UPDATE_GOLDEN=1 uv run pytest tests/test_integration_sites.py`"
        )

    expected = json.loads(path.read_text())

    actual_fit = digest.pop("fit_score")
    expected_fit = expected.pop("fit_score")
    assert actual_fit == pytest.approx(expected_fit, abs=FIT_SCORE_TOL), (
        f"{name}: fit_score {actual_fit} drifted from golden {expected_fit}"
    )

    actual_struct = digest.pop("theme_structure")
    expected_struct = expected.pop("theme_structure")
    assert actual_struct.keys() == expected_struct.keys(), (
        f"{name}: theme set {sorted(actual_struct)} != golden {sorted(expected_struct)}"
    )
    for theme, exp in expected_struct.items():
        act = actual_struct[theme]
        for exact_key in ("populated_roles", "populated_usage"):
            assert act[exact_key] == exp[exact_key], (
                f"{name}/{theme}: {exact_key} {act[exact_key]} != golden {exp[exact_key]}"
            )
        for color_key in ("top_role_colors", "top_usage_colors"):
            exp_colors, act_colors = exp[color_key], act[color_key]
            assert act_colors.keys() == exp_colors.keys(), (
                f"{name}/{theme}: {color_key} keys {sorted(act_colors)} "
                f"!= golden {sorted(exp_colors)}"
            )
            for key, exp_hex in exp_colors.items():
                act_hex = act_colors[key]
                act_color, exp_color = parse_css_color(act_hex), parse_css_color(exp_hex)
                assert act_color is not None and exp_color is not None
                drift = delta_e(act_color, exp_color)
                assert drift <= COLOR_MATCH_TOL, (
                    f"{name}/{theme}/{color_key}/{key}: color {act_hex} drifted "
                    f"{drift:.4f} ΔE from golden {exp_hex} (tol {COLOR_MATCH_TOL})"
                )

    assert digest == expected, f"{name}: digest diverged from golden (run UPDATE_GOLDEN=1)"


# ---------------------------------------------------------------------------
# Fixture 1 — token-driven design system (light + dark)
# ---------------------------------------------------------------------------


@pytest.mark.browser
async def test_design_system_site(fixtures_dir: Path) -> None:
    result = await _analyze(fixtures_dir / "ds_site.html")

    # A real dark-mode block: both themes survive (no collapse).
    assert {str(t) for t in result.themes} == {"light", "dark"}
    assert len(result.metadata.themes_analyzed) == 2

    light = result.themes[Theme.light]
    assert light.tokens is not None

    # Tokens classify by name, with the `--color-` namespace stripped first.
    semantic = {t.name: t.semantic_role for t in light.tokens}
    assert semantic["--color-primary"] is TokenSemanticRole.brand_primary
    assert semantic["--color-secondary"] is TokenSemanticRole.brand_secondary
    assert semantic["--color-accent"] is TokenSemanticRole.brand_accent
    assert semantic["--color-on-primary"] is TokenSemanticRole.text_on
    assert semantic["--color-bg"] is TokenSemanticRole.surface_base
    assert semantic["--color-border"] is TokenSemanticRole.border

    # Status tokens are kept out of the palette views but still surface as DesignTokens.
    assert semantic["--color-success"] is TokenSemanticRole.status
    status_hexes = {t.color.hex for t in light.tokens if t.semantic_role == "status"}
    assert status_hexes == {"#16a34a", "#dc2626"}
    all_usage_hexes = {e.color.hex for entries in light.usage.mapping.values() for e in entries}
    assert not status_hexes & all_usage_hexes
    assert not result.third_party_colors

    # A token-driven site agrees well between declared intent and measured usage.
    assert light.fit_score > 0.6

    # Dominance (perceptual, platform-robust): the accent role is led by a declared
    # brand color, and the same brand colors lead the interactive usage category.
    accent = light.roles.mapping.get(PaletteRole.accent, [])
    assert accent, "expected accent candidates"
    brand_hexes = ("#2563eb", "#7c3aed", "#f59e0b")
    assert any(_color_near([accent[0].color], h) for h in brand_hexes)
    interactive = light.usage.mapping[UsageCategory.interactive]
    assert interactive, "expected interactive usage entries"
    assert any(_color_near([interactive[0].color], h) for h in brand_hexes)

    # The result is a clean Pydantic round-trip.
    assert AnalysisResult.model_validate_json(result.model_dump_json()) == result

    _check_golden("ds_site", _digest(result))


# ---------------------------------------------------------------------------
# Fixture 2 — no-token legacy site (usage-only, collapses to one theme)
# ---------------------------------------------------------------------------


@pytest.mark.browser
async def test_legacy_site(fixtures_dir: Path) -> None:
    result = await _analyze(fixtures_dir / "legacy_site.html")

    # No dark-mode block -> identical renders -> single theme.
    assert len(result.themes) == 1
    assert len(result.metadata.themes_analyzed) == 1

    # No custom properties: tokens were requested but none exist -> () (not None), the
    # palette is usage-driven, and every prominent color is "used but undeclared".
    (palette,) = result.themes.values()
    assert palette.tokens == ()
    assert palette.divergence  # used-but-undeclared discrepancies are reported
    assert all("undeclared" in item.note.lower() for item in palette.divergence)

    _check_golden("legacy_site", _digest(result))


# ---------------------------------------------------------------------------
# Fixture 3 — card-heavy catalog with a third-party widget
# ---------------------------------------------------------------------------


@pytest.mark.browser
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


# ---------------------------------------------------------------------------
# Fixture 4 — neutral-layered repo page: THE regression test for the usage-keyed
# palette redesign. A GitHub-repo-page-like design is almost entirely grays; the
# 60/30/10 roles view cannot express its gray text/border hierarchy, the usage
# view must.
# ---------------------------------------------------------------------------


@pytest.mark.browser
async def test_usage_redesign_captures_neutral_layered_design(fixtures_dir: Path) -> None:
    result = await _analyze(fixtures_dir / "repo_site.html")

    assert len(result.themes) == 1
    (palette,) = result.themes.values()
    usage = palette.usage.mapping

    # SURFACE: the white page background dominates, with the layered light-gray
    # surfaces (header/nav/cards, #f6f8fa) also present.
    surface = usage[UsageCategory.surface]
    assert surface, "expected surface entries"
    assert _color_near([surface[0].color], "#ffffff")
    assert _color_near([e.color for e in surface], "#f6f8fa")

    # TEXT: the gray text scale survives — at least two distinct grays (dark body
    # #1f2328 and muted #59636e), the structure the roles view loses entirely.
    text_colors = [e.color for e in usage[UsageCategory.text]]
    assert len(text_colors) >= 2
    assert _color_near(text_colors, "#1f2328")
    assert _color_near(text_colors, "#59636e")

    # INTERACTIVE: both the blue links and the green CTA buttons.
    interactive_colors = [e.color for e in usage[UsageCategory.interactive]]
    assert _color_near(interactive_colors, "#0969da")
    assert _color_near(interactive_colors, "#1f883d")

    # BORDER: the gray border/divider color.
    border_colors = [e.color for e in usage[UsageCategory.border]]
    assert _color_near(border_colors, "#d1d9e0")

    _check_golden("repo_site", _digest(result))


# ---------------------------------------------------------------------------
# Browserless unit tests of the golden helper itself
# ---------------------------------------------------------------------------


def _unit_digest() -> dict[str, Any]:
    """A minimal but structurally complete digest for exercising ``_check_golden``."""
    return {
        "themes": ["light"],
        "single_theme": True,
        "tokens": {"--color-primary": "brand_primary"},
        "has_divergence": False,
        "theme_structure": {
            "light": {
                "populated_roles": ["accent", "primary"],
                "top_role_colors": {"accent": "#2563eb", "primary": "#ffffff"},
                "populated_usage": ["interactive", "surface"],
                "top_usage_colors": {"interactive": "#2563eb", "surface": "#ffffff"},
            }
        },
        "fit_score": 0.8123,
    }


def test_check_golden_missing_golden_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing golden without UPDATE_GOLDEN must FAIL, never self-create-and-pass.

    Regression guard: the helper used to write the golden and return on `not
    path.exists()`, so a deleted/renamed golden silently passed forever.
    """
    monkeypatch.delenv("UPDATE_GOLDEN", raising=False)

    with pytest.raises(pytest.fail.Exception) as excinfo:
        _check_golden("no_such_site", _unit_digest(), golden_dir=tmp_path)

    message = str(excinfo.value)
    assert "no_such_site.json" in message
    assert "UPDATE_GOLDEN=1" in message
    # And it must not have created the file as a side effect.
    assert not (tmp_path / "no_such_site.json").exists()


def test_check_golden_update_env_writes_golden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under UPDATE_GOLDEN the helper (re)writes the golden and passes."""
    monkeypatch.setenv("UPDATE_GOLDEN", "1")
    digest = _unit_digest()

    _check_golden("unit_site", dict(digest), golden_dir=tmp_path)

    written = json.loads((tmp_path / "unit_site.json").read_text())
    assert written == digest


def test_check_golden_matches_and_mismatches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A digest matching a stored golden passes; a structural change fails."""
    monkeypatch.setenv("UPDATE_GOLDEN", "1")
    _check_golden("unit_site", _unit_digest(), golden_dir=tmp_path)
    monkeypatch.delenv("UPDATE_GOLDEN")

    # Same digest round-trips cleanly (note: _check_golden mutates via pop, so
    # always pass a fresh dict).
    _check_golden("unit_site", _unit_digest(), golden_dir=tmp_path)

    # A structural (exactly-compared) field change must fail.
    changed = _unit_digest()
    changed["has_divergence"] = True
    with pytest.raises(AssertionError):
        _check_golden("unit_site", changed, golden_dir=tmp_path)
