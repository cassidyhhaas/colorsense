"""Authoring aid for ``ground_truth.yaml`` — turns GT authoring from judgement into evidence.

The ground truth must be sourced *independently* of the algorithm (declared tokens + brand
guidelines), but deciding whether a color the algorithm emits is a REAL on-page color or an
artifact still needs evidence from the page. This tool provides that evidence deterministically
so two authors reach the same answer — the operationalized rules behind the methodology:

* **Same tolerance as the scorer.** Element/bin matching here uses ``score.DEFAULT_TOLERANCE``
  — the perceptual ΔE2000 ``IDENTITY_TOLERANCE`` (see ``colormetric.py``), not a finer one.
  Authoring at a resolution the scorer can't distinguish makes the GT author-dependent. Two
  colors closer than this are ONE color.

* **Page-truth paint only.** Evidence counts a channel color only when it actually paints —
  ``alpha > 0``. The CSS-default transparent ``background-color`` paints nothing, so it must not
  inflate a color's element count (it otherwise reports thousands of phantom "elements" for a
  color the page never shows). This mirrors the pipeline's own ``alpha > 0`` gates, but the
  reason is page-truth, not pipeline-mirroring: a transparent paint is not on the page.

* **Gradient fills are real paint.** A gradient CTA's computed ``background-color`` is transparent
  — its brand colors live in ``bg_gradient_stops``. Those stops genuinely paint the page (and the
  pipeline consumes them), so a ``bg_grad`` channel counts them; without it a real gradient CTA
  reads as a phantom and an author wrongly drops it.

* **Hover/state colors are a separate axis.** A color that only appears as ``hover_bg`` is real
  but is not painted in the default render; it is reported as a ``[hover]`` diagnostic and kept
  OUT of the default-render GT (unless it also paints by default). If the pipeline fails to
  surface a genuine on-page state color, that is a harvest-completeness gap to note, not a GT
  exclusion.

* **Area-aware phantom check.** A color with zero elements on every channel is *not*
  automatically a phantom: it may be a real area color with no single element bg (gradient,
  background-image, body/inherited, pseudo-element). This reports each candidate's element
  counts AND its screenshot-bin area AND whether it sits *between* two element-supported colors
  (a median-cut quantizer blend), so the author omits a true phantom but keeps a real area
  color (verifying the latter is a real rendered surface, e.g. a gradient/image, not an artifact).

* **Operationalized "real use" for interactive roles.** For ``cta``/``action``/``link`` a color
  earns the role only if enough *clickable* elements paint it — a single non-clickable 10x10
  status dot is not a CTA. The per-channel breakdown reports the clickable share.

* **Keyed to the emitted cluster.** Each probed hex reports the nearest pipeline *cluster* color;
  the GT hex is keyed to that emitted hex when it is within tolerance, so the authored hex is
  deterministic rather than author-chosen among sub-tolerance variants. When the nearest cluster
  is beyond tolerance the pipeline does not surface the color — the author still records the
  page-true hex and the eval then flags the pipeline gap.

Usage:
  uv run python eval/probe.py <site>                 # full shaped output (role/color views)
  uv run python eval/probe.py <site> '#08872b' ...   # + element-evidence for each hex
"""

from __future__ import annotations

import gzip
import sys
from dataclasses import dataclass
from pathlib import Path

from colormetric import pdelta  # same dir; perceptual ΔE2000 identity metric
from score import DEFAULT_TOLERANCE  # same dir; one tolerance, shared with the scorer

from colorsense.classify.components import classify_components
from colorsense.classify.tokens import classify_tokens
from colorsense.color.primitives import delta_e, parse_css_color
from colorsense.config import load_default_config
from colorsense.models import Color, Harvest
from colorsense.palette.inventory import build_inventory
from colorsense.palette.reconcile import reconcile
from colorsense.palette.usage import build_color_index, build_usage

HARVEST_DIR = Path(__file__).parent / "harvests"

# A screenshot bin smaller than this is treated as "no real area" for the phantom check.
NEGLIGIBLE_AREA: float = 0.002
# The blend check below is GEOMETRIC, not perceptual-identity: it asks whether a screenshot bin
# sits on the straight line between two real element colors (the signature of a median-cut
# quantizer blend), which is a question about position in the quantizer's color space. It stays
# in OKLab (via `delta_e`) with its own OKLab-unit constants — distinct from the ΔE2000
# IDENTITY_TOLERANCE used for "is this the same color" decisions.
# Collinearity residual (OKLab) below which a candidate counts as a blend of two element colors.
BLEND_RESIDUAL: float = 0.015
# OKLab self-exclusion: element colors nearer than this to the target are "self", not endpoints.
BLEND_SELF_OKLAB: float = 0.05


def _load(name: str) -> Harvest:
    with gzip.open(HARVEST_DIR / f"{name}.json.gz") as fh:
        return Harvest.model_validate_json(fh.read())


def _dump_output(name: str, harvest: Harvest) -> list[Color]:
    """Print the full shaped pipeline output and return the emitted cluster colors.

    The returned cluster colors are the pipeline's *emitted* hexes; per-hex probing keys GT
    colors to them (MAJOR 1), so this computes them once and hands them back.
    """
    cfg = load_default_config()
    ct = classify_tokens(harvest.tokens, cfg)
    ce = classify_components(harvest.elements, cfg, harvest.viewport)
    clusters = build_inventory(harvest, ce)
    color_index = build_color_index(clusters)
    usage = build_usage(clusters)
    posterior, _ = reconcile(usage, ct, measured_colors=[c.color for c in clusters])
    print(f"\n===== {name}  ({len(harvest.elements)} elements) =====")
    print("--- usage (role -> colors -> components) ---")
    for role, entries in posterior.mapping.items():
        if not entries:
            print(f"  {role.value:8}: (empty)")
            continue
        print(f"  {role.value}:")
        for e in entries:
            comps = {c.value: round(w, 2) for c, w in e.components.items()}
            print(f"     {e.color.hex}  p={e.probability:.2f} area={e.area:.3f}  {comps}")
    print("--- colors (color -> usages) [top 12 by prominence] ---")
    for cu in color_index[:12]:
        print(f"  {cu.color.hex}  prom={cu.prominence:.2f} area={cu.area:.3f}")
        for u in cu.usages:
            comps = {c.value: round(w, 2) for c, w in u.components.items()}
            print(f"     -> {u.role.value:8} {comps}")
    return [c.color for c in clusters]


@dataclass(frozen=True)
class ChannelEvidence:
    channel: str
    count: int
    clickable: int


def _channel_evidence(harvest: Harvest, target: Color, tol: float) -> list[ChannelEvidence]:
    out: list[ChannelEvidence] = []
    for chan in ("bg", "text", "border"):
        count = clickable = 0
        for el in harvest.elements:
            col = {"bg": el.bg, "text": el.text, "border": el.border}[chan]
            # ``alpha == 0`` paints nothing on the page (the CSS-default transparent
            # ``background-color``, a fully-transparent text/border) — counting it invents
            # hundreds-to-thousands of phantom "elements" for a color the surface never shows.
            # The pipeline discards it (``Color.alpha > 0`` gates in inventory.py / components.py);
            # page-truth says only painted color is evidence.
            if col is not None and col.alpha > 0.0 and pdelta(col, target) <= tol:
                count += 1
                clickable += int(el.clickable)
        out.append(ChannelEvidence(chan, count, clickable))
    # Gradient-stop fill: a clickable pill CTA paints its brand colors through gradient stops
    # while its computed ``background-color`` is transparent, so the flat-bg scan above is blind
    # to it (resend's #02fcef/#a02bfe CTA is the motivating case). The pipeline DOES consume these
    # (inventory._bg_fill_colors); they are real on-page paint. Stops can be partly transparent
    # (resend's are alpha 0.44) — the harvester voids any gradient containing a *fully* transparent
    # stop, but keeps partial ones, so gate on ``alpha > 0`` for the same page-truth reason as the
    # flat channels. Stops are only populated on clickable elements, so each match is clickable.
    g_count = g_clickable = 0
    for el in harvest.elements:
        if any(stop.alpha > 0.0 and pdelta(stop, target) <= tol for stop in el.bg_gradient_stops):
            g_count += 1
            g_clickable += int(el.clickable)
    out.append(ChannelEvidence("bg_grad", g_count, g_clickable))
    return out


def _bin_area(harvest: Harvest, target: Color, tol: float) -> float:
    return sum(b.area_fraction for b in harvest.screenshot_bins if pdelta(b.color, target) <= tol)


def _element_colors(harvest: Harvest) -> list[Color]:
    """The distinct colors actually painted by elements (the basis for the blend check).

    Only ``alpha > 0`` paint counts — a transparent paint is not a real color the blend check
    can sit between. Gradient stops are included: they are real on-page fills (a quantizer bin
    can legitimately blend a gradient stop with a flat surface).
    """
    seen: dict[str, Color] = {}
    for el in harvest.elements:
        for col in (el.bg, el.text, el.border):
            if col is not None and col.alpha > 0.0:
                seen.setdefault(col.hex, col)
        for stop in el.bg_gradient_stops:
            if stop.alpha > 0.0:
                seen.setdefault(stop.hex, stop)
    return list(seen.values())


def _is_blend(target: Color, element_colors: list[Color]) -> tuple[Color, Color] | None:
    """If ``target`` sits ~on the OKLab segment between two element colors, return that pair.

    A median-cut quantizer blend of two real colors lands near the straight line between them in
    OKLab — the signature of a phantom bin. This is geometric (OKLab units, ``delta_e``), separate
    from the perceptual ΔE2000 identity tolerance used elsewhere.
    """
    near = [c for c in element_colors if delta_e(c, target) > BLEND_SELF_OKLAB]  # exclude self
    best: tuple[Color, Color] | None = None
    best_res = BLEND_RESIDUAL
    for i, a in enumerate(near):
        for b in near[i + 1 :]:
            ab = delta_e(a, b)
            if ab <= 1e-6:
                continue
            residual = delta_e(target, a) + delta_e(target, b) - ab
            if 0 <= residual < best_res and delta_e(target, a) < ab and delta_e(target, b) < ab:
                best, best_res = (a, b), residual
    return best


def _verdict(
    channels: list[ChannelEvidence], area: float, blend: tuple[Color, Color] | None
) -> str:
    by_chan = {c.channel: c for c in channels}
    grad = by_chan["bg_grad"]
    bg = by_chan["bg"]
    flat_total = sum(by_chan[c].count for c in ("bg", "text", "border"))

    # Gradient stops fill clickable pill CTAs (the only place the harvester records them). Gate the
    # CTA recommendation on the ACTUAL clickable share rather than assuming it — a future non-
    # clickable decorative gradient must not earn a CTA recommendation. Appended (not returned
    # early) so a color that is ALSO a flat surface keeps that role in the verdict.
    if grad.count > 0 and grad.clickable > 0:
        grad_note = (
            f" PLUS a gradient stop paints {grad.clickable} clickable element(s) — also author "
            "cta/action (the pipeline surfaces these via bg_gradient_stops)."
        )
    elif grad.count > 0:
        grad_note = f" PLUS a NON-CLICKABLE gradient stop ({grad.count} element(s)) — decorative."
    else:
        grad_note = ""

    if flat_total > 0:
        if bg.count > 0 and bg.clickable == 0:
            base = (
                "ELEMENT-SUPPORTED, but bg is NON-CLICKABLE — a surface/badge, NOT cta/action "
                "(a status dot is not a CTA); author it only for surface/banner/page as fits"
            )
        else:
            base = "ELEMENT-SUPPORTED (real — author for the channels/roles its elements support)"
        return base + grad_note
    if grad.count > 0:
        if grad.clickable > 0:
            return (
                "GRADIENT-FILL (real — a gradient stop paints clickable pill CTAs here; author it "
                "for cta/action. The pipeline surfaces these via bg_gradient_stops)"
            )
        return (
            "GRADIENT-FILL but NON-CLICKABLE — a decorative gradient, NOT cta/action; author only "
            "as surface/banner if it's a real rendered region"
        )
    if blend is not None:
        pair = f"{blend[0].hex}+{blend[1].hex}"
        if area < NEGLIGIBLE_AREA:
            return f"PHANTOM blend of {pair}, ~0 area (omit — quantizer artifact)"
        # Collinear AND area-bearing is ambiguous: a quantizer blend OR a real two-stop
        # gradient midpoint. Don't say "omit" — that is exactly how an area-real color gets
        # silently dropped. Hedge to the same verify-real-surface instruction as AREA-ONLY.
        return (
            f"LIKELY quantizer blend of {pair}, BUT area={area:.3f} — could be a real gradient "
            "midpoint; VERIFY it's a real rendered surface (not an artifact) before omitting"
        )
    if area < NEGLIGIBLE_AREA:
        return "PHANTOM (0 elements, ~0 area — omit)"
    return (
        f"AREA-ONLY (0 elements but area={area:.3f}, not a blend) — a gradient/image/body bg? "
        "VERIFY it's a real rendered surface (not an artifact) before authoring or omitting"
    )


def _hover_evidence(harvest: Harvest, target: Color, tol: float) -> int:
    """Count elements whose HOVER background matches ``target`` (a hover/state-only color)."""
    return sum(
        1
        for el in harvest.elements
        if el.hover_bg is not None
        and el.hover_bg.alpha > 0.0
        and pdelta(el.hover_bg, target) <= tol
    )


def _nearest_cluster(
    target: Color, cluster_colors: list[Color], tol: float
) -> tuple[Color, float, bool] | None:
    """The pipeline cluster color nearest ``target`` — ``(color, ΔE, within_tol)`` or None.

    Authors key a GT hex to the pipeline's *emitted* cluster hex when one is within tolerance, so
    the GT hex is deterministic rather than author-chosen among sub-tol variants (#090909 vs
    #0a0a0a). When the nearest cluster is *beyond* tolerance the pipeline does not surface this
    color: page-truth still authors the real on-page hex, and the eval then flags the pipeline gap.
    """
    if not cluster_colors:
        return None
    best = min(cluster_colors, key=lambda c: (pdelta(c, target), c.hex))
    d = pdelta(best, target)
    return best, d, d <= tol


def _probe_hex(harvest: Harvest, hx: str, tol: float, cluster_colors: list[Color]) -> None:
    target = parse_css_color(hx)
    if target is None:
        print(f"  {hx}: unparseable", file=sys.stderr)
        return
    channels = _channel_evidence(harvest, target, tol)
    area = _bin_area(harvest, target, tol)
    blend = _is_blend(target, _element_colors(harvest))
    hover = _hover_evidence(harvest, target, tol)
    nearest = _nearest_cluster(target, cluster_colors, tol)
    print(f"\n  -- {hx} (ΔE<= {tol}) --")
    for ch in channels:
        print(f"     {ch.channel:6}: {ch.count:4d} elements  ({ch.clickable} clickable)")
    print(f"     screenshot area: {area:.4f}")
    if nearest is not None:
        col, d, within = nearest
        if within:
            print(f"     nearest pipeline cluster: {col.hex} (ΔE {d:.4f}) — key the GT hex to THIS")
        else:
            print(
                f"     nearest pipeline cluster: {col.hex} (ΔE {d:.4f} > tol) — pipeline does NOT "
                "surface this color; if it is genuinely on the page, author the page-true hex "
                "(the eval will then flag the pipeline gap)"
            )
    if hover > 0:
        print(
            f"     [hover] matches hover_bg on {hover} elements — a hover/interaction-STATE color, "
            "a SEPARATE axis from the default-render GT; do not add it to the default GT unless it "
            "also paints by default (see channels above)"
        )
    print(f"     => {_verdict(channels, area, blend)}")


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 0
    name, hexes = argv[0], argv[1:]
    harvest = _load(name)
    cluster_colors = _dump_output(name, harvest)
    if hexes:
        print("\n--- element evidence (authoring decision per candidate color) ---")
        for hx in hexes:
            _probe_hex(harvest, hx, DEFAULT_TOLERANCE, cluster_colors)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
