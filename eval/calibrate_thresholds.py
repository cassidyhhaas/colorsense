"""Calibration harness for ``theta_present`` per role (tuning-spec §4.2, §5).

Sweeps ``theta_present`` per role over a grid of multiples of ``theta_noise`` and
reports per-role must-keep RECALL and NOISE at each multiplier, using the same
GT-loading and color-matching machinery as the panel scorer (``eval/score.py``).

THIS IS AN IN-SAMPLE FIT on the 10 QUALITY sites.  The out-of-sample check is the
full goldens regression and the panel comparison in ``eval/score.py``.

Excluded from the fit:
  - ``platform_disco`` — category ``harvest_completeness``, excluded from the quality
    aggregate just as the panel scorer does.

Usage:
    uv run python eval/calibrate_thresholds.py          # full sweep table + knee choices
    uv run python eval/calibrate_thresholds.py --role text cta   # subset of roles only
"""

from __future__ import annotations

import gzip
import sys
from collections import defaultdict
from pathlib import Path
from typing import NamedTuple

# Put the eval directory on sys.path so score.py helpers are importable.
_EVAL_DIR = Path(__file__).parent
if str(_EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(_EVAL_DIR))

from colormetric import pdelta  # noqa: E402 — after path fix
from score import (  # noqa: E402
    COMPLETENESS_MAX_ELEMENTS,
    HARVEST_DIR,
    GTColor,
    GTSite,
    _load_gt,
)

from colorsense.classify.components import classify_components  # noqa: E402
from colorsense.classify.tokens import classify_tokens  # noqa: E402
from colorsense.config import load_default_config  # noqa: E402
from colorsense.models import Harvest, UsageRole, Viewport  # noqa: E402
from colorsense.palette.detect import _Candidate, _score_candidates  # noqa: E402
from colorsense.palette.fusion import build_evidence  # noqa: E402
from colorsense.palette.usage import _AREA_RANKED_ROLES  # noqa: E402

# ---------------------------------------------------------------------------
# Multiplier grids (expressed as multiples of theta_noise per role family)
# ---------------------------------------------------------------------------

#: Surface roles use area-sum; theta_present defaults to theta_noise (1.0x).
_SURFACE_ROLES: frozenset[UsageRole] = frozenset(_AREA_RANKED_ROLES)

#: Element roles: smaller multiples for medium-evidence roles, wider for discovery.
_ELEMENT_MULTIPLIERS: list[float] = [1.0, 1.4, 2.0, 3.0, 5.0, 8.0, 12.0, 20.0, 35.0, 60.0]
_SURFACE_MULTIPLIERS: list[float] = [1.0, 1.5, 2.0, 3.0, 5.0, 8.0]


# ---------------------------------------------------------------------------
# Per-site pre-gate candidate computation
# ---------------------------------------------------------------------------


class _SiteData(NamedTuple):
    """Pre-gate candidates + GT for one quality site."""

    name: str
    candidates: list[_Candidate]
    gt: GTSite
    theta_noise: dict[UsageRole, float]


def _load_site(name: str, gt: GTSite) -> _SiteData | None:
    """Load harvest, run up to build_evidence + _score_candidates, return pre-gate data.

    Returns ``None`` if the harvest has fewer than ``COMPLETENESS_MAX_ELEMENTS`` (same
    exclusion the panel scorer applies to ``harvest_completeness`` sites).
    """
    harvest_path = HARVEST_DIR / f"{name}.json.gz"
    if not harvest_path.exists():
        return None
    with gzip.open(harvest_path) as fh:
        harvest = Harvest.model_validate_json(fh.read())

    if len(harvest.elements) < COMPLETENESS_MAX_ELEMENTS:
        return None  # thin/walled harvest — exclude just like the panel

    config = load_default_config()
    viewport: Viewport = harvest.viewport
    classified_tokens = classify_tokens(harvest.tokens, config)
    classified_elements = classify_components(harvest.elements, config, viewport)
    evidence = build_evidence(harvest, classified_elements, config, viewport)
    candidates = _score_candidates(evidence, classified_tokens, config)

    theta_noise_map = {role: config.detection.roles[role].theta_noise for role in UsageRole}
    return _SiteData(name=name, candidates=candidates, gt=gt, theta_noise=theta_noise_map)


# ---------------------------------------------------------------------------
# Scoring at a given theta_present per role
# ---------------------------------------------------------------------------


def _survivors_at(
    candidates: list[_Candidate],
    theta_noise: dict[UsageRole, float],
    theta_present_override: dict[UsageRole, float],
) -> dict[UsageRole, list[_Candidate]]:
    """Apply gates using ``theta_present_override`` instead of the config values.

    A candidate survives role ``r`` iff:
      ``s_measured >= theta_noise[r]`` AND ``s_final >= theta_present_override[r]``.
    """
    survivors: dict[UsageRole, list[_Candidate]] = defaultdict(list)
    for c in candidates:
        role = c.evidence.role
        if c.s_measured >= theta_noise[role] and c.s_final >= theta_present_override[role]:
            survivors[role].append(c)
    return dict(survivors)


def _nearest_gt(predicted_color: object, gt_colors: list[GTColor], tol: float) -> GTColor | None:
    """The closest GT color within ``tol`` ΔE2000, or None — mirrors ``score._nearest``."""
    from colorsense.models import Color

    best: GTColor | None = None
    best_d = tol + 1.0
    pred: Color = predicted_color  # type: ignore[assignment]
    for cand in gt_colors:
        d = pdelta(pred, cand.color)
        if d <= tol and (d < best_d or (d == best_d and best is not None and cand.hex < best.hex)):
            best, best_d = cand, d
    return best


def _score_role_at(
    role: UsageRole,
    survivors: list[_Candidate],
    expected: list[GTColor],
    tol: float,
) -> tuple[int, int, int]:
    """Return (n_expected_matched, n_expected_total, n_noise) for ``role`` at this threshold.

    Recall logic mirrors ``score._score_role``: a GT color is "present" iff ANY survivor is
    within ``tol`` of it (overlap-safe, independent per expected color).
    NOISE = # survivors matching no expected color for this role.
    """
    n_total = len(expected)
    # Recall: for each GT color, does any survivor match it?
    found = 0
    for gtc in expected:
        for surv in survivors:
            if pdelta(surv.evidence.color, gtc.color) <= tol:
                found += 1
                break

    # Noise: survivors not matching any expected color
    noise = 0
    for surv in survivors:
        matched = _nearest_gt(surv.evidence.color, expected, tol)
        if matched is None:
            noise += 1

    return found, n_total, noise


# ---------------------------------------------------------------------------
# Full sweep
# ---------------------------------------------------------------------------


class _RoleSweepRow(NamedTuple):
    multiplier: float
    theta_present: float
    recall_num: int
    recall_den: int
    noise: int


def _sweep_role(
    role: UsageRole,
    sites: list[_SiteData],
    multipliers: list[float],
) -> list[_RoleSweepRow]:
    """Sweep ``theta_present`` multipliers for ``role`` across all quality sites."""
    rows: list[_RoleSweepRow] = []
    for mult in multipliers:
        total_found = 0
        total_expected = 0
        total_noise = 0
        for site in sites:
            theta_noise_r = site.theta_noise[role]
            tp = mult * theta_noise_r
            # Build survivors for this role only at this threshold.
            survivors_r: list[_Candidate] = [
                c
                for c in site.candidates
                if c.evidence.role == role and c.s_measured >= theta_noise_r and c.s_final >= tp
            ]
            expected = site.gt.expected_for(role)
            found, n_exp, noise = _score_role_at(role, survivors_r, expected, site.gt.tolerance)
            total_found += found
            total_expected += n_exp
            total_noise += noise
        rows.append(
            _RoleSweepRow(
                multiplier=mult,
                theta_present=mult * sites[0].theta_noise[role] if sites else 0.0,
                recall_num=total_found,
                recall_den=total_expected,
                noise=total_noise,
            )
        )
    return rows


# ---------------------------------------------------------------------------
# Knee selection
# ---------------------------------------------------------------------------


def _pick_knee(rows: list[_RoleSweepRow]) -> _RoleSweepRow:
    """Choose the knee of the recall/noise curve for one role (tuning-spec §4.2).

    Strategy: find the maximum achievable recall across all multipliers, then select the
    LARGEST multiplier that still achieves that recall (minimizing noise, per the spec's
    "raise the bar as far as possible without dropping must-keep colors" instruction).
    Ties on recall are broken by the larger multiplier (less noise) — tuning-spec §3, step 5.
    """
    if not rows:
        raise ValueError("no sweep rows")
    max_recall = max(r.recall_num for r in rows)
    # All rows at max recall — pick the one with largest multiplier (rightmost in the grid).
    at_max = [r for r in rows if r.recall_num == max_recall]
    return max(at_max, key=lambda r: r.multiplier)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    """Run the calibration sweep and print results."""
    # Parse optional --role filter.
    role_filter: set[UsageRole] | None = None
    remaining = list(argv)
    if "--role" in remaining:
        idx = remaining.index("--role")
        role_names = []
        i = idx + 1
        while i < len(remaining) and not remaining[i].startswith("--"):
            role_names.append(remaining[i])
            i += 1
        role_filter = {UsageRole(n) for n in role_names}
        remaining = remaining[:idx] + remaining[i:]

    gt_all = _load_gt()

    # Load quality sites (exclude harvest_completeness category or thin harvests).
    sites: list[_SiteData] = []
    for name, gt in gt_all.items():
        if gt.category == "harvest_completeness":
            continue
        if not (HARVEST_DIR / f"{name}.json.gz").exists():
            print(f"  skip {name}: no harvest", file=sys.stderr)
            continue
        if not gt.colors:
            print(f"  skip {name}: no GT colors", file=sys.stderr)
            continue
        data = _load_site(name, gt)
        if data is None:
            print(
                f"  skip {name}: thin harvest (harvest_completeness by element count)",
                file=sys.stderr,
            )
            continue
        sites.append(data)
        print(f"  loaded {name} ({len(data.candidates)} candidates)", file=sys.stderr)

    if not sites:
        print("ERROR: no quality sites loaded", file=sys.stderr)
        return 1

    print(f"\nFit corpus: {len(sites)} quality sites (IN-SAMPLE)", file=sys.stderr)
    print("", file=sys.stderr)

    config = load_default_config()

    chosen: dict[UsageRole, _RoleSweepRow] = {}
    all_rows: dict[UsageRole, list[_RoleSweepRow]] = {}

    for role in UsageRole:
        if role_filter and role not in role_filter:
            continue
        multipliers = _SURFACE_MULTIPLIERS if role in _SURFACE_ROLES else _ELEMENT_MULTIPLIERS
        rows = _sweep_role(role, sites, multipliers)
        all_rows[role] = rows
        knee = _pick_knee(rows)
        chosen[role] = knee

    # --- Print sweep tables ---
    print("=" * 70)
    print("SWEEP TABLES (per role)")
    print("=" * 70)
    for role, rows in all_rows.items():
        rc = config.detection.roles[role]
        tn = rc.theta_noise
        tp_cur = rc.theta_present
        print(f"\n{role.value:10s}  theta_noise={tn:.2e}  (current theta_present={tp_cur:.2e})")
        print(f"  {'mult':>6}  {'theta_present':>14}  {'recall':>12}  {'noise':>6}")
        print(f"  {'-' * 6}  {'-' * 14}  {'-' * 12}  {'-' * 6}")
        for r in rows:
            pct = f"{round(100 * r.recall_num / r.recall_den)}%" if r.recall_den else "n/a"
            recall_str = f"{r.recall_num}/{r.recall_den} ({pct})"
            is_knee = r.multiplier == chosen[role].multiplier
            marker = " <-- KNEE" if is_knee else ""
            tp_str = f"{r.theta_present:.2e}"
            print(f"  {r.multiplier:>6.1f}  {tp_str:>14}  {recall_str:>12}  {r.noise:>6}{marker}")

    # --- Print knee choices ---
    print("\n" + "=" * 70)
    print("CHOSEN MULTIPLIERS (knee per role)")
    print("=" * 70)
    hdr = (
        f"  {'role':10s}  {'mult':>6}  {'theta_noise':>12}  "
        f"{'theta_present':>14}  {'recall':>12}  {'noise':>6}  justification"
    )
    print(hdr)
    sep = f"  {'-' * 10}  {'-' * 6}  {'-' * 12}  {'-' * 14}  {'-' * 12}  {'-' * 6}  {'-' * 40}"
    print(sep)
    for role, row in chosen.items():
        rc = config.detection.roles[role]
        pct = f"{round(100 * row.recall_num / row.recall_den)}%" if row.recall_den else "n/a"
        recall_str = f"{row.recall_num}/{row.recall_den} ({pct})"
        max_recall = max(r.recall_num for r in all_rows[role])
        at_max = [r for r in all_rows[role] if r.recall_num == max_recall]
        if len(at_max) > 1:
            just = f"largest mult at plateau ({len(at_max)} ties)"
        else:
            just = "only mult achieving max recall"
        tn_s = f"{rc.theta_noise:.2e}"
        tp_s = f"{row.theta_present:.2e}"
        print(
            f"  {role.value:10s}  {row.multiplier:>6.1f}  {tn_s:>12}  "
            f"{tp_s:>14}  {recall_str:>12}  {row.noise:>6}  {just}"
        )

    # --- Rescue-band invariant check ---
    print("\n" + "=" * 70)
    print("RESCUE-BAND INVARIANT CHECK  (theta_present/(1+alpha) >= theta_noise)")
    print("=" * 70)
    alpha = config.detection.alpha
    element_roles = {
        UsageRole.CTA,
        UsageRole.ACTION,
        UsageRole.TEXT,
        UsageRole.LINK,
        UsageRole.BORDER,
    }
    all_ok = True
    for role in element_roles:
        if role not in chosen:
            continue
        row = chosen[role]
        rc = config.detection.roles[role]
        lower_band = row.theta_present / (1.0 + alpha)
        ok = lower_band >= rc.theta_noise
        status = "OK" if ok else "VIOLATION"
        if not ok:
            all_ok = False
        tp_s = f"{row.theta_present:.2e}"
        lb_s = f"{lower_band:.2e}"
        tn_s = f"{rc.theta_noise:.2e}"
        print(f"  {role.value:10s}  tp={tp_s}  tp/(1+{alpha})={lb_s}  theta_noise={tn_s}  {status}")
    if all_ok:
        print("  All element roles: invariant holds.")

    # --- YAML values to apply ---
    print("\n" + "=" * 70)
    print("YAML VALUES TO APPLY  (detection.roles[*].theta_present)")
    print("=" * 70)
    for role, row in chosen.items():
        mult_tag = f"# {row.multiplier:.1f}x theta_noise"
        print(f"  {role.value}: theta_present: {row.theta_present:.4e}  {mult_tag}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
