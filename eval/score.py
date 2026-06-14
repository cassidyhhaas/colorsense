"""Offline real-site quality eval for the colorsense palette.

This is the project's **quality** signal, distinct from the golden snapshots in
``tests/golden/`` (which guard *determinism* and *churn* on synthetic fixtures, not
correctness). It runs the real, shipped per-theme pipeline
(``classify_components -> build_inventory -> build_color_index/build_usage -> reconcile``)
against a panel of **frozen** real-site harvests (``eval/harvests/*.json.gz``) and scores
the output against a human-reviewed expected palette (``eval/ground_truth.yaml``).

Why frozen harvests + a separate ground-truth file:

* **Reproducible & offline.** Sites drift and live rendering is non-deterministic
  page-to-page; pinning the harvested input isolates *code* changes from *page* changes
  (the "harvest-once / classify-many" technique) and lets the eval run with no network.
* **Non-self-referential.** Goldens are regenerated from the algorithm's own output, so
  they can only catch drift, never wrongness. The ground truth here is sourced
  independently (each site's declared design tokens + published brand guidelines) and
  reviewed by a human, so a panel-score change *means* something.

The ground truth is a single canonical **color-keyed** table per site (color -> roles ->
components — see ``ground_truth.yaml``). From it the scorer derives BOTH views the library
emits and checks each, surfacing three things a role-subset GT cannot:

* **recall**   — an expected color is absent from a role it should paint.
* **noise**    — a color lands in a role the GT does NOT list for it (mis-bucketing). A
  role no color lists is expected empty; any output there is noise.
* **component** — a matched color is attributed to component types the GT doesn't expect.

Run:  ``uv run python eval/score.py``                (full panel scorecard)
      ``uv run python eval/score.py shadcn vercel``  (named sites)
      ``uv run python eval/score.py --json``         (machine-readable)

Exit status is always 0 — this is a report for human review on palette-affecting PRs,
not a CI gate. Compare a baseline run (on ``main``) against a branch run by eye.
"""

from __future__ import annotations

import gzip
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from colorsense.classify.components import classify_components
from colorsense.classify.tokens import classify_tokens
from colorsense.color.primitives import delta_e, parse_css_color
from colorsense.config import load_default_config
from colorsense.models import (
    Color,
    ComponentType,
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
_ELEMENT_COLOR_ROLES = (UsageRole.text, UsageRole.link, UsageRole.border)


# --------------------------------------------------------------------------- #
# Ground-truth model (the canonical color-keyed table, parsed once)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class GTColor:
    """One expected color and every role/component it legitimately paints."""

    hex: str
    color: Color
    source: str
    roles: dict[UsageRole, frozenset[ComponentType]]  # role -> accepted component types


@dataclass(frozen=True)
class GTSite:
    category: str
    tolerance: float
    viewport: dict[str, Any]
    colors: tuple[GTColor, ...]

    def expected_for(self, role: UsageRole) -> list[GTColor]:
        """The colors the GT says belong in ``role`` (empty => role expected empty)."""
        return [c for c in self.colors if role in c.roles]


def _parse_color(hx: str) -> Color:
    color = parse_css_color(hx)
    if color is None:
        raise ValueError(f"ground_truth.yaml: unparseable color {hx!r}")
    return color


def _load_gt() -> dict[str, GTSite]:
    raw = yaml.safe_load(GROUND_TRUTH.read_text())["sites"]
    sites: dict[str, GTSite] = {}
    for name, spec in raw.items():
        colors: list[GTColor] = []
        for c in spec.get("colors", []):
            roles = {
                UsageRole(r): frozenset(ComponentType(ct) for ct in comps)
                for r, comps in (c.get("roles") or {}).items()
            }
            colors.append(
                GTColor(
                    hex=c["hex"],
                    color=_parse_color(c["hex"]),
                    source=c.get("source", ""),
                    roles=roles,
                )
            )
        sites[name] = GTSite(
            category=spec.get("category", "quality"),
            tolerance=float(spec.get("tolerance", DEFAULT_TOLERANCE)),
            viewport=spec.get("viewport", {}),
            colors=tuple(colors),
        )
    return sites


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class EntryScore:
    """One color the library put in a role, judged against the GT for that role."""

    hex: str
    is_winner: bool
    expected: bool  # matched a GT color listed for this role
    bled: bool  # (element-color roles only) winner matches a background-role color
    extra_components: tuple[str, ...]  # output components the matched GT color doesn't list


@dataclass(frozen=True)
class RoleScore:
    role: str
    entries: tuple[EntryScore, ...]
    present_expected: tuple[str, ...]  # GT colors found somewhere in the role's list
    missing_expected: tuple[str, ...]  # GT colors absent from the output list
    has_expectation: bool  # GT lists ≥1 color for this role

    @property
    def won(self) -> bool:
        return bool(self.entries) and self.entries[0].expected

    @property
    def noise(self) -> tuple[str, ...]:
        return tuple(e.hex for e in self.entries if not e.expected)


@dataclass(frozen=True)
class ColorViewMismatch:
    """A GT color whose role membership in the color-keyed index disagrees with the GT."""

    hex: str
    absent: bool  # the color isn't in the colors index at all
    missing_roles: tuple[str, ...]  # GT roles not seen on the color's `usages`
    extra_roles: tuple[str, ...]  # roles on the color's `usages` the GT doesn't list


@dataclass(frozen=True)
class SiteScore:
    site: str
    category: str
    elements: int
    roles: tuple[RoleScore, ...]
    color_view: tuple[ColorViewMismatch, ...] = field(default_factory=tuple)


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


def _nearest(predicted: Color, candidates: list[GTColor], tol: float) -> GTColor | None:
    """The closest GT color within ``tol``, or None (so we can read its accepted comps)."""
    best: GTColor | None = None
    best_d = tol
    for cand in candidates:
        d = delta_e(predicted, cand.color)
        if d <= best_d:
            best, best_d = cand, d
    return best


def _score_role(
    role: UsageRole,
    entries: tuple[Any, ...],
    expected: list[GTColor],
    bg_colors: list[Color],
    tol: float,
) -> RoleScore:
    accepted = {c.hex for c in expected}
    scored: list[EntryScore] = []
    found: set[str] = set()
    for i, e in enumerate(entries):
        match = _nearest(e.color, expected, tol)
        if match is not None:
            found.add(match.hex)
            extra = tuple(sorted(c.value for c in e.components if c not in match.roles[role]))
        else:
            extra = ()
        bled = (
            role in _ELEMENT_COLOR_ROLES
            and i == 0
            and any(delta_e(e.color, bg) <= 1e-6 for bg in bg_colors)
        )
        scored.append(
            EntryScore(
                hex=e.color.hex,
                is_winner=(i == 0),
                expected=match is not None,
                bled=bled,
                extra_components=extra,
            )
        )
    missing = tuple(sorted(h for h in accepted if h not in found))
    present = tuple(sorted(found))
    return RoleScore(
        role=role.value,
        entries=tuple(scored),
        present_expected=present,
        missing_expected=missing,
        has_expectation=bool(expected),
    )


def _score_color_view(palette: ThemePalette, gt: GTSite) -> tuple[ColorViewMismatch, ...]:
    """Check the color-keyed index: each GT color's roles vs its `usages`."""
    out: list[ColorViewMismatch] = []
    for gtc in gt.colors:
        match = min(
            (cu for cu in palette.colors if delta_e(cu.color, gtc.color) <= gt.tolerance),
            key=lambda cu: delta_e(cu.color, gtc.color),
            default=None,
        )
        if match is None:
            out.append(ColorViewMismatch(gtc.hex, absent=True, missing_roles=(), extra_roles=()))
            continue
        gt_roles = set(gtc.roles)
        out_roles = {u.role for u in match.usages}
        missing = tuple(sorted(r.value for r in gt_roles - out_roles))
        extra = tuple(sorted(r.value for r in out_roles - gt_roles))
        if missing or extra:
            out.append(
                ColorViewMismatch(gtc.hex, absent=False, missing_roles=missing, extra_roles=extra)
            )
    return tuple(out)


def _score_site(name: str, gt: GTSite) -> SiteScore:
    with gzip.open(HARVEST_DIR / f"{name}.json.gz") as fh:
        harvest = Harvest.model_validate_json(fh.read())
    viewport = Viewport(
        width=gt.viewport.get("width", 1280),
        height=gt.viewport.get("height", 800),
        device_scale_factor=gt.viewport.get("dsf", 1.0),
    )
    palette = _run_pipeline(harvest, viewport)

    bg_colors: list[Color] = [
        entry.color for role in _BG_ROLES for entry in palette.usage.mapping.get(role, ())
    ]

    # Score EVERY role (not just the ones with expectations) so a color landing in a role
    # the GT says should be empty is flagged as noise.
    role_scores = tuple(
        _score_role(
            role,
            palette.usage.mapping.get(role, ()),
            gt.expected_for(role),
            bg_colors,
            gt.tolerance,
        )
        for role in UsageRole
    )
    return SiteScore(
        site=name,
        category=gt.category,
        elements=len(harvest.elements),
        roles=role_scores,
        color_view=_score_color_view(palette, gt),
    )


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #


def _print_human(scores: list[SiteScore]) -> None:
    quality = [s for s in scores if s.category == "quality"]
    for site in scores:
        tag = "" if site.category == "quality" else f"  [{site.category}]"
        print(f"\n{site.site}  ({site.elements} elements){tag}")
        for r in site.roles:
            # Skip roles with no expectation AND no output — nothing to say.
            if not r.has_expectation and not r.entries:
                continue
            print(f"   {r.role}:")
            for e in r.entries:
                # W=won (expected top), +=present (expected), !=noise/mis-bucket.
                mark = ("W" if e.is_winner else "+") if e.expected else "!"
                bleed = " BLEED" if e.bled else ""
                comp = f"  comp+={','.join(e.extra_components)}" if e.extra_components else ""
                note = "" if e.expected else "  <- NOISE (not in GT)"
                star = " *" if e.is_winner else "  "
                print(f"     {star}{mark} {e.hex}{note}{bleed}{comp}")
            if r.missing_expected:
                print(f"      X missing: {','.join(r.missing_expected)}")
            if not r.entries and r.has_expectation:
                print(f"      X (empty) missing: {','.join(r.missing_expected)}")
        if site.color_view:
            print("   colors-index disagreements:")
            for m in site.color_view:
                if m.absent:
                    print(f"      {m.hex}: absent from colors index")
                else:
                    bits = []
                    if m.missing_roles:
                        bits.append(f"missing roles {','.join(m.missing_roles)}")
                    if m.extra_roles:
                        bits.append(f"extra roles {','.join(m.extra_roles)}")
                    print(f"      {m.hex}: {'; '.join(bits)}")

    # Aggregate over the quality panel only.
    expected_facts = present_facts = 0
    roles_with_exp = won_roles = 0
    noise = comp_mismatch = bled = 0
    for s in quality:
        for r in s.roles:
            if r.has_expectation:
                roles_with_exp += 1
                won_roles += int(r.won)
                expected_facts += len(r.present_expected) + len(r.missing_expected)
                present_facts += len(r.present_expected)
            noise += len(r.noise)
            comp_mismatch += sum(bool(e.extra_components) for e in r.entries)
            bled += sum(e.bled for e in r.entries)

    def pct(n: int, d: int) -> str:
        return f"{100 * n // d}%" if d else "n/a"

    recall = f"{present_facts}/{expected_facts}  ({pct(present_facts, expected_facts)})"
    winners = f"{won_roles}/{roles_with_exp}  ({pct(won_roles, roles_with_exp)})"
    print("\n" + "=" * 64)
    print(f"QUALITY PANEL ({len(quality)} sites)")
    print(f"  recall (expected colors present) : {recall}")
    print(f"  role winners correct             : {winners}")
    print(f"  NOISE (colors in a wrong/empty role): {noise}")
    print(f"  component mis-attributions       : {comp_mismatch}")
    print(f"  family-bleed                     : {bled}")
    print("  legend: W=won(expected top)  +=present(expected)  !=NOISE  X=missing expected")


def _as_dict(scores: list[SiteScore]) -> dict[str, Any]:
    return {
        s.site: {
            "category": s.category,
            "elements": s.elements,
            "roles": {
                r.role: {
                    "won": r.won,
                    "has_expectation": r.has_expectation,
                    "present_expected": list(r.present_expected),
                    "missing_expected": list(r.missing_expected),
                    "noise": list(r.noise),
                    "entries": [
                        {
                            "hex": e.hex,
                            "winner": e.is_winner,
                            "expected": e.expected,
                            "bled": e.bled,
                            "extra_components": list(e.extra_components),
                        }
                        for e in r.entries
                    ],
                }
                for r in s.roles
            },
            "color_view": [
                {
                    "hex": m.hex,
                    "absent": m.absent,
                    "missing_roles": list(m.missing_roles),
                    "extra_roles": list(m.extra_roles),
                }
                for m in s.color_view
            ],
        }
        for s in scores
    }


def main(argv: list[str]) -> int:
    as_json = "--json" in argv
    names = [a for a in argv if not a.startswith("--")]
    gt = _load_gt()
    selected = names or list(gt)

    scores: list[SiteScore] = []
    for name in selected:
        if name not in gt:
            print(f"warning: {name} not in ground_truth.yaml; skipping", file=sys.stderr)
            continue
        if not (HARVEST_DIR / f"{name}.json.gz").exists():
            print(f"warning: no frozen harvest for {name}; skipping", file=sys.stderr)
            continue
        if not gt[name].colors:
            print(f"warning: {name} has no authored colors yet; skipping", file=sys.stderr)
            continue
        scores.append(_score_site(name, gt[name]))

    if as_json:
        print(json.dumps(_as_dict(scores), indent=2))
    else:
        _print_human(scores)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
