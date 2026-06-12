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
3. Softmax the surviving positive votes with ``softmax_temperature``, prune
   components below ``min_component_prob``, and renormalize the survivors.

Repetition is approximated at list level: this layer has no real DOM tree, so
"structurally similar siblings" are detected as elements sharing the same tag
*and* at least one class token. Any such group of at least
``repetition.min_siblings`` members satisfying ``requires_any`` receives the
repetition votes. (Golden case: 4 ``.card`` ``<div>`` siblings -> ``card_bg``.)
"""

from __future__ import annotations

import math

from colorsense.config import (
    Config,
    GeometryThresholds,
    VoteRule,
    WhenRule,
)
from colorsense.models import (
    ClassifiedElement,
    ComponentType,
    HarvestedElement,
    Viewport,
)

__all__ = ["classify_components"]

_DEFAULT_VIEWPORT = Viewport(width=1280, height=800, device_scale_factor=1.0)

# channel_routing sentinels that are NOT ComponentType values.
_NON_COMPONENT_VOTE_KEYS = frozenset({"ignore"})

# <input> type attributes that render as a real button (button chrome, button-styled
# background): these — and only these — make an input button-like for both the
# `input[submit]` semantic rule and the `input[submit|button]` interactivity predicate.
# "reset" and "image" are included deliberately: both paint as buttons, and this
# classifier scores visual roles, not form semantics. A missing/None type is NOT
# button-like — the HTML default type is "text".
_BUTTONLIKE_INPUT_TYPES = frozenset({"submit", "button", "image", "reset"})


def _add_votes(
    accum: dict[ComponentType, float],
    votes: dict[str, float],
) -> None:
    """Add a config vote dict (keyed by component-type strings) into ``accum``.

    Keys that are channel sentinels (e.g. ``"ignore"``) or otherwise not valid
    [`ComponentType`][colorsense.ComponentType] members are skipped rather than crashing.
    """
    for key, weight in votes.items():
        if key in _NON_COMPONENT_VOTE_KEYS:
            continue
        try:
            component = ComponentType(key)
        except ValueError:
            continue
        accum[component] = accum.get(component, 0.0) + weight


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

    def satisfies_requires_any(element: HarvestedElement) -> bool:
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
        return (
            "distinct_bg_from_parent" in requires_any
            and element.bg is not None
            and element.bg.alpha > 0.0
        )

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


def _apply_suppressors(
    accum: dict[ComponentType, float],
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
                for component in accum:
                    accum[component] *= suppressor.factor
        elif suppressor.applies_to == "brand_components":
            if _is_third_party(element):
                for raw in config.component_classifier.brand_components:
                    try:
                        component = ComponentType(raw)
                    except ValueError:
                        continue
                    if component in accum:
                        accum[component] *= suppressor.factor


def _finalize_distribution(
    accum: dict[ComponentType, float],
    config: Config,
) -> dict[ComponentType, float]:
    """Softmax positive votes, prune below threshold, and renormalize survivors.

    The prune/renormalize/argmax-fallback shape mirrors ``palette/_pruning.py``'s
    `prune_distribution`, but deliberately stays local: this ranks ``ComponentType``
    keys, not colors, so the palette helper's hex tie-break convention has no analogue
    here (and ``classify/`` does not depend on ``palette/``). The argmax fallback is
    deterministic regardless — ``accum`` is built in config-rule order.
    """
    cc = config.component_classifier
    positive = {comp: vote for comp, vote in accum.items() if vote > 0.0}
    if not positive:
        return {}

    # Max-shifted (matching palette/roles.py's _softmax_weights): mathematically the
    # probabilities are shift-invariant, but unshifted exp(vote/T) overflows once a
    # config's vote weights stack high enough relative to its temperature.
    temperature = cc.softmax_temperature
    max_vote = max(positive.values())
    exps = {comp: math.exp((vote - max_vote) / temperature) for comp, vote in positive.items()}
    total = sum(exps.values())
    probs = {comp: value / total for comp, value in exps.items()}

    survivors = {comp: p for comp, p in probs.items() if p >= cc.min_component_prob}
    if not survivors:
        # Pruning removed everything: keep the single argmax.
        argmax = max(probs, key=lambda comp: probs[comp])
        return {argmax: 1.0}

    survivor_total = sum(survivors.values())
    return {comp: p / survivor_total for comp, p in survivors.items()}


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
    cc = config.component_classifier
    thresholds = cc.geometry.thresholds

    repetition_members = _repetition_member_indices(elements, config)

    results: list[ClassifiedElement] = []
    for index, element in enumerate(elements):
        accum: dict[ComponentType, float] = {}

        # 1. Semantic tags / ARIA roles.
        for rule in cc.semantic_tags:
            if _matches_semantic_tag(rule, element):
                _add_votes(accum, rule.votes)

        # 2. Geometry / position.
        for geo_rule in cc.geometry.rules:
            if _matches_geometry(geo_rule.when, element, thresholds, active_viewport):
                _add_votes(accum, geo_rule.votes)

        # 3. Class / id token substring match.
        for rule in cc.class_tokens:
            if _matches_class_token(rule, element):
                _add_votes(accum, rule.votes)

        # 4. Interactivity.
        for when_rule in cc.interactivity:
            if _matches_interactivity(when_rule, element):
                _add_votes(accum, when_rule.votes)

        # 4b. Border presence: the harvester width-gates ``border``, so non-None
        # means the element genuinely paints one (see the YAML calibration comment).
        if element.border is not None:
            _add_votes(accum, cc.border_presence.votes)

        # 4c. Text presence: direct (non-descendant) text content on a NON-clickable
        # element. Clickable elements are excluded — their typography is interactive
        # by definition and already routed via the link rules (see the YAML comment).
        if element.has_text and not element.clickable:
            _add_votes(accum, cc.text_presence.votes)

        # 5. Repetition (the card detector).
        if index in repetition_members:
            _add_votes(accum, cc.repetition.votes)

        # 6. Origin / third-party.
        if element.is_iframe:
            _add_votes(accum, cc.third_party.votes_iframe)
        if element.cross_origin:
            _add_votes(accum, cc.third_party.votes_cross_origin)
        if element.shadow_host:
            _add_votes(accum, cc.third_party.votes_shadow_host)
        if element.vendor_match:
            _add_votes(accum, cc.third_party.votes_vendor_match)

        # Suppressors, then softmax/prune/renormalize.
        _apply_suppressors(accum, element, config)
        component_dist = _finalize_distribution(accum, config)

        results.append(ClassifiedElement(element=element, component_dist=component_dist))

    return results
