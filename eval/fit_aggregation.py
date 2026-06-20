"""Learning-to-rank fit of the per-role salience aggregation params (tuning-spec §2, §5, §7).

Sibling to ``eval/calibrate_thresholds.py``. That harness fits *only* ``theta_present`` at the
hand-set ``(lambda_r, beta_r)``; this one fits the **aggregation** itself — the
``(lambda_r, beta_r)`` of ``S_measured = sigma_(1) + lambda_r * sum_{i>=2} sigma_(i) ^ beta_r``
(`colorsense.palette.salience.aggregate_salience`) — and **re-fits ``theta_present`` jointly**
for every grid point, because the three are one coupled calibration (tuning-spec §7: "treat
``sigma_i``, ``(lambda_r, beta_r)``, and the thresholds as one coupled calibration, re-fit
together"). ``theta_noise`` is held fixed as the physical artifact anchor (tuning-spec §4.1).

Objective, per element role, against the labeled must-keep set (``eval/ground_truth.yaml``):

1. **must-keep recall** — never drop a ground-truth color that the role currently keeps;
2. **ranking quality** — NDCG (binary must-keep relevance, position-discounted) and top-1
   ("won") accuracy of the survivor ordering;
3. **precision** — minimize NOISE (survivors matching no must-keep color).

selected lexicographically in that order, so the fit raises ranking/precision *without*
sacrificing recall.

The magnitude subtlety (tuning-spec note, restated in the task): per-instance saliences
``sigma_i`` are area-fractions ``< 1``, so for ``x in (0, 1)`` the map ``x ^ beta`` is *larger*
for smaller ``beta`` (``sqrt(0.0001) = 0.01``, a 100x inflation). ``beta < 1`` therefore
*inflates* the corroboration tail rather than saturating it, letting a swarm of tiny instances
overwhelm the peak — the opposite of the intended "headcount cannot beat peak prominence." So
within the model's ``beta in (0, 1]`` box the **saturating** end is ``beta -> 1``, not
``beta -> 0``; the grid spans it and the harness prints the realized peak/tail split so the
choice is auditable. ``--tail-report`` dumps the inflation explicitly.

Area-ranked roles (``page``/``surface``/``banner`` — `palette.usage._AREA_RANKED_ROLES`) score
on screenshot area, not the aggregation, so ``(lambda_r, beta_r)`` are inert for them AND there is
no coupling forcing a threshold re-fit: the harness keeps their prior calibrated ``theta_present``
verbatim (re-fitting the area-floor on 10 sites would overfit) and only scores them for the
report. Their ``theta_present`` is owned by ``calibrate_thresholds.py``.

THIS IS AN IN-SAMPLE FIT on the 10 QUALITY sites (same corpus, loader, and color-matching as
``eval/score.py`` / ``eval/calibrate_thresholds.py``). The out-of-sample check is the full
goldens regression and the panel comparison in ``eval/score.py``.

Usage:
    uv run python eval/fit_aggregation.py            # fit all element roles, print panel diff
    uv run python eval/fit_aggregation.py --role text link   # subset of roles
    uv run python eval/fit_aggregation.py --tail-report      # + per-role tail-inflation table

The harness only reports; apply by hand-editing ``detection.roles`` in the YAML from the
"YAML VALUES TO APPLY" block, then confirm with ``eval/score.py`` + the goldens regression.
"""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

# Put the eval directory on sys.path so sibling helpers import cleanly.
_EVAL_DIR = Path(__file__).parent
if str(_EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(_EVAL_DIR))

from calibrate_thresholds import _load_site, _SiteData  # noqa: E402
from colormetric import pdelta  # noqa: E402
from score import GTColor, _load_gt  # noqa: E402

from colorsense.config import load_default_config  # noqa: E402
from colorsense.models import UsageRole  # noqa: E402
from colorsense.palette.salience import aggregate_salience, intent_multiplier  # noqa: E402
from colorsense.palette.usage import _AREA_RANKED_ROLES  # noqa: E402

if TYPE_CHECKING:
    from colorsense.palette.detect import _Candidate

# --------------------------------------------------------------------------- #
# Fit grids
# --------------------------------------------------------------------------- #

#: lambda_r in [0, 1] (corroboration weight); coarse 0.1 step is finer than the signal warrants.
_LAMBDA_GRID: tuple[float, ...] = (0.0, 0.1, 0.2, 0.3, 0.5, 0.7, 0.85, 1.0)

#: beta_r in (0, 1]; spans from the inflating end (0.5) to the saturating end (1.0). See the
#: module docstring on why 1.0 (not a small beta) is the saturating choice for sigma < 1.
_BETA_GRID: tuple[float, ...] = (0.5, 0.6, 0.7, 0.8, 0.9, 1.0)

#: Element roles whose aggregation is fit here. Area-ranked roles are excluded (their score is
#: screenshot area, so lambda/beta never enter `S_measured`).
_ELEMENT_ROLES: tuple[UsageRole, ...] = tuple(r for r in UsageRole if r not in _AREA_RANKED_ROLES)


# --------------------------------------------------------------------------- #
# Per-(lambda, beta) salience recomputation
# --------------------------------------------------------------------------- #


def _s_final_at(cand: _Candidate, lam: float, beta: float, alpha: float) -> tuple[float, float]:
    """Recompute ``(S_measured, S_final)`` for ``cand`` at trial ``(lambda, beta)``.

    The intent multiplier ``f`` is independent of ``(lambda, beta)`` — it depends only on the
    pre-computed ``q_intent`` — so it is reapplied unchanged. Area-ranked roles keep their
    screenshot-area ``S_measured`` (the aggregation never enters), matching
    `palette.detect._s_measured`.
    """
    if cand.evidence.role in _AREA_RANKED_ROLES:
        s_measured = cand.evidence.area
    else:
        s_measured = aggregate_salience(cand.evidence.instance_saliences, lam, beta)
    f = intent_multiplier(cand.q_intent, alpha)
    return s_measured, s_measured * f


# --------------------------------------------------------------------------- #
# Ranking metrics (binary must-keep relevance)
# --------------------------------------------------------------------------- #


def _is_relevant(color: object, expected: list[GTColor], tol: float) -> bool:
    """Whether ``color`` matches any must-keep GT color for the role (within ``tol``)."""
    from colorsense.models import Color

    c: Color = color  # type: ignore[assignment]
    return any(pdelta(c, gtc.color) <= tol for gtc in expected)


def _ndcg(rels: list[int]) -> float:
    """Binary NDCG of a survivor ordering (``rels`` is relevance in survivor rank order).

    ``IDCG`` ranks every relevant survivor first. Returns ``1.0`` for a role with no relevant
    survivors *and* no expectation pressure is handled by the caller (this is only called when
    the role has expectations and at least one survivor).
    """
    dcg = sum(rel / math.log2(i + 2) for i, rel in enumerate(rels))
    ideal = sorted(rels, reverse=True)
    idcg = sum(rel / math.log2(i + 2) for i, rel in enumerate(ideal))
    return dcg / idcg if idcg > 0.0 else 0.0


class _RoleMetrics(NamedTuple):
    """Aggregated ranking metrics for one role across the panel at a fixed threshold."""

    recall_num: int
    recall_den: int
    ndcg_sum: float  # sum of per-site NDCG over sites with an expectation + survivors
    ndcg_sites: int  # how many sites contributed to ndcg_sum (the denominator)
    top1_num: int  # sites whose top survivor is a must-keep color ("won")
    top1_den: int  # sites with an expectation AND at least one survivor
    noise: int  # survivors matching no must-keep color


def _score_role_panel(
    role: UsageRole,
    sites: list[_SiteData],
    lam: float,
    beta: float,
    alpha: float,
    theta_present: float,
) -> _RoleMetrics:
    """Score ``role`` across every site at trial ``(lambda, beta, theta_present)``.

    Survivors are the role's candidates passing both gates (``S_measured >= theta_noise`` AND
    ``S_final >= theta_present``), ranked by ``S_final`` descending (ties by hex, matching
    `palette.detect._build_usage_palette`).
    """
    recall_num = recall_den = 0
    ndcg_sum = 0.0
    ndcg_sites = top1_num = top1_den = noise = 0

    for site in sites:
        tn = site.theta_noise[role]
        scored: list[tuple[float, object]] = []
        for c in site.candidates:
            if c.evidence.role != role:
                continue
            s_measured, s_final = _s_final_at(c, lam, beta, alpha)
            if s_measured >= tn and s_final >= theta_present:
                scored.append((s_final, c.evidence.color))
        scored.sort(key=lambda t: (-t[0], t[1].hex))  # type: ignore[attr-defined]
        survivors = [color for _, color in scored]

        expected = site.gt.expected_for(role)
        tol = site.gt.tolerance

        # Recall is per-expected-color (overlap-safe), mirroring score._score_role.
        recall_den += len(expected)
        for gtc in expected:
            if any(pdelta(color, gtc.color) <= tol for color in survivors):  # type: ignore[arg-type]
                recall_num += 1

        rels = [int(_is_relevant(color, expected, tol)) for color in survivors]
        noise += sum(1 - r for r in rels)

        # Ranking metrics only meaningful where the GT lists must-keep colors and at least one
        # survivor exists to rank.
        if expected and survivors:
            top1_den += 1
            top1_num += rels[0]
            ndcg_sum += _ndcg(rels)
            ndcg_sites += 1
    return _RoleMetrics(recall_num, recall_den, ndcg_sum, ndcg_sites, top1_num, top1_den, noise)


# --------------------------------------------------------------------------- #
# Joint (lambda, beta, theta_present) selection
# --------------------------------------------------------------------------- #


def _candidate_thresholds(
    role: UsageRole, sites: list[_SiteData], lam: float, beta: float, alpha: float
) -> list[float]:
    """Threshold sweep points for ``theta_present``: just-above each realized ``S_final``.

    Sampling the actual ``S_final`` values (not a fixed multiplier grid) finds every threshold
    at which the survivor set changes, so the recall/NDCG/noise trade-off is resolved exactly.
    The role's ``theta_noise`` is the floor (``theta_present >= theta_noise`` keeps the
    rescue-band lower edge at or above the artifact floor — tuning-spec §4.3).
    """
    tn = min(site.theta_noise[role] for site in sites)
    finals: set[float] = {tn}
    for site in sites:
        for c in site.candidates:
            if c.evidence.role != role:
                continue
            s_measured, s_final = _s_final_at(c, lam, beta, alpha)
            if s_measured >= site.theta_noise[role] and s_final >= tn:
                finals.add(s_final)
    # Test "tp exactly at a survivor's S_final" (keeps it) by using each value directly; a tp
    # equal to S_final survives because the gate is ``>=``.
    return sorted(finals)


@dataclass(frozen=True)
class _RoleFit:
    """The chosen ``(lambda, beta, theta_present)`` for one role and its realized metrics."""

    role: UsageRole
    lambda_: float
    beta: float
    theta_present: float
    metrics: _RoleMetrics


def _fit_role(
    role: UsageRole,
    sites: list[_SiteData],
    alpha: float,
    fit_aggregation: bool,
    fixed_theta_present: float,
) -> _RoleFit:
    """Fit ``(lambda, beta)`` then re-fit ``theta_present`` — two decoupled stages.

    The two are a coupled calibration (tuning-spec §7), but they answer *different* questions and
    must not be collapsed into one scalar objective: ``(lambda, beta)`` shape the **ranking**
    (the order survivors come out in), ``theta_present`` sets the **cut** (recall vs. NOISE).
    Optimizing a single recall+ndcg+noise scalar lets ``beta`` trade against a jointly-refit
    threshold to game in-sample NOISE — and because ``sigma_i < 1`` makes ``beta < 1`` *inflate*
    the tail (module docstring), that trade pulls ``beta`` to the anti-saturating end. So:

    1. **Pick ``(lambda, beta)`` on threshold-free ranking quality.** Score the *full*
       ``theta_noise``-passing ordering (``theta_present = 0``): maximize must-keep recall, then
       panel NDCG, then top-1. This is the aggregation's actual job and is independent of where
       the present-cut lands. Ties break toward the **saturating** end (``beta`` nearest 1) then
       parsimony (smaller ``lambda``) — directly encoding "account for the inflation subtlety":
       a smaller ``beta`` is chosen only if it *genuinely* ranks better, never to cut noise.
    2. **Re-fit ``theta_present`` at the chosen aggregation** (the knee, mirroring
       ``calibrate_thresholds._pick_knee``): among thresholds holding max must-keep recall, take
       the one with least NOISE, then the largest ``theta_present`` (raise the bar).

    ``theta_present`` is re-fit ONLY when the aggregation actually changed (``fit_aggregation``).
    Area-ranked roles score on screenshot area — ``(lambda, beta)`` never enter their
    ``S_measured``, so there is no coupling forcing a re-fit; their already-calibrated
    ``fixed_theta_present`` is kept verbatim (re-fitting it would overfit the area-floor to these
    10 sites). The harness still scores them at that fixed threshold for the report.
    """
    if not fit_aggregation:
        # Area-ranked: keep area-sum (1, 1) and the existing calibrated theta_present.
        metrics = _score_role_panel(role, sites, 1.0, 1.0, alpha, fixed_theta_present)
        return _RoleFit(role, 1.0, 1.0, fixed_theta_present, metrics)

    grid = [(lam, beta) for lam in _LAMBDA_GRID for beta in _BETA_GRID]

    # Stage 1 — ranking: choose (lambda, beta) on the threshold-free (theta_present=0) ordering.
    best_key: tuple[float, ...] | None = None
    lam_star, beta_star = 1.0, 1.0
    for lam, beta in grid:
        rq = _score_role_panel(role, sites, lam, beta, alpha, 0.0)
        ndcg = rq.ndcg_sum / rq.ndcg_sites if rq.ndcg_sites else 0.0
        top1 = rq.top1_num / rq.top1_den if rq.top1_den else 0.0
        recall = rq.recall_num / rq.recall_den if rq.recall_den else 0.0
        key = (recall, ndcg, top1, -abs(1.0 - beta), -lam)  # saturation + parsimony tie-breaks
        if best_key is None or key > best_key:
            best_key = key
            lam_star, beta_star = lam, beta

    # Stage 2 — threshold: re-fit theta_present at (lam_star, beta_star) via the recall/noise knee.
    rows = [
        (tp, _score_role_panel(role, sites, lam_star, beta_star, alpha, tp))
        for tp in _candidate_thresholds(role, sites, lam_star, beta_star, alpha)
    ]
    max_recall = max(m.recall_num for _, m in rows)
    feasible = [(tp, m) for tp, m in rows if m.recall_num == max_recall]
    tp_star, metrics = min(feasible, key=lambda r: (r[1].noise, -r[0]))
    return _RoleFit(role, lam_star, beta_star, tp_star, metrics)


# --------------------------------------------------------------------------- #
# Tail-inflation diagnostic
# --------------------------------------------------------------------------- #


def _tail_report(sites: list[_SiteData], role: UsageRole, lam: float, beta: float) -> None:
    """Print, per role, how much ``beta`` inflates the corroboration tail vs. the raw sum.

    Demonstrates the magnitude subtlety concretely: ``raw_tail = sum sigma_i`` vs.
    ``pow_tail = sum sigma_i ^ beta`` over the role's multi-instance evidence. ``pow/raw > 1``
    means ``beta`` is *inflating* (anti-saturating) the tail.
    """
    raw = pow_ = 0.0
    n_inst = 0
    for site in sites:
        for c in site.candidates:
            if c.evidence.role != role:
                continue
            tail = c.evidence.instance_saliences[1:]
            n_inst += len(tail)
            raw += sum(tail)
            pow_ += sum(math.pow(s, beta) for s in tail)
    ratio = (pow_ / raw) if raw > 0.0 else float("nan")
    print(
        f"  {role.value:8s} beta={beta:.2f}  tail instances={n_inst:4d}  "
        f"raw_sum={raw:.4e}  pow_sum={pow_:.4e}  inflation(pow/raw)={ratio:6.2f}x"
    )


# --------------------------------------------------------------------------- #
# Panel diff (stock config vs. fitted override) via the real scorer
# --------------------------------------------------------------------------- #


class _Panel(NamedTuple):
    recall_num: int
    recall_den: int
    won: int
    roles_with_exp: int
    noise: int
    bleed: int


def _run_panel(fits: dict[UsageRole, _RoleFit] | None) -> _Panel:
    """Run the FULL shipping scorer over the quality panel, optionally with a fitted override.

    When ``fits`` is given, ``score.load_default_config`` is monkeypatched to return a config
    whose ``detection.roles`` carry the fitted ``(lambda, beta, theta_present)`` (``theta_noise``
    untouched), so the numbers are produced by the exact code path ``eval/score.py`` ships.
    """
    import score

    base = load_default_config()
    if fits is not None:
        from colorsense.config import RoleAggregationConfig

        new_roles = dict(base.detection.roles)
        for role, rc in base.detection.roles.items():
            fit = fits.get(role)
            if fit is None:
                continue
            new_roles[role] = RoleAggregationConfig(
                lambda_=fit.lambda_,
                beta=fit.beta,
                theta_noise=rc.theta_noise,
                theta_present=fit.theta_present,
            )
        new_detection = base.detection.model_copy(update={"roles": new_roles})
        override = base.model_copy(update={"detection": new_detection})
        # Patch the config loader score._run_pipeline calls so the fitted (lambda, beta,
        # theta_present) flow through the EXACT shipping scorer. setattr/getattr keep mypy happy
        # (score re-exports the symbol; direct attribute assignment trips attr-defined).
        original = getattr(score, "load_default_config")  # noqa: B009
        setattr(score, "load_default_config", lambda: override)  # noqa: B010
    try:
        gt = _load_gt()
        scores = [
            score._score_site(name, g)
            for name, g in gt.items()
            if g.colors and (score.HARVEST_DIR / f"{name}.json.gz").exists()
        ]
    finally:
        if fits is not None:
            setattr(score, "load_default_config", original)  # noqa: B010

    quality = [s for s in scores if s.category == "quality"]
    recall_num = recall_den = won = roles_with_exp = noise = bleed = 0
    for s in quality:
        for r in s.roles:
            if r.has_expectation:
                roles_with_exp += 1
                won += int(r.won)
                recall_num += len(r.present_expected)
                recall_den += len(r.present_expected) + len(r.missing_expected)
            noise += len(r.noise)
            bleed += sum(e.bled for e in r.entries)
    return _Panel(recall_num, recall_den, won, roles_with_exp, noise, bleed)


def _print_panel(label: str, p: _Panel) -> None:
    rp = f"{p.recall_num}/{p.recall_den} ({round(100 * p.recall_num / p.recall_den)}%)"
    wp = f"{p.won}/{p.roles_with_exp} ({round(100 * p.won / p.roles_with_exp)}%)"
    print(f"  {label:8s}  recall={rp:14s}  winners={wp:12s}  NOISE={p.noise:3d}  bleed={p.bleed}")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def _load_quality_sites() -> list[_SiteData]:
    """Load the 10 quality sites' pre-gate candidates (same exclusions as the panel scorer)."""
    gt = _load_gt()
    sites: list[_SiteData] = []
    for name, g in gt.items():
        if g.category == "harvest_completeness" or not g.colors:
            continue
        data = _load_site(name, g)
        if data is None:
            continue
        sites.append(data)
        print(f"  loaded {name} ({len(data.candidates)} candidates)", file=sys.stderr)
    return sites


def main(argv: list[str]) -> int:
    """Fit per-role aggregation + joint theta_present and print the before/after panel."""
    args = list(argv)
    tail_report = "--tail-report" in args
    args = [a for a in args if a != "--tail-report"]

    role_filter: set[UsageRole] | None = None
    if "--role" in args:
        idx = args.index("--role")
        names = []
        i = idx + 1
        while i < len(args) and not args[i].startswith("--"):
            names.append(args[i])
            i += 1
        role_filter = {UsageRole(n) for n in names}

    sites = _load_quality_sites()
    if not sites:
        print("ERROR: no quality sites loaded", file=sys.stderr)
        return 1
    print(f"\nFit corpus: {len(sites)} quality sites (IN-SAMPLE)\n", file=sys.stderr)

    alpha = load_default_config().detection.alpha
    config = load_default_config()

    # Fit element roles (aggregation + theta_present); area-ranked roles keep their calibrated
    # theta_present (their S_measured is area, unchanged by lambda/beta).
    fits: dict[UsageRole, _RoleFit] = {}
    for role in UsageRole:
        if role_filter and role not in role_filter:
            continue
        is_element = role not in _AREA_RANKED_ROLES
        fits[role] = _fit_role(
            role,
            sites,
            alpha,
            fit_aggregation=is_element,
            fixed_theta_present=config.detection.roles[role].theta_present,
        )

    # --- Per-role fit table ---
    print("=" * 92)
    print("FITTED PER-ROLE AGGREGATION + theta_present  (theta_noise held fixed)")
    print("=" * 92)
    hdr = (
        f"  {'role':8s}  {'lambda':>7}  {'beta':>5}  {'theta_noise':>12}  {'theta_present':>14}  "
        f"{'recall':>11}  {'ndcg':>6}  {'top1':>9}  {'noise':>5}"
    )
    print(hdr)
    print("  " + "-" * 88)
    for role in UsageRole:
        if role not in fits:
            continue
        f = fits[role]
        m = f.metrics
        rc = config.detection.roles[role]
        old = f"(was l={rc.lambda_} b={rc.beta} tp={rc.theta_present:.2e})"
        rec = f"{m.recall_num}/{m.recall_den}" if m.recall_den else "n/a"
        ndcg = m.ndcg_sum / m.ndcg_sites if m.ndcg_sites else float("nan")
        top1 = f"{m.top1_num}/{m.top1_den}" if m.top1_den else "n/a"
        print(
            f"  {role.value:8s}  {f.lambda_:>7.2f}  {f.beta:>5.2f}  {rc.theta_noise:>12.2e}  "
            f"{f.theta_present:>14.2e}  {rec:>11}  {ndcg:>6.3f}  {top1:>9}  {m.noise:>5}"
        )
        print(f"           {old}")

    if tail_report:
        print("\n" + "=" * 92)
        print("TAIL-INFLATION DIAGNOSTIC  (pow_sum/raw_sum > 1 => beta is INFLATING the tail)")
        print("=" * 92)
        for role in _ELEMENT_ROLES:
            if role_filter and role not in role_filter:
                continue
            for beta in (0.5, 0.9, 1.0):
                _tail_report(sites, role, fits[role].lambda_, beta)

    # --- Rescue-band invariant check (tuning-spec §4.3, §7) ---
    print("\n" + "=" * 92)
    print("RESCUE-BAND REGIME  (element roles): theta_present vs. (1+alpha)*theta_noise")
    print("=" * 92)
    for role in _ELEMENT_ROLES:
        if role not in fits:
            continue
        f = fits[role]
        tn = config.detection.roles[role].theta_noise
        lower = f.theta_present / (1.0 + alpha)
        # The hard invariant (band floor >= theta_noise) is enforced structurally by the
        # S_measured >= theta_noise gate; this only reports which rescue regime tp lands in.
        regime = "narrow gap (intent rescues any above-noise)" if lower < tn else "wide gap"
        print(
            f"  {role.value:8s}  tp={f.theta_present:.3e}  tp/(1+{alpha})={lower:.3e}  "
            f"tn={tn:.1e}  {regime}"
        )

    # --- Before/after FULL panel (real scorer) ---
    print("\n" + "=" * 92)
    print("FULL QUALITY PANEL — before (stock) vs. after (fitted)")
    print("=" * 92)
    _print_panel("before", _run_panel(None))
    # Only override roles we actually fit (a --role subset leaves the rest stock).
    _print_panel("after", _run_panel(fits))

    # --- YAML values ---
    print("\n" + "=" * 92)
    print("YAML VALUES TO APPLY  (detection.roles[*])")
    print("=" * 92)
    for role in UsageRole:
        if role not in fits:
            continue
        f = fits[role]
        rc = config.detection.roles[role]
        print(
            f"    {role.value + ':':9s} {{lambda: {f.lambda_}, beta: {f.beta}, "
            f"theta_noise: {rc.theta_noise}, theta_present: {f.theta_present:.4e}}}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
