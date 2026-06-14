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
3. Partition the surviving positive votes by color channel
   (``models.channel_for``) and, INDEPENDENTLY within each painted channel,
   softmax with ``softmax_temperature``, prune components below
   ``min_component_prob``, and renormalize the survivors. Recombine the
   per-channel sub-distributions with channel weights summing to 1.0 so the
   element distribution still sums to ~1.0. Single-channel elements are
   byte-identical to a global softmax (see `_finalize_distribution`).

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
    channel_for,
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
    vote_totals: dict[ComponentType, float],
    votes: dict[str, float],
) -> None:
    """Add a config vote dict (keyed by component-type strings) into ``vote_totals``.

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
        vote_totals[component] = vote_totals.get(component, 0.0) + weight


def _is_pill(element: HarvestedElement) -> bool:
    """Whether the element is a fully-rounded, elongated **pill/chip** shape.

    Pure shape: all four corners fully rounded (the ``min_corner_radius >= height/2``
    test) and wider than tall (excluding circles, where ``width == height``). This is
    intentionally size-agnostic — so the card-detector exclusion ("a stadium shape is
    never a card") applies at any size — while the badge *rule* layers the size/text gates
    on top.
    """
    height = element.rect.height
    return (
        height > 0.0 and element.min_corner_radius >= height / 2.0 and element.rect.width > height
    )


def _paints_fill(element: HarvestedElement) -> bool:
    """Whether the element paints a visible fill: a non-transparent bg, border, or ring.

    Gates the badge rule so a decorative fully-rounded divider (a ``rounded-full`` bar
    with a transparent/gradient background and no border) is not mislabeled a badge — a
    colored chip always paints one of these.
    """
    return (
        (element.bg is not None and element.bg.alpha > 0.0)
        or element.border is not None
        or element.has_box_shadow
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
            and _paints_fill(element)
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

    def satisfies_requires_any(element: HarvestedElement) -> bool:
        # A pill/chip is never a card, however much it repeats: fully-rounded badges
        # (status pills, category chips) commonly recur in grids and carry a ring/bg,
        # which would otherwise satisfy the card heuristic and flood `card_bg` with their
        # accent colors. They are routed to `badge` by the geometry rule instead.
        if _is_pill(element):
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
    by_channel: dict[str, dict[ComponentType, float]],
) -> dict[str, float]:
    """Channel weights (summing to 1.0 across painted channels) for recombination.

    Vote-mass-share: ``w[channel] = (raw positive votes in channel) / (all raw
    positive votes)``. The channel that accumulated more/stronger raw votes
    dominates the recombination, so a faint secondary channel cannot dilute a
    strongly-evidenced primary one. With a single painted channel this returns
    ``{channel: 1.0}``.
    """
    channel_mass = {channel: sum(votes.values()) for channel, votes in by_channel.items()}
    total_mass = sum(channel_mass.values())
    return {channel: mass / total_mass for channel, mass in channel_mass.items()}


def _finalize_distribution(
    vote_totals: dict[ComponentType, float],
    config: Config,
) -> dict[ComponentType, float]:
    """Per-channel softmax/prune/renormalize, recombined with channel weights.

    The positive votes are partitioned by color channel (``models.channel_for``, the
    single source of truth shared with ``palette/inventory.py``; ``classify/`` does not
    import from ``palette/``). Each painted channel — one with >=1 positive vote — gets
    its OWN softmax/prune/renormalize sub-distribution summing to 1.0, and the
    sub-distributions are recombined with channel weights summing to 1.0, so the element
    distribution still sums to ~1.0.

    The point of per-channel normalization: an element's text-channel and bg-channel votes
    no longer compete in one global softmax. A filled clickable ``<a>`` (a gradient CTA
    pill) carries a large ``link`` (text-channel) vote and a small ``cta_bg`` (bg-channel)
    vote; globally the softmax starved the bg vote, so the pill's fill got ~no attribution.
    Per-channel, the lone ``cta_bg`` normalizes to 1.0 within the bg partition and the fill
    attributes at full bg-channel strength.

    INVARIANT — single-channel elements are byte-identical to the former global softmax:
    when every positive vote falls in ONE channel, ``by_channel`` has one entry, its
    recombination weight is 1.0, and the within-channel
    softmax/prune/renorm is exactly the old global computation. So plain text / plain
    surface / single-channel elements (the large majority) are unchanged; only
    multi-channel elements differ.
    """
    classifier_config = config.component_classifier
    # Suppressors have already been applied upstream; they are multiplicative and never
    # produce negatives, so a non-positive vote is just an absent contribution.
    positive = {component: vote for component, vote in vote_totals.items() if vote > 0.0}
    if not positive:
        return {}

    # Partition positive votes by channel (a channel is "painted" iff it has >=1 vote).
    by_channel: dict[str, dict[ComponentType, float]] = {}
    for component, vote in positive.items():
        by_channel.setdefault(channel_for(component), {})[component] = vote

    weights = _recombination_weights(by_channel)

    distribution: dict[ComponentType, float] = {}
    for channel, votes in by_channel.items():
        channel_distribution = _softmax_prune_renormalize(
            votes,
            classifier_config.softmax_temperature,
            classifier_config.min_component_prob,
        )
        channel_weight = weights[channel]
        for component, probability in channel_distribution.items():
            distribution[component] = channel_weight * probability
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

        # 6. Origin / third-party.
        if element.is_iframe:
            _add_votes(vote_totals, classifier_config.third_party.votes_iframe)
        if element.cross_origin:
            _add_votes(vote_totals, classifier_config.third_party.votes_cross_origin)
        if element.shadow_host:
            _add_votes(vote_totals, classifier_config.third_party.votes_shadow_host)
        if element.vendor_match:
            _add_votes(vote_totals, classifier_config.third_party.votes_vendor_match)

        # Suppressors, then softmax/prune/renormalize.
        _apply_suppressors(vote_totals, element, config)
        component_dist = _finalize_distribution(vote_totals, config)

        results.append(ClassifiedElement(element=element, component_dist=component_dist))

    return results
