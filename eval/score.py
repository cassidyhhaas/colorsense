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
  they can only catch drift, never wrongness. The ground truth here is sourced independently
  but from the PAGE — the harvest's declared token values + the measured color of every
  element, reviewed by a human — never from off-page brand knowledge the algorithm can't see,
  so a panel-score change *means* something and is something the algorithm can actually reach.

The ground truth is a single canonical **color-keyed** table per site (color -> roles ->
components — see ``ground_truth.yaml``). It records ONLY what the page actually renders and how
it is used — never what a brand "should" use. From it the scorer derives BOTH views the library
emits and checks each, surfacing things a role-subset GT cannot:

* **recall**   — an expected color is absent from a role it should paint (computed per
  expected color, so it is overlap-safe and order-independent).
* **noise**    — a color lands in a role the GT does NOT list for it (mis-bucketing). A
  role no color lists is expected empty; any output there is noise.
* **won**      — the role's top entry is an expected color. A page whose CTAs are white has
  white as the correct `cta` answer; `won` does not assume a brand color "should" lead (the
  library no longer has a primary/secondary/accent frame, and neither does this eval).

Component attribution is reported as an *unscored diagnostic only*: the expected component
lists can only be sourced from the algorithm's own output, so scoring them would be self-
referential (the trap this eval exists to avoid). Matching uses one tolerance shared with the
authoring probe (``eval/probe.py``); two colors closer than it in one role are merged, enforced
at load. The harvest is scored on its own captured viewport. ``category`` is derived from the
harvest's element count, not trusted from the YAML, so a large site can't be hand-tagged
``harvest_completeness`` to escape the aggregate.

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
from colormetric import IDENTITY_TOLERANCE, pdelta  # same dir; perceptual ΔE2000 metric

from colorsense.classify.components import classify_components
from colorsense.classify.tokens import classify_tokens
from colorsense.color.primitives import parse_css_color
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

# Default per-site ΔE2000 tolerance for "this predicted color matches the expected one".
# In CIEDE2000 units via `pdelta` (see colormetric.py for why perceptual, not OKLab): measured
# cross-OS jitter for GT colors is ~0 and the tightest legitimately-distinct GT pair is 1.59, so
# 1.0 sits in that gap. Overridable per site in the YAML.
#
# This tolerance is the eval's *resolution*: it is the SAME tolerance authors must use when
# deciding whether two rendered colors are "the same" while authoring ground_truth.yaml.
# Authoring at a finer resolution than the scorer can distinguish (e.g. listing two near-
# identical grays the scorer will merge) makes the GT author-dependent and corrupts recall —
# so two colors closer than this (in any role) are one color, enforced globally at load time.
DEFAULT_TOLERANCE: float = IDENTITY_TOLERANCE

# A harvest with fewer than this many visible elements is too thin to score for *quality*
# (consent/login walls, lazy SPAs) — it is reported under `harvest_completeness` and excluded
# from the aggregate. Computed from the harvest, not trusted from the YAML, so a large site
# cannot be hand-tagged `harvest_completeness` to dodge a bad aggregate score.
COMPLETENESS_MAX_ELEMENTS: int = 100

# Family-bleed = the SAME color reported as both a background and an element-color winner.
# "Same" here is the identity tolerance: ΔE2000 is perceptually uniform, so (unlike OKLab) it
# does not over-compress near-blacks, and one tolerance serves both matching and bleed — a
# winner within IDENTITY_TOLERANCE of a GT background color (and not itself a sanctioned
# element color) is a leaked surface hex.
BLEED_EPS: float = IDENTITY_TOLERANCE

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
    colors: tuple[GTColor, ...]

    def expected_for(self, role: UsageRole) -> list[GTColor]:
        """The colors the GT says belong in ``role`` (empty => role expected empty)."""
        return [c for c in self.colors if role in c.roles]


def _parse_color(hx: str) -> Color:
    color = parse_css_color(hx)
    if color is None:
        raise ValueError(f"ground_truth.yaml: unparseable color {hx!r}")
    return color


def _validate_separation(name: str, colors: tuple[GTColor, ...], tol: float) -> None:
    """Reject two GT colors closer than ``tol`` — GLOBALLY, across all roles.

    The GT is color-keyed: one entry per color, carrying every role it paints. Two distinct
    entries within ``tol`` are the same color to the scorer regardless of role — in
    ``_score_color_view`` both match the single emitted cluster within tolerance, so one entry
    has its roles satisfied and the other spuriously reports absent/missing_roles. A role-scoped
    check misses this when the two colors sit in *different* roles, yet they still collide on the
    one output entry. A color is a color: merge them into ONE GT entry whose ``roles`` is the
    union. (The near-black lesson still holds the other way: variants > tol apart stay split.)
    """
    for i, a in enumerate(colors):
        for b in colors[i + 1 :]:
            d = pdelta(a.color, b.color)
            if d <= tol:  # `<=`, matching the scorer's `<= tol` so a pair at exactly tol (which
                # the matcher would merge onto one cluster) is rejected here too.
                raise ValueError(
                    f"ground_truth.yaml: {name} lists {a.hex} and {b.hex} only ΔE {d:.4f} apart "
                    f"(<= tolerance {tol}); the scorer matches both to one output cluster — merge "
                    f"them into one GT color whose `roles` is the union of theirs."
                )


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
        tolerance = float(spec.get("tolerance", DEFAULT_TOLERANCE))
        colors_t = tuple(colors)
        _validate_separation(name, colors_t, tolerance)
        sites[name] = GTSite(
            category=spec.get("category", "quality"),
            tolerance=tolerance,
            colors=colors_t,
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
    """The closest GT color within ``tol``, or None (so we can read its accepted comps).

    Deterministic: strictly-closer wins, ties broken by hex so the result never depends on
    YAML row order (the scorer must be reproducible regardless of how the GT was typed).
    """
    best: GTColor | None = None
    best_d = tol + 1.0
    for cand in candidates:
        d = pdelta(predicted, cand.color)
        if d <= tol and (d < best_d or (d == best_d and best is not None and cand.hex < best.hex)):
            best, best_d = cand, d
    return best


def _score_role(
    role: UsageRole,
    entries: tuple[Any, ...],
    expected: list[GTColor],
    bg_colors: list[Color],
    tol: float,
) -> RoleScore:
    scored: list[EntryScore] = []
    for i, e in enumerate(entries):
        match = _nearest(e.color, expected, tol)
        if match is not None:
            extra = tuple(sorted(c.value for c in e.components if c not in match.roles[role]))
        else:
            extra = ()
        # A bleed is a background color leaking into an element-color WINNER — but only when
        # that color isn't itself sanctioned for this element role (a neutral the GT lists as
        # both text and a dark surface, e.g. vercel #171717, is legit dual-use, not a leak).
        bled = (
            role in _ELEMENT_COLOR_ROLES
            and i == 0
            and match is None
            and any(pdelta(e.color, bg) <= BLEED_EPS for bg in bg_colors)
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
    # Recall is computed per EXPECTED color independently (overlap-safe): a GT color is
    # "present" iff ANY output entry is within tol of it. The old "credit only the nearest
    # entry's match" logic under-counted when one prediction sat between two GT colors.
    found = {gtc.hex for gtc in expected if any(pdelta(e.color, gtc.color) <= tol for e in entries)}
    missing = tuple(sorted(c.hex for c in expected if c.hex not in found))
    present = tuple(sorted(found))
    return RoleScore(
        role=role.value,
        entries=tuple(scored),
        present_expected=present,
        missing_expected=missing,
        has_expectation=bool(expected),
    )


def _score_color_view(palette: ThemePalette, gt: GTSite) -> tuple[ColorViewMismatch, ...]:
    """Check the color-keyed index: each GT color's roles vs its `usages`.

    Matching is union-over-tolerance, NOT nearest-only. When the pipeline emits more than one
    cluster within ``tolerance`` of a single GT color (e.g. anti-alias variants of one surface
    that cluster apart but both fall within the identity tolerance of the GT hex), those clusters
    are indistinguishable to the scorer — they are "the same color." The GT color's roles are
    therefore checked against the UNION of every within-tolerance cluster's roles. Nearest-only
    would credit just one cluster and spuriously flag the roles carried by the others as missing —
    the symmetric counterpart of the GT-side global-separation rule, and consistent with the
    overlap-safe per-color recall in ``_score_role``.
    """
    out: list[ColorViewMismatch] = []
    for gtc in gt.colors:
        matches = [cu for cu in palette.colors if pdelta(cu.color, gtc.color) <= gt.tolerance]
        if not matches:
            out.append(ColorViewMismatch(gtc.hex, absent=True, missing_roles=(), extra_roles=()))
            continue
        gt_roles = set(gtc.roles)
        out_roles = {u.role for cu in matches for u in cu.usages}
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

    # The category is derived from the harvest, not trusted from the YAML: a site with enough
    # elements to score for quality cannot be hand-tagged `harvest_completeness` to dodge the
    # aggregate. (A thin site tagged `quality` is allowed but warned — the author may want the
    # recall signal anyway.)
    n = len(harvest.elements)
    auto = "harvest_completeness" if n < COMPLETENESS_MAX_ELEMENTS else "quality"
    category = gt.category
    if gt.category == "harvest_completeness" and auto == "quality":
        raise ValueError(
            f"ground_truth.yaml: {name} is tagged harvest_completeness but has {n} elements "
            f"(>= {COMPLETENESS_MAX_ELEMENTS}); it cannot be excluded from the quality aggregate."
        )
    if gt.category == "quality" and auto == "harvest_completeness":
        print(
            f"warning: {name} tagged quality but only {n} elements (< "
            f"{COMPLETENESS_MAX_ELEMENTS}) — likely a thin/walled harvest",
            file=sys.stderr,
        )

    # Run on the viewport the harvest was actually captured at — classification area-fraction
    # gates depend on it, so a mismatched default would silently change the output.
    palette = _run_pipeline(harvest, harvest.viewport)

    # Family-bleed compares an element-color winner against the GT's *known background*
    # colors — not against the output's background entries, which can themselves contain the
    # very leak (or a phantom) we're trying to detect, making the check circular.
    bg_colors: list[Color] = [
        c.color for c in gt.colors if any(role in c.roles for role in _BG_ROLES)
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
        category=category,
        elements=n,
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
        return f"{round(100 * n / d)}%" if d else "n/a"

    recall = f"{present_facts}/{expected_facts}  ({pct(present_facts, expected_facts)})"
    winners = f"{won_roles}/{roles_with_exp}  ({pct(won_roles, roles_with_exp)})"
    print("\n" + "=" * 64)
    print(f"QUALITY PANEL ({len(quality)} sites)")
    print(f"  recall (expected colors present) : {recall}")
    print(f"  role winners correct             : {winners}")
    print(f"  NOISE (colors in a wrong/empty role): {noise}")
    print(f"  family-bleed                     : {bled}")
    print(
        f"  [diagnostic] component mismatches: {comp_mismatch}  "
        "(derived from the algorithm's own output — informational, not a scored signal)"
    )
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
