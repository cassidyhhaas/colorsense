"""Offline real-site quality eval for the colorsense palette.

This is the project's **quality** signal, distinct from the golden snapshots in
``tests/golden/`` (which guard *determinism* and *churn* on synthetic fixtures, not
correctness). It runs the real, shipped per-theme pipeline
(``classify_components -> build_inventory -> build_color_index/build_usage -> reconcile``)
against a panel of **frozen** real-site harvests (``eval/harvests/*.json.gz``) and scores
the output against human-reviewed expected colors (``eval/ground_truth.yaml``).

Why frozen harvests + a separate ground-truth file:

* **Reproducible & offline.** Sites drift and live rendering is non-deterministic
  page-to-page; pinning the harvested input isolates *code* changes from *page* changes
  (the "harvest-once / classify-many" technique) and lets the eval run with no network.
* **Non-self-referential.** Goldens are regenerated from the algorithm's own output, so
  they can only catch drift, never wrongness. The ground truth here is sourced
  independently (each site's declared design tokens + published brand guidelines) and
  reviewed by a human, so a panel-score change *means* something.

Run:  ``uv run python eval/score.py``            (full panel scorecard)
      ``uv run python eval/score.py stripe github``  (named sites)
      ``uv run python eval/score.py --json``        (machine-readable)

Exit status is always 0 — this is a report for human review on palette-affecting PRs,
not a CI gate. Compare a baseline run (on ``main``) against a branch run by eye.
"""

from __future__ import annotations

import gzip
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from colorsense.classify.components import classify_components
from colorsense.classify.tokens import classify_tokens
from colorsense.color.primitives import delta_e, parse_css_color
from colorsense.config import load_default_config
from colorsense.models import (
    Color,
    Harvest,
    ThemePalette,
    UsageRole,
    Viewport,
)
from colorsense.palette.inventory import build_inventory
from colorsense.palette.reconcile import reconcile
from colorsense.palette.usage import build_color_index, build_usage

EVAL_DIR = Path(__file__).parent
HARVEST_DIR = EVAL_DIR / "harvests"
GROUND_TRUTH = EVAL_DIR / "ground_truth.yaml"

# Default per-site OKLab ΔE tolerance for "this predicted color matches the expected one".
# 0.06 comfortably covers anti-alias/quantization drift (sub-0.04, per the golden tol of
# 0.10) without accepting a genuinely different hue; overridable per site in the YAML.
DEFAULT_TOLERANCE: float = 0.06

# Background roles, used by the family-bleed check: a text/link/border winner that matches
# one of these roles' colors has leaked a surface hex into an element-color answer.
_BG_ROLES = (UsageRole.page, UsageRole.surface, UsageRole.banner)


@dataclass(frozen=True)
class RoleScore:
    role: str
    expected: tuple[str, ...]
    winner: str | None
    present: bool  # an expected color appears anywhere in the role's ranked list
    won: bool  # an expected color is the role's top entry
    bled: bool  # (text/link/border only) winner matches a background-role color


@dataclass(frozen=True)
class SiteScore:
    site: str
    category: str
    elements: int
    roles: tuple[RoleScore, ...]


def _run_pipeline(harvest: Harvest, viewport: Viewport) -> ThemePalette:
    """Reproduce the shipped per-theme chain (``pipeline._analyze_theme``) exactly."""
    config = load_default_config()
    classified_tokens = classify_tokens(harvest.tokens, config)
    classified_elements = classify_components(harvest.elements, config, viewport)
    clusters = build_inventory(harvest, classified_elements)
    color_index = build_color_index(clusters)
    measured_usage = build_usage(clusters)
    posterior_usage, divergence = reconcile(
        measured_usage, classified_tokens, measured_colors=[c.color for c in clusters]
    )
    return ThemePalette(
        theme=harvest.theme,
        colors=color_index,
        usage=posterior_usage,
        divergence=tuple(divergence),
        tokens=None,
    )


def _matches(predicted: Color, expected: list[Color], tol: float) -> bool:
    return any(delta_e(predicted, exp) <= tol for exp in expected)


def _parse_all(hexes: list[str]) -> list[Color]:
    out: list[Color] = []
    for hx in hexes:
        color = parse_css_color(hx)
        if color is None:
            raise ValueError(f"ground_truth.yaml: unparseable color {hx!r}")
        out.append(color)
    return out


def _score_site(name: str, spec: dict[str, Any]) -> SiteScore:
    with gzip.open(HARVEST_DIR / f"{name}.json.gz") as fh:
        harvest = Harvest.model_validate_json(fh.read())
    vp_spec = spec.get("viewport", {})
    viewport = Viewport(
        width=vp_spec.get("width", 1280),
        height=vp_spec.get("height", 800),
        device_scale_factor=vp_spec.get("dsf", 1.0),
    )
    palette = _run_pipeline(harvest, viewport)
    tol = float(spec.get("tolerance", DEFAULT_TOLERANCE))

    # Background-role colors present in the output, for the bleed check.
    bg_colors: list[Color] = [
        entry.color for role in _BG_ROLES for entry in palette.usage.mapping.get(role, ())
    ]

    role_scores: list[RoleScore] = []
    for role_name, role_spec in spec.get("roles", {}).items():
        role = UsageRole(role_name)
        expected = _parse_all(role_spec["expect"])
        entries = palette.usage.mapping.get(role, ())
        winner = entries[0].color if entries else None
        present = any(_matches(e.color, expected, tol) for e in entries)
        won = winner is not None and _matches(winner, expected, tol)
        bled = (
            role in (UsageRole.text, UsageRole.link, UsageRole.border)
            and winner is not None
            and _matches(winner, bg_colors, 1e-6)
        )
        role_scores.append(
            RoleScore(
                role=role_name,
                expected=tuple(role_spec["expect"]),
                winner=winner.hex if winner else None,
                present=present,
                won=won,
                bled=bled,
            )
        )

    return SiteScore(
        site=name,
        category=spec.get("category", "quality"),
        elements=len(harvest.elements),
        roles=tuple(role_scores),
    )


def _print_human(scores: list[SiteScore]) -> None:
    quality = [s for s in scores if s.category == "quality"]
    for site in scores:
        tag = "" if site.category == "quality" else f"  [{site.category}]"
        print(f"\n{site.site}  ({site.elements} elements){tag}")
        for r in site.roles:
            marks = "".join(
                (
                    "W" if r.won else ("." if r.present else "X"),
                    "!" if r.bled else " ",
                )
            )
            exp = ",".join(r.expected)
            print(f"   {r.role:8} {marks}  winner={r.winner or '-':9} expect={exp}")

    # Aggregate over the quality panel only.
    all_roles = [r for s in quality for r in s.roles]
    won = sum(r.won for r in all_roles)
    present = sum(r.present for r in all_roles)
    bled = sum(r.bled for r in all_roles)
    total = len(all_roles)
    print("\n" + "=" * 60)
    print(f"QUALITY PANEL ({len(quality)} sites, {total} scored roles)")
    print(f"  winner-correct : {won}/{total}  ({100 * won // total if total else 0}%)")
    print(f"  present-anywhere: {present}/{total}  ({100 * present // total if total else 0}%)")
    print(f"  family-bleed    : {bled}  (text/link/border winner == a background hex)")
    print("  legend: W=won  .=present-not-won  X=absent  !=bleed")


def _as_dict(scores: list[SiteScore]) -> dict[str, Any]:
    return {
        s.site: {
            "category": s.category,
            "elements": s.elements,
            "roles": {
                r.role: {
                    "winner": r.winner,
                    "expected": list(r.expected),
                    "present": r.present,
                    "won": r.won,
                    "bled": r.bled,
                }
                for r in s.roles
            },
        }
        for s in scores
    }


def main(argv: list[str]) -> int:
    as_json = "--json" in argv
    names = [a for a in argv if not a.startswith("--")]
    spec = yaml.safe_load(GROUND_TRUTH.read_text())["sites"]
    selected = names or list(spec)

    scores: list[SiteScore] = []
    for name in selected:
        if name not in spec:
            print(f"warning: {name} not in ground_truth.yaml; skipping", file=sys.stderr)
            continue
        if not (HARVEST_DIR / f"{name}.json.gz").exists():
            print(f"warning: no frozen harvest for {name}; skipping", file=sys.stderr)
            continue
        scores.append(_score_site(name, spec[name]))

    if as_json:
        print(json.dumps(_as_dict(scores), indent=2))
    else:
        _print_human(scores)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
