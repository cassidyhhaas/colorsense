"""Rule-based component classifier.

Scores each harvested DOM element into a probability distribution over
[`ComponentType`][colorsense.ComponentType]. Every weight, threshold, predicate,
and vocabulary entry is read from [`Config`][colorsense.Config] (the palette
configuration YAML); nothing is hard-coded here.

Scoring pipeline (per element):

1. Accumulate additive votes across eight feature families — semantic tags,
   geometry, class/id tokens, interactivity, border presence (the element
   genuinely paints a border), text presence (a non-clickable element with
   direct text content), repetition (the card detector), and
   origin/third-party.
2. Apply multiplicative suppressors (``aria_hidden`` / hidden-or-zero-area zero
   everything; ``third_party_present`` damps the configured brand components on
   third-party widgets).
3. Partition the surviving positive votes by property family
   (``ComponentType.property_family``) and, INDEPENDENTLY within each painted
   family, softmax with ``softmax_temperature``, prune components below
   ``min_component_prob``, and renormalize the survivors. Recombine the
   per-family sub-distributions with family weights summing to 1.0 so the
   element distribution still sums to ~1.0. Single-family elements are
   byte-identical to a global softmax (see `_finalize_distribution`).

Repetition is approximated at list level: this layer has no real DOM tree, so
"structurally similar siblings" are detected as elements sharing the same tag
*and* at least one class token. Any such group of at least
``repetition.min_siblings`` members satisfying ``requires_any`` receives the
repetition votes. (Golden case: 4 ``.card`` ``<div>`` siblings -> ``card_bg``.)
"""

from __future__ import annotations

import math

from colorsense.color.primitives import ciede2000, contrast_ratio, is_opaque, is_painting
from colorsense.config import (
    Config,
    ContrastRelabelConfig,
    GeometryThresholds,
    PageCanvasFallbackConfig,
    VoteRule,
    WhenRule,
)
from colorsense.models import (
    ClassifiedElement,
    Color,
    ComponentType,
    HarvestedElement,
    PropertyFamily,
    Viewport,
    is_circle_shape,
    is_pill_shape,
)

__all__ = ["classify_components"]

_DEFAULT_VIEWPORT = Viewport(width=1280, height=800, device_scale_factor=1.0)

# Vote-routing sentinels that are NOT ComponentType values.
_NON_COMPONENT_VOTE_KEYS = frozenset({"ignore"})

# <input> type attributes that render as a real button (button chrome, button-styled
# background): these — and only these — make an input button-like for both the
# `input[submit]` semantic rule and the `input[submit|button]` interactivity predicate.
# "reset" and "image" are included deliberately: both paint as buttons, and this
# classifier scores visual roles, not form semantics. A missing/None type is NOT
# button-like — the HTML default type is "text".
_BUTTONLIKE_INPUT_TYPES = frozenset({"submit", "button", "image", "reset"})


def _add_votes(
    vote_totals: dict[ComponentType, float],
    votes: dict[str, float],
) -> None:
    """Add a config vote dict (keyed by component-type strings) into ``vote_totals``.

    Keys that are routing sentinels (e.g. ``"ignore"``) or otherwise not valid
    [`ComponentType`][colorsense.ComponentType] members are skipped rather than crashing.
    """
    for key, weight in votes.items():
        if key in _NON_COMPONENT_VOTE_KEYS:
            continue
        try:
            component = ComponentType(key)
        except ValueError:
            continue
        vote_totals[component] = vote_totals.get(component, 0.0) + weight


def _is_pill(element: HarvestedElement) -> bool:
    """Whether the element is a fully-rounded, elongated **pill/chip** shape.

    Pure shape: all four corners fully rounded (the ``min_corner_radius >= height/2``
    test) and wider than tall (excluding circles, where ``width == height``). This is
    intentionally size-agnostic — so the card-detector exclusion ("a stadium shape is
    never a card") applies at any size — while the badge *rule* layers the size/text gates
    on top.
    """
    return is_pill_shape(element.rect.width, element.rect.height, element.min_corner_radius)


def _is_small_circle(element: HarvestedElement, max_h_px: float) -> bool:
    """Whether the element is a small fully-rounded **circle/dot** (``rounded-full``, w==h).

    Pure shape plus a single absolute-size cap: a circle (`is_circle_shape`) no taller than
    ``max_h_px`` — a UI chip/dot, not a large circular avatar or thumbnail. Used both to keep
    small circles OUT of the card detector (a small circle is never a card) and to gate the
    circle-badge promotion (which layers clickable + recurrence on top).
    """
    return (
        is_circle_shape(element.rect.width, element.rect.height, element.min_corner_radius)
        and element.rect.height <= max_h_px
    )


def _paints_visible_fill(element: HarvestedElement) -> bool:
    """Whether the element paints a visible fill: a non-transparent bg, border, or ring.

    Gates the badge rule so a decorative fully-rounded divider (a ``rounded-full`` bar
    with a transparent/gradient background and no border) is not mislabeled a badge — a
    colored chip always paints one of these.
    """
    return is_painting(element.bg) or element.border is not None or element.has_box_shadow


def _paints_button_surface(element: HarvestedElement) -> bool:
    """Whether the element paints a **button-like** surface: a non-transparent bg or a border.

    A button surface is a solid/opaque ``bg``, a ``border``, or a gradient fill
    (``bg_gradient_stops`` — populated only on clickable gradient pill CTAs, whose computed
    ``bg`` is transparent; see `HarvestedElement`). It DIFFERS from `_paints_visible_fill` (they
    do not nest): this counts gradient stops but a box-shadow alone does NOT count, whereas
    `_paints_visible_fill` counts a box-shadow but not gradient stops. It gates the anchor
    routing (the ``a & button_surface`` interactivity predicate) — a filled, outlined, or
    gradient-pill anchor is a button-styled CTA whose label text is `cta_text` (a CTA
    label, carried by no usage role), while a bare anchor — even an elevated, shadow-only
    one such as a text link with a focus ring — stays a genuine inline ``link``.
    """
    return (
        is_painting(element.bg) or element.border is not None or len(element.bg_gradient_stops) > 0
    )


def _derive_page_canvas_color(elements: list[HarvestedElement]) -> Color | None:
    """Best-effort page canvas color: the surface a genuine inline link reads against.

    Classify does not otherwise know the page background. Derived deterministically with a
    cheap pre-pass: prefer the ``<html>``/``<body>`` element's own opaque ``bg`` (the
    canonical canvas), else the largest-area element that paints an opaque background.
    Returns ``None`` only when nothing on the page paints an opaque background — in which
    case the CTA-label relabel cannot fire (no canvas to compare against) and every
    clickable label keeps its ``link`` vote. ``elements`` is in deterministic document
    order, so the ``max``-area tie-break (first-seen on equal area) is stable.
    """

    def opaque_bg(element: HarvestedElement) -> Color | None:
        bg = element.bg
        return bg if is_opaque(bg) else None

    # Canonical canvas: the root document elements, outermost first.
    for tag in ("html", "body"):
        for element in elements:
            if element.tag == tag:
                bg = opaque_bg(element)
                if bg is not None:
                    return bg

    # Fallback: the largest opaque painted surface.
    best: Color | None = None
    best_area = -1.0
    for element in elements:
        bg = opaque_bg(element)
        if bg is None:
            continue
        area = element.rect.width * element.rect.height
        if area > best_area:
            best_area = area
            best = bg
    return best


def _canonical_canvas_is_opaque(elements: list[HarvestedElement]) -> bool:
    """Whether the canonical canvas (``html``/``body``/``main``) paints an opaque background.

    The gate for the page-canvas fallback: a site whose root document elements all leave
    ``background: transparent`` (alpha 0) paints its page color somewhere else (a
    full-viewport ``<div>``), which is exactly the case the fallback exists to repair. An
    opaque-body site returns ``True`` and is never touched.
    """
    for element in elements:
        if element.tag in {"html", "body", "main"} and is_opaque(element.bg):
            return True
    return False


def _page_canvas_index(
    elements: list[HarvestedElement],
    thresholds: GeometryThresholds,
    viewport: Viewport,
    page_canvas: Color | None,
    fallback: PageCanvasFallbackConfig,
) -> int | None:
    """Index of the fallback page-canvas element, or ``None`` if no fallback applies.

    Returns an index ONLY when the canonical canvas (``html``/``body``/``main``) paints no
    opaque background — otherwise the canonical canvas is the page surface and the fallback
    must not fire. When it does apply, the canvas is the largest-area element that spans the
    viewport width (``full_width``) near the top of the page (``top < top_band``), paints an
    opaque background, AND whose color matches the independently-derived page-canvas color
    within ``color_match_delta_e`` (CIEDE2000). The color match is the safety gate: without
    it the bare "largest full-width top-band opaque element" can be a brand-colored hero or
    banner larger than the real canvas wrapper, and the fallback would mislabel that hero as
    ``page`` and erase its surface votes. Requiring the picked element to actually paint the
    page color makes the fallback pick the canvas wrapper, never a differently-colored hero.
    ``elements`` is in deterministic document order, so the ``max``-area tie-break (first-seen
    on equal area) is stable.
    """
    if _canonical_canvas_is_opaque(elements) or page_canvas is None:
        return None

    vp_w = float(viewport.width)
    vp_h = float(viewport.height)
    if vp_w <= 0.0 or vp_h <= 0.0:
        return None

    best_index: int | None = None
    best_area = -1.0
    for index, element in enumerate(elements):
        bg = element.bg
        if bg is None or not is_opaque(bg):
            continue
        if ciede2000(bg, page_canvas) > fallback.color_match_delta_e:
            continue
        rect = element.rect
        full_width = (rect.width / vp_w) >= thresholds.full_width
        top = rect.y / vp_h
        if not (full_width and top < thresholds.top_band):
            continue
        area = rect.width * rect.height
        if area > best_area:
            best_area = area
            best_index = index
    return best_index


def _is_cta_label(
    element: HarvestedElement,
    page_canvas: Color | None,
    relabel: ContrastRelabelConfig,
) -> bool:
    """Whether a non-anchor clickable's text is a CTA-button LABEL, not an inline link.

    The theme/contrast-relative discriminator. Four clauses, each guarding a distinct
    failure mode so that genuine inline links survive — confirmed against the eval panel:

    1. **Sits on an interactive fill** (``effective_bg_from_clickable``): the composited
       background was painted by a clickable ancestor (the button), not a passive page or
       section surface. Protects a non-anchor link inside a passive dark hero/section,
       whose effective background is the section, not a button.
    2. **The fill is a distinct surface** (``ciede2000(effective_bg, page_canvas) >
       canvas_delta_e``): a clickable element whose fill IS the page color is effectively
       on the page (text reads against the canvas) — keep it a link. This is an *identity*
       comparison, so it uses **CIEDE2000** (not OKLab ``delta_e``): page canvases are
       near-white/near-black, exactly where OKLab ΔE is least accurate.
    3. **The text is legible on the fill** (``contrast_ratio(text, effective_bg) >=
       wcag_min_contrast``): a real label's color is chosen to READ on its button. A
       brand-colored link that merely overlaps a soft tinted clickable card (e.g. stripe's
       orange ``#ff6118`` on a peach ``#ffe0d1`` fill, contrast ~2.4) is decorative
       low-contrast styling, not a label — keep it a link.
    4. **The text is illegible on the canvas** (``contrast_ratio(text, page_canvas) <
       wcag_min_contrast``): a genuine inline link must be readable as page text. If the
       text reads fine on the canvas it is not exclusively a button label — keep it a link
       (protects dark text on a light tinted button, which is also a valid body/link color).

    Anchors are excluded (``tag != "a"``): they are already routed by the
    ``a & button_surface`` / ``a & !button_surface`` rules.

    Known boundary (intentional): a fully *clickable card* — an entire content tile wrapped
    in a link, its text on the card's own dark fill — relabels its text to ``cta_text`` too.
    That is acceptable: a clickable element's text never reaches the ``text`` role anyway
    (text-presence excludes clickables), so before this rule the card text landed only in
    ``link`` (not a genuine inline link either); routing it to the CTA-label sink is at least
    as correct, and no panel site regresses.
    """
    if element.tag == "a" or not element.clickable:
        return False
    if not element.effective_bg_from_clickable:
        return False
    text = element.text
    effective_bg = element.effective_bg
    if text is None or effective_bg is None or page_canvas is None:
        return False
    if ciede2000(effective_bg, page_canvas) <= relabel.canvas_delta_e:
        return False
    return (
        contrast_ratio(text, effective_bg)
        >= relabel.wcag_min_contrast
        > contrast_ratio(text, page_canvas)
    )


def _matches_semantic_tag(rule: VoteRule, element: HarvestedElement) -> bool:
    """Return whether a semantic-tag rule matches the element's tag/role/input type.

    ``input[submit]`` matches only inputs whose harvested ``type`` attribute is
    button-like (see `_BUTTONLIKE_INPUT_TYPES`). Regression guard: it used to
    match EVERY ``<input>``, giving search/text inputs a spurious cta_bg vote.
    """
    match = rule.match
    if match.startswith("role="):
        return element.role == match[len("role=") :]
    if match == "input[submit]":
        return element.tag == "input" and element.input_type in _BUTTONLIKE_INPUT_TYPES
    # Bare tag name.
    return element.tag == match


def _matches_interactivity(rule: WhenRule, element: HarvestedElement) -> bool:
    """Return whether an interactivity ``when`` predicate holds for the element."""
    when = rule.when
    if when == "clickable":
        return element.clickable
    if when == "input[submit|button]":
        # Inputs are gated on the harvested type attribute, not `clickable`: a text
        # input styled with cursor:pointer (or carrying onclick) is still a text
        # input, not a button. <button> keeps the clickable gate.
        if element.tag == "input":
            return element.input_type in _BUTTONLIKE_INPUT_TYPES
        return element.tag == "button" and element.clickable
    if when == "has_hover_color_change":
        return element.has_hover_color_change
    if when == "a & button_surface":
        # A button-styled anchor (solid bg or border): its label text is a CTA label.
        return element.tag == "a" and _paints_button_surface(element)
    if when == "a & !button_surface":
        # A bare anchor (no button surface): a genuine inline text link.
        return element.tag == "a" and not _paints_button_surface(element)
    # Unreachable for validated configs: Config rejects unknown predicates at load time.
    return False


def _matches_geometry(
    when: str,
    element: HarvestedElement,
    thresholds: GeometryThresholds,
    viewport: Viewport,
) -> bool:
    """Dispatch the geometry ``when`` predicates (validated at config load)."""
    rect = element.rect
    vp_w = float(viewport.width)
    vp_h = float(viewport.height)
    if vp_w <= 0.0 or vp_h <= 0.0:
        return False

    full_width = (rect.width / vp_w) >= thresholds.full_width
    top = rect.y / vp_h
    height = rect.height / vp_h
    area = (rect.width * rect.height) / (vp_w * vp_h)

    if when == "full_width & top<top_band & h<short_h":
        return full_width and top < thresholds.top_band and height < thresholds.short_h
    if when == "position in (fixed,sticky) & top<sticky_top_px":
        return element.position in {"fixed", "sticky"} and rect.y < thresholds.sticky_top_px
    if when == "full_width & top<top_band & h>=hero_min_h":
        return full_width and top < thresholds.top_band and height >= thresholds.hero_min_h
    if when == "top>=bottom_band & full_width":
        return top >= thresholds.bottom_band and full_width
    if when == "area<=small_area & clickable":
        return area <= thresholds.small_area and element.clickable
    if when == "pill & paints_fill & has_text & h<=badge_max_h_px":
        return (
            _is_pill(element)
            and _paints_visible_fill(element)
            and element.has_text
            and element.rect.height <= thresholds.badge_max_h_px
        )
    return False


def _matches_class_token(rule: VoteRule, element: HarvestedElement) -> bool:
    """Fuzzy substring match of a rule against class tokens or id (lowercased)."""
    needle = rule.match.lower()
    for token in element.class_tokens:
        if needle in token.lower():
            return True
    return element.id is not None and needle in element.id.lower()


def _is_third_party(element: HarvestedElement) -> bool:
    """Whether the element looks like a third-party widget."""
    return element.is_iframe or element.cross_origin or element.vendor_match or element.shadow_host


def _repetition_member_indices(
    elements: list[HarvestedElement],
    config: Config,
) -> set[int]:
    """Indices of elements belonging to a qualifying repeated-sibling group.

    Groups are formed by ``(tag, shared-class-token)``: two elements are grouped
    if they share a tag and at least one class token. A group qualifies when it
    has at least ``min_siblings`` members and each member satisfies
    ``requires_any``. This is a list-level heuristic standing in for real DOM
    sibling/structural-similarity analysis.
    """
    rep = config.component_classifier.repetition
    requires_any = set(rep.requires_any)
    circle_max_h = config.component_classifier.circle_badge.max_h_px

    def satisfies_requires_any(element: HarvestedElement) -> bool:
        # A pill/chip is never a card, however much it repeats: fully-rounded badges
        # (status pills, category chips) commonly recur in grids and carry a ring/bg,
        # which would otherwise satisfy the card heuristic and flood `card_bg` with their
        # accent colors. They are routed to `badge` by the geometry rule instead.
        # A small fully-rounded circle (a dot / icon-only corner chip) is excluded for the
        # same reason — `_is_pill` excludes circles (width == height), so they are handled
        # here so a recurring or decorative `rounded-full` chip never leaks into `card_bg`.
        if _is_pill(element) or _is_small_circle(element, circle_max_h):
            return False
        if not requires_any:
            return True
        if "box_shadow" in requires_any and element.has_box_shadow:
            return True
        # ``border`` is now width-gated at harvest time, so non-None means the element
        # genuinely paints a border. ``distinct_bg_from_parent`` remains approximated as
        # "paints any background" (no parent info at this layer) — which requires a
        # non-transparent color: the default ``background-color: transparent`` computes
        # to an ``alpha == 0`` Color, and treating it as a background made every run of
        # repeated text spans (e.g. ``.muted`` metadata) a false-positive "card" whose
        # repetition votes crushed their text-presence votes.
        if "border" in requires_any and element.border is not None:
            return True
        return "distinct_bg_from_parent" in requires_any and is_painting(element.bg)

    # Bucket element indices by (tag, class-token).
    buckets: dict[tuple[str, str], list[int]] = {}
    for index, element in enumerate(elements):
        for token in element.class_tokens:
            buckets.setdefault((element.tag, token.lower()), []).append(index)

    members: set[int] = set()
    for indices in buckets.values():
        qualifying = [i for i in indices if satisfies_requires_any(elements[i])]
        if len(qualifying) >= rep.min_siblings:
            members.update(qualifying)
    return members


def _circle_badge_member_indices(
    elements: list[HarvestedElement],
    config: Config,
) -> set[int]:
    """Indices of small clickable circular chips that RECUR as a structurally-similar group.

    A perfect circle is not a pill, so the badge geometry rule skips it; an icon-only
    circular chip (no text node) then falls through to ``card_bg``. This detector promotes
    such a chip to ``badge`` only when it (1) is a small circle (`_is_small_circle`), (2) is
    clickable, (3) paints a fill, and (4) belongs to a ``(tag, shared-class-token)`` group of
    at least ``min_siblings`` members all meeting (1)-(3). The grouping mirrors
    `_repetition_member_indices` — the same list-level stand-in for DOM sibling similarity.

    The recurrence gate is the load-bearing discriminator: supabase's 54 identical black
    corner badges form a qualifying group (→ ``action``), while a LONE clickable status dot
    (e.g. vercel's single ``status-dot``) never reaches ``min_siblings`` and is left alone, so
    its color keeps winning ``link`` from the text channel rather than being stolen into
    ``action``. A non-clickable decorative dot is excluded by the clickable gate.
    """
    circle_cfg = config.component_classifier.circle_badge
    max_h = circle_cfg.max_h_px

    def is_chip(element: HarvestedElement) -> bool:
        return (
            element.clickable and _is_small_circle(element, max_h) and _paints_visible_fill(element)
        )

    buckets: dict[tuple[str, str], list[int]] = {}
    for index, element in enumerate(elements):
        if not is_chip(element):
            continue
        for token in element.class_tokens:
            buckets.setdefault((element.tag, token.lower()), []).append(index)

    members: set[int] = set()
    for indices in buckets.values():
        if len(indices) >= circle_cfg.min_siblings:
            members.update(indices)
    return members


def _apply_suppressors(
    vote_totals: dict[ComponentType, float],
    element: HarvestedElement,
    config: Config,
) -> None:
    """Apply multiplicative suppressors to the accumulated votes in place."""
    suppressors = config.component_classifier.suppressors
    rect = element.rect

    for key, suppressor in suppressors.items():
        if suppressor.applies_to == "all":
            triggered = False
            if key == "aria_hidden":
                triggered = element.aria_hidden
            elif key == "zero_area_or_hidden":
                triggered = (not element.visible) or rect.width <= 0.0 or rect.height <= 0.0
            if triggered:
                for component in vote_totals:
                    vote_totals[component] *= suppressor.factor
        elif suppressor.applies_to == "brand_components":
            if _is_third_party(element):
                for raw in config.component_classifier.brand_components:
                    try:
                        component = ComponentType(raw)
                    except ValueError:
                        continue
                    if component in vote_totals:
                        vote_totals[component] *= suppressor.factor


def _softmax_prune_renormalize(
    votes: dict[ComponentType, float],
    temperature: float,
    min_component_prob: float,
) -> dict[ComponentType, float]:
    """Softmax a pool of positive votes, prune below threshold, renormalize survivors.

    The prune/renormalize/argmax-fallback shape mirrors ``palette/_pruning.py``'s
    `prune_distribution`, but deliberately stays local: this ranks ``ComponentType``
    keys, not colors, so the palette helper's hex tie-break convention has no analogue
    here. The argmax fallback is deterministic regardless — ``votes`` preserves
    config-rule insertion order. ``votes`` must be non-empty and all-positive.
    """
    # Max-shifted softmax: mathematically the probabilities are shift-invariant, but
    # unshifted exp(vote/T) overflows once a config's vote weights stack high enough
    # relative to its temperature.
    max_vote = max(votes.values())
    exp_weights = {
        component: math.exp((vote - max_vote) / temperature) for component, vote in votes.items()
    }
    total = sum(exp_weights.values())
    probabilities = {component: value / total for component, value in exp_weights.items()}

    survivors = {
        component: probability
        for component, probability in probabilities.items()
        if probability >= min_component_prob
    }
    if not survivors:
        # Pruning removed everything: keep the single argmax.
        argmax = max(probabilities, key=lambda component: probabilities[component])
        return {argmax: 1.0}

    survivor_total = sum(survivors.values())
    return {component: probability / survivor_total for component, probability in survivors.items()}


def _recombination_weights(
    by_family: dict[PropertyFamily, dict[ComponentType, float]],
) -> dict[PropertyFamily, float]:
    """Property-family weights (summing to 1.0 across painted families) for recombination.

    Vote-mass-share: ``w[family] = (raw positive votes in family) / (all raw
    positive votes)``. The family that accumulated more/stronger raw votes
    dominates the recombination, so a faint secondary family cannot dilute a
    strongly-evidenced primary one. With a single painted family this returns
    ``{family: 1.0}``.
    """
    family_mass = {family: sum(votes.values()) for family, votes in by_family.items()}
    total_mass = sum(family_mass.values())
    return {family: mass / total_mass for family, mass in family_mass.items()}


def _finalize_distribution(
    vote_totals: dict[ComponentType, float],
    config: Config,
) -> dict[ComponentType, float]:
    """Per-family softmax/prune/renormalize, recombined with property-family weights.

    The positive votes are partitioned by property family
    (``ComponentType.property_family``, the single source of truth shared with
    ``palette/inventory.py``; ``classify/`` does not import from ``palette/``). Each painted
    family — one with >=1 positive vote — gets its OWN softmax/prune/renormalize
    sub-distribution summing to 1.0, and the sub-distributions are recombined with family
    weights summing to 1.0, so the element distribution still sums to ~1.0.

    The point of per-family normalization: an element's text-family and background-family
    votes no longer compete in one global softmax. A filled clickable ``<a>`` (a gradient CTA
    pill) carries a large ``link`` (text-family) vote and a small ``cta_bg`` (background-family)
    vote; globally the softmax starved the background vote, so the pill's fill got ~no
    attribution. Per-family, the lone ``cta_bg`` normalizes to 1.0 within the background
    partition and the fill attributes at full background-family strength.

    INVARIANT — single-family elements are byte-identical to the former global softmax:
    when every positive vote falls in ONE family, ``by_family`` has one entry, its
    recombination weight is 1.0, and the within-family
    softmax/prune/renorm is exactly the old global computation. So plain text / plain
    surface / single-family elements (the large majority) are unchanged; only
    multi-family elements differ.
    """
    classifier_config = config.component_classifier
    # Suppressors have already been applied upstream; they are multiplicative and never
    # produce negatives, so a non-positive vote is just an absent contribution.
    positive = {component: vote for component, vote in vote_totals.items() if vote > 0.0}
    if not positive:
        return {}

    # Partition positive votes by family (a family is "painted" iff it has >=1 vote).
    by_family: dict[PropertyFamily, dict[ComponentType, float]] = {}
    for component, vote in positive.items():
        by_family.setdefault(component.property_family, {})[component] = vote

    weights = _recombination_weights(by_family)

    distribution: dict[ComponentType, float] = {}
    for family, votes in by_family.items():
        family_distribution = _softmax_prune_renormalize(
            votes,
            classifier_config.softmax_temperature,
            classifier_config.min_component_prob,
        )
        family_weight = weights[family]
        for component, probability in family_distribution.items():
            distribution[component] = family_weight * probability
    return distribution


def classify_components(
    elements: list[HarvestedElement],
    config: Config,
    viewport: Viewport | None = None,
) -> list[ClassifiedElement]:
    """Classify harvested elements into per-component probability distributions.

    Each element is scored with the rule-based scorer defined entirely in the
    component-classifier section of ``config``. ``viewport`` supplies the frame
    of reference for the geometry feature family; when ``None`` a default
    1280x800 viewport is used so geometry fractions remain computable.
    """
    active_viewport = viewport if viewport is not None else _DEFAULT_VIEWPORT
    classifier_config = config.component_classifier
    thresholds = classifier_config.geometry.thresholds

    repetition_members = _repetition_member_indices(elements, config)
    circle_badge_members = _circle_badge_member_indices(elements, config)
    page_canvas = _derive_page_canvas_color(elements)
    relabel_config = classifier_config.contrast_relabel
    canvas_fallback = classifier_config.page_canvas_fallback
    page_canvas_index = _page_canvas_index(
        elements, thresholds, active_viewport, page_canvas, canvas_fallback
    )

    results: list[ClassifiedElement] = []
    for index, element in enumerate(elements):
        vote_totals: dict[ComponentType, float] = {}

        # 1. Semantic tags / ARIA roles.
        for rule in classifier_config.semantic_tags:
            if _matches_semantic_tag(rule, element):
                _add_votes(vote_totals, rule.votes)

        # 2. Geometry / position.
        for geo_rule in classifier_config.geometry.rules:
            if _matches_geometry(geo_rule.when, element, thresholds, active_viewport):
                _add_votes(vote_totals, geo_rule.votes)

        # 3. Class / id token substring match.
        for rule in classifier_config.class_tokens:
            if _matches_class_token(rule, element):
                _add_votes(vote_totals, rule.votes)

        # 4. Interactivity.
        for when_rule in classifier_config.interactivity:
            if _matches_interactivity(when_rule, element):
                _add_votes(vote_totals, when_rule.votes)

        # 4b. Border presence: the harvester width-gates ``border``, so non-None
        # means the element genuinely paints one (see the YAML calibration comment).
        if element.border is not None:
            _add_votes(vote_totals, classifier_config.border_presence.votes)

        # 4c. Text presence: direct (non-descendant) text content on a NON-clickable
        # element. Clickable elements are excluded — their typography is interactive
        # by definition and already routed via the link rules (see the YAML comment).
        if element.has_text and not element.clickable:
            _add_votes(vote_totals, classifier_config.text_presence.votes)

        # 5. Repetition (the card detector).
        if index in repetition_members:
            _add_votes(vote_totals, classifier_config.repetition.votes)

        # 5b. Circle-badge group: a small clickable circular chip that recurs as a group is
        # a badge (-> action), not a card. The detector already enforces clickable + small
        # circle + fill + recurrence, so a lone status dot and a decorative dot never land
        # here (see `_circle_badge_member_indices`).
        if index in circle_badge_members:
            _add_votes(vote_totals, classifier_config.circle_badge.votes)

        # 6. Origin / third-party.
        if element.is_iframe:
            _add_votes(vote_totals, classifier_config.third_party.votes_iframe)
        if element.cross_origin:
            _add_votes(vote_totals, classifier_config.third_party.votes_cross_origin)
        if element.shadow_host:
            _add_votes(vote_totals, classifier_config.third_party.votes_shadow_host)
        if element.vendor_match:
            _add_votes(vote_totals, classifier_config.third_party.votes_vendor_match)

        # 7. CTA-label contrast relabel. A non-anchor clickable whose label text sits on its
        # OWN distinct interactive fill (legible there, illegible on the page canvas) is a
        # CTA-button LABEL, not an inline link. RELABEL all its accumulated text-family
        # `link` mass to `cta_text` (the unrouted button-label sink). A structural mass-MOVE,
        # not a per-rule tweak, because `link` reaches such an element from several feature
        # families at once (the generic `clickable` rule AND the `area<=small_area & clickable`
        # geometry rule are both link sources on a small CTA label); moving the accumulated
        # total is the only way to fully re-route it. The move preserves total text-family
        # mass (`cta_text.property_family == link.property_family == PropertyFamily.TEXT`), so
        # the per-family recombination weights are unchanged and the background family never
        # gains mass — the hard S27 lesson that DELETING link regresses cta/surface noise.
        if _is_cta_label(element, page_canvas, relabel_config):
            link_mass = vote_totals.pop(ComponentType.LINK, 0.0)
            if link_mass > 0.0:
                vote_totals[ComponentType.CTA_TEXT] = (
                    vote_totals.get(ComponentType.CTA_TEXT, 0.0) + link_mass
                )

        # 8. Page-canvas fallback. On a transparent-canonical-canvas site, the single
        # full-viewport element carrying the page color reads as a hero (full-width, tall)
        # and a repetition card (shared layout token), burying its weak page_bg signal.
        # Inject a page_bg prior on that one element and clear the competing hero/card
        # votes so the page color reaches the `page` role. Gated entirely on
        # `page_canvas_index` being None for opaque-body sites (see `_page_canvas_index`).
        if index == page_canvas_index:
            for raw in canvas_fallback.suppress:
                try:
                    suppressed = ComponentType(raw)
                except ValueError:
                    continue
                vote_totals.pop(suppressed, None)
            vote_totals[ComponentType.PAGE_BG] = (
                vote_totals.get(ComponentType.PAGE_BG, 0.0) + canvas_fallback.page_bg_vote
            )

        # Suppressors, then softmax/prune/renormalize.
        _apply_suppressors(vote_totals, element, config)
        component_dist = _finalize_distribution(vote_totals, config)

        results.append(ClassifiedElement(element=element, component_dist=component_dist))

    return results
