"""Unit tests for reconciliation (log-linear pooling of intent + measured usage)."""

from __future__ import annotations

import math

from colorsense.color.primitives import parse_css_color
from colorsense.models import (
    ClassifiedToken,
    Color,
    ComponentType,
    TokenOrigin,
    TokenRecord,
    TokenSemanticRole,
    UsageEntry,
    UsagePalette,
    UsageRole,
)
from colorsense.palette.reconcile import reconcile


def _color(css: str) -> Color:
    c = parse_css_color(css)
    assert c is not None, css
    return c


def _entry(
    css: str,
    prob: float,
    area: float = 0.1,
    components: dict[ComponentType, float] | None = None,
) -> UsageEntry:
    return UsageEntry(color=_color(css), probability=prob, area=area, components=components or {})


def _token(
    name: str,
    css: str,
    usage_intent: dict[UsageRole, float],
    weight: float = 1.0,
    semantic_role: TokenSemanticRole = TokenSemanticRole.brand_accent,
    origin: TokenOrigin = TokenOrigin.name_rule,
) -> ClassifiedToken:
    return ClassifiedToken(
        record=TokenRecord(
            name=name,
            raw_value=css,
            resolved=_color(css),
            scope=":root",
        ),
        semantic_role=semantic_role,
        weight=weight,
        usage_intent=usage_intent,
        origin=origin,
    )


def _prob_for(results: UsagePalette, category: UsageRole, css: str) -> float:
    target = _color(css)
    for entry in results.mapping.get(category, ()):
        if entry.color.hex == target.hex:
            return entry.probability
    raise AssertionError(f"{css} not present in category {category}")


def test_intent_boost_breaks_tie_toward_declared() -> None:
    a = "#2563eb"
    b = "#e11d48"
    usage = UsagePalette(
        mapping={
            UsageRole.cta: (
                _entry(a, 0.5),
                _entry(b, 0.5),
            )
        }
    )
    # Both colors are declared for interactive, but A carries far more intent mass; this
    # should tip the 0.5/0.5 usage tie toward A while keeping B present.
    tokens = [
        _token("--accent", a, {UsageRole.cta: 0.85, UsageRole.surface: 0.15}),
        _token("--accent-2", b, {UsageRole.cta: 0.15, UsageRole.surface: 0.85}),
    ]
    posterior, _ = reconcile(usage, tokens, alpha=0.4)

    p_a = _prob_for(posterior, UsageRole.cta, a)
    p_b = _prob_for(posterior, UsageRole.cta, b)
    assert p_a > p_b
    assert p_a > 0.5


def test_declared_but_unused_appears_in_divergence() -> None:
    usage = UsagePalette(
        mapping={
            UsageRole.cta: (_entry("#2563eb", 1.0),),
        }
    )
    unused = "#10b981"
    tokens = [
        _token("--primary", unused, {UsageRole.surface: 1.0}, origin=TokenOrigin.name_rule),
    ]
    _, divergence = reconcile(usage, tokens, alpha=0.4)

    target = _color(unused)
    hits = [d for d in divergence if d.color.hex == target.hex and "unused" in d.note]
    assert hits, divergence
    assert hits[0].role == UsageRole.surface


def _relational_token(name: str, css: str, weight: float = 1.0) -> ClassifiedToken:
    # The shape classify_tokens actually produces for --on-*/-foreground tokens: role
    # text_on, EMPTY usage intent (the config row is a channel route, not a distribution).
    return _token(
        name,
        css,
        usage_intent={},
        weight=weight,
        semantic_role=TokenSemanticRole.text_on,
        origin=TokenOrigin.relational,
    )


def test_declared_but_unused_gated_to_high_intent_origins() -> None:
    # The divergence-noise fix: a scale-origin token (e.g. an unused --green-300 shade)
    # must NOT raise declared-but-unused — on token-heavy sites every unused shade of
    # every scale used to fire. A name_rule-origin token with the same color MUST, and
    # so must a relational token (which classifies with an EMPTY usage intent, the shape
    # classify_tokens really emits — it reports through the dedicated relational pass).
    usage = UsagePalette(mapping={UsageRole.cta: (_entry("#2563eb", 1.0),)})
    unused = "#10b981"

    for low_intent in (TokenOrigin.scale, TokenOrigin.alias, TokenOrigin.fallback):
        tokens = [_token("--green-300", unused, {UsageRole.cta: 1.0}, origin=low_intent)]
        _, divergence = reconcile(usage, tokens, alpha=0.4)
        assert not any("unused" in d.note for d in divergence), low_intent

    tokens = [_token("--brand", unused, {UsageRole.cta: 1.0})]
    _, divergence = reconcile(usage, tokens, alpha=0.4)
    assert any("unused" in d.note for d in divergence)

    tokens = [_relational_token("--on-primary", unused)]
    _, divergence = reconcile(usage, tokens, alpha=0.4)
    hits = [d for d in divergence if "unused" in d.note]
    assert hits and hits[0].role == UsageRole.text
    assert hits[0].note == "declared '--on-primary' unused in render"


def test_rendered_relational_token_color_is_not_undeclared() -> None:
    # The release-review false positive: a page whose dominant text color exactly
    # matches its declared --on-primary was reported "used but undeclared" because
    # empty-usage-intent tokens (relational, excluded status) were invisible to the
    # membership test. Undeclaredness is about the stylesheet, not intent mass.
    white = "#ffffff"
    usage = UsagePalette(
        mapping={UsageRole.text: (_entry(white, 1.0, components={ComponentType.page_text: 1.0}),)}
    )
    tokens = [_relational_token("--on-primary", white)]
    _, divergence = reconcile(usage, tokens, alpha=0.4)

    assert not any(d.note == "used but undeclared" for d in divergence)
    # And the rendered relational token is not "unused" either.
    assert not any("unused" in d.note for d in divergence)


def test_rendered_status_token_color_is_not_undeclared() -> None:
    # Status tokens get an empty usage intent when status_excluded_from_palette is set; their
    # declared color must still count for the used-but-undeclared membership test.
    red = "#dc2626"
    usage = UsagePalette(mapping={UsageRole.cta: (_entry(red, 1.0),)})
    tokens = [
        _token("--danger", red, usage_intent={}, semantic_role=TokenSemanticRole.status),
    ]
    _, divergence = reconcile(usage, tokens, alpha=0.4)

    assert not any(d.note == "used but undeclared" for d in divergence)


def test_near_identical_relational_tokens_report_once() -> None:
    # Two unused foreground tokens within MAX_TOKEN_MERGE_DELTA_E fold into one relational group:
    # one divergence item, representative_name from the heavier token.
    usage = UsagePalette(mapping={UsageRole.surface: (_entry("#111111", 1.0),)})
    tokens = [
        _relational_token("--on-primary", "#fefefe", weight=1.0),
        _relational_token("--card-foreground", "#ffffff", weight=3.0),
    ]
    _, divergence = reconcile(usage, tokens, alpha=0.4)

    unused = [d for d in divergence if "unused" in d.note]
    assert len(unused) == 1
    assert unused[0].note == "declared '--card-foreground' unused in render"
    assert unused[0].role == UsageRole.text


def test_alpha_zero_is_pure_usage() -> None:
    usage = UsagePalette(
        mapping={
            UsageRole.cta: (
                _entry("#2563eb", 0.7),
                _entry("#e11d48", 0.3),
            )
        }
    )
    # Strong intent for a token-only color that should be ignored at alpha=0.
    tokens = [
        _token("--accent", "#10b981", {UsageRole.cta: 1.0}, weight=5.0),
    ]
    posterior, _ = reconcile(usage, tokens, alpha=0.0)

    entries = posterior.mapping[UsageRole.cta]
    hexes = {e.color.hex for e in entries}
    assert hexes == {_color("#2563eb").hex, _color("#e11d48").hex}

    p_blue = _prob_for(posterior, UsageRole.cta, "#2563eb")
    p_rose = _prob_for(posterior, UsageRole.cta, "#e11d48")
    # Ratio preserved: 0.7 / 0.3.
    assert math.isclose(p_blue / p_rose, 0.7 / 0.3, rel_tol=1e-4)


def test_alpha_one_is_pure_intent() -> None:
    usage = UsagePalette(
        mapping={
            UsageRole.cta: (
                _entry("#2563eb", 0.9),
                _entry("#e11d48", 0.1),
            )
        }
    )
    # Token favors the rose color strongly for interactive.
    tokens = [
        _token("--accent", "#e11d48", {UsageRole.cta: 1.0}, weight=3.0),
    ]
    posterior, _ = reconcile(usage, tokens, alpha=1.0)

    entries = posterior.mapping[UsageRole.cta]
    argmax = max(entries, key=lambda e: e.probability)
    assert argmax.color.hex == _color("#e11d48").hex


def test_every_category_distribution_normalized() -> None:
    usage = UsagePalette(
        mapping={
            UsageRole.surface: (
                _entry("#2563eb", 0.6),
                _entry("#1d4ed8", 0.4),
            ),
            UsageRole.cta: (
                _entry("#e11d48", 0.5),
                _entry("#10b981", 0.5),
            ),
        }
    )
    tokens = [
        _token("--brand", "#2563eb", {UsageRole.surface: 1.0}),
        _token("--pop", "#e11d48", {UsageRole.cta: 1.0}),
    ]
    posterior, _ = reconcile(usage, tokens, alpha=0.4)

    # Every UsageRole is always present (categories with no entries map to ()); a
    # non-empty category's entry probabilities form a normalized distribution.
    assert set(posterior.mapping) == set(UsageRole)
    for category, entries in posterior.mapping.items():
        if not entries:
            continue
        total = sum(e.probability for e in entries)
        assert math.isclose(total, 1.0, abs_tol=1e-6), (category, total)


def test_used_but_undeclared_appears_in_divergence() -> None:
    usage = UsagePalette(
        mapping={
            UsageRole.cta: (
                _entry("#2563eb", 0.8),
                _entry("#e11d48", 0.2),
            )
        }
    )
    # No tokens declared at all -> prominent usage color is undeclared.
    tokens: list[ClassifiedToken] = []
    _, divergence = reconcile(usage, tokens, alpha=0.4)

    target = _color("#2563eb")
    hits = [d for d in divergence if d.color.hex == target.hex and d.note == "used but undeclared"]
    assert hits, divergence
    assert hits[0].role == UsageRole.cta


def _entry_lists(results: UsagePalette) -> dict[UsageRole, list[tuple[str, float]]]:
    """The full per-category (hex, probability) lists, for whole-posterior equality."""
    return {
        category: [(e.color.hex, e.probability) for e in entries]
        for category, entries in results.mapping.items()
    }


def test_alpha_out_of_range_is_clamped() -> None:
    # A setup where alpha genuinely matters: usage and intent disagree about the
    # interactive color, so the alpha=0 and alpha=1 posteriors differ — proving the
    # comparisons below are not vacuous. Out-of-range alphas must clamp to the boundary
    # posteriors exactly.
    usage = UsagePalette(
        mapping={
            UsageRole.cta: (
                _entry("#2563eb", 0.7),
                _entry("#e11d48", 0.3),
            )
        }
    )
    tokens = [_token("--accent", "#e11d48", {UsageRole.cta: 1.0})]

    at_zero = _entry_lists(reconcile(usage, tokens, alpha=0.0)[0])
    at_one = _entry_lists(reconcile(usage, tokens, alpha=1.0)[0])
    assert at_zero != at_one  # alpha is load-bearing in this setup

    assert _entry_lists(reconcile(usage, tokens, alpha=-0.5)[0]) == at_zero
    assert _entry_lists(reconcile(usage, tokens, alpha=5.0)[0]) == at_one


def test_near_colors_join_across_usage_and_tokens() -> None:
    # The ΔE nearest-color join: #fa0202 is within MAX_MEASURED_MATCH_DELTA_E of the used
    # #ff0000 (measured entry vs declared token — the loose radius applies), so the
    # token must pool INTO the usage entry (both signals on one color) instead of
    # surfacing as a separate token-only entry.
    used_red = "#ff0000"
    declared_red = "#fa0202"
    usage = UsagePalette(
        mapping={
            UsageRole.cta: (
                _entry(used_red, 0.7, area=0.05, components={ComponentType.link: 1.0}),
                _entry("#0000ff", 0.3),
            )
        }
    )
    tokens = [_token("--brand-red", declared_red, {UsageRole.cta: 1.0})]
    posterior, divergence = reconcile(usage, tokens, alpha=0.4)

    entries = posterior.mapping[UsageRole.cta]
    hexes = {e.color.hex for e in entries}
    # No separate token-only entry: the declared color merged into the usage color.
    assert _color(declared_red).hex not in hexes
    assert _color(used_red).hex in hexes

    joined = next(e for e in entries if e.color.hex == _color(used_red).hex)
    # Intent backing lifts the red above its pure-usage 0.7, and the measured entry's
    # area/components ride along on the posterior entry.
    assert joined.probability > 0.7
    assert joined.area == 0.05
    assert joined.components == {ComponentType.link: 1.0}

    # And the joined color is neither "declared but unused" nor "used but undeclared".
    assert not any(d.color.hex == _color(declared_red).hex for d in divergence)
    assert not any(d.color.hex == _color(used_red).hex for d in divergence)


def test_token_only_color_never_enters_posterior() -> None:
    # The posterior universe is the measured entries only: a declared color with no
    # measured match never appears as a posterior entry — even when the category's
    # measured evidence is weak — so every posterior entry structurally carries measured
    # area/components. The declared intent surfaces through divergence instead.
    usage = UsagePalette(
        mapping={
            UsageRole.cta: (
                _entry("#2563eb", 1.0, area=0.05, components={ComponentType.link: 1.0}),
            )
        }
    )
    token_only = "#10b981"
    tokens = [_token("--green", token_only, {UsageRole.cta: 1.0}, weight=5.0)]
    posterior, divergence = reconcile(usage, tokens, alpha=0.4)

    entries = posterior.mapping[UsageRole.cta]
    assert {e.color.hex for e in entries} == {_color("#2563eb").hex}
    assert all(e.components for e in entries)
    assert any(d.color.hex == _color(token_only).hex and "unused" in d.note for d in divergence)


def test_near_identical_tokens_aggregate_into_one_intent_group() -> None:
    # #2563eb and #2a66ec are within MAX_TOKEN_MERGE_DELTA_E: _aggregate_intent must fold them into
    # ONE intent group (one joined entry, one divergence entry), with representative_name taken
    # from the heavier-weighted token.
    usage = UsagePalette(mapping={UsageRole.cta: (_entry("#10b981", 1.0),)})
    tokens = [
        _token("--a-light-blue", "#2563eb", {UsageRole.surface: 1.0}, weight=1.0),
        _token("--b-heavy-blue", "#2a66ec", {UsageRole.surface: 1.0}, weight=3.0),
    ]
    posterior, divergence = reconcile(usage, tokens, alpha=0.4)

    # Surface has no measured usage: the empty-category gate keeps it empty (no
    # token-only injection); the aggregated group surfaces via divergence instead.
    assert posterior.mapping[UsageRole.surface] == ()

    # One group -> exactly one declared-but-unused entry; representative_name is the heavier token.
    unused = [d for d in divergence if "unused" in d.note]
    assert len(unused) == 1
    assert unused[0].note == "declared '--b-heavy-blue' unused in render"
    assert unused[0].role == UsageRole.surface


def test_colors_outside_delta_e_threshold_stay_separate() -> None:
    # #2563eb vs #10b981 are far outside MAX_TOKEN_MERGE_DELTA_E: two intent groups, two separate
    # divergence entries. The unmeasured surface category itself stays empty (the
    # empty-category gate) rather than carrying token-only entries.
    usage = UsagePalette(mapping={UsageRole.cta: (_entry("#e11d48", 1.0),)})
    tokens = [
        _token("--blue", "#2563eb", {UsageRole.surface: 1.0}),
        _token("--green", "#10b981", {UsageRole.surface: 1.0}),
    ]
    posterior, divergence = reconcile(usage, tokens, alpha=0.4)

    assert posterior.mapping[UsageRole.surface] == ()
    unused_hexes = {d.color.hex for d in divergence if "unused" in d.note}
    assert unused_hexes == {_color("#2563eb").hex, _color("#10b981").hex}


def test_weak_entries_pruned_and_survivors_renormalized() -> None:
    # At alpha=0 the posterior equals the usage distribution, so the 0.01 entry falls
    # below MIN_POSTERIOR_PROB (0.02) and is pruned; the two survivors renormalize from
    # 0.495 each to 0.5 each (summing to ~1.0).
    weak = "#aaaaaa"
    usage = UsagePalette(
        mapping={
            UsageRole.cta: (
                _entry("#2563eb", 0.495),
                _entry("#e11d48", 0.495),
                _entry(weak, 0.01),
            )
        }
    )
    posterior, _ = reconcile(usage, [], alpha=0.0)

    entries = posterior.mapping[UsageRole.cta]
    hexes = {e.color.hex for e in entries}
    assert _color(weak).hex not in hexes  # pruned
    assert hexes == {_color("#2563eb").hex, _color("#e11d48").hex}
    assert math.isclose(sum(e.probability for e in entries), 1.0, abs_tol=1e-9)
    for entry in entries:
        assert math.isclose(entry.probability, 0.5, abs_tol=1e-9)


def test_pruning_that_empties_category_keeps_argmax_at_one() -> None:
    # 60 entries, all below MIN_POSTERIOR_PROB: naive pruning would empty the category,
    # so the single argmax entry must be kept at probability 1.0 instead.
    n = 60
    strongest = "#00ff00"
    weak_share = (1.0 - 0.019) / (n - 1)  # every entry < MIN_POSTERIOR_PROB (0.02)
    entries_in = [_entry(strongest, 0.019)] + [
        _entry(f"#0000{i:02x}", weak_share) for i in range(n - 1)
    ]
    assert all(e.probability < 0.02 for e in entries_in)
    usage = UsagePalette(mapping={UsageRole.cta: tuple(entries_in)})
    posterior, _ = reconcile(usage, [], alpha=0.0)

    entries = posterior.mapping[UsageRole.cta]
    assert len(entries) == 1
    assert entries[0].color.hex == _color(strongest).hex
    assert entries[0].probability == 1.0


def test_argmax_fallback_tie_is_broken_by_smallest_hex() -> None:
    # Regression for the release-review tie-break unification: when pruning empties the
    # category AND the posterior has exactly-tied maxima, the smallest hex must win (the
    # shared prune_distribution convention) — not the entry's position in the input
    # (the old bare-max() behavior). The larger hex is deliberately listed FIRST.
    n = 60
    tied_prob = 0.019  # below MIN_POSTERIOR_PROB (0.02)
    weak_share = (1.0 - 2 * tied_prob) / (n - 2)
    assert weak_share < tied_prob
    entries_in = [_entry("#cccccc", tied_prob), _entry("#aaaaaa", tied_prob)] + [
        _entry(f"#0000{i:02x}", weak_share) for i in range(n - 2)
    ]
    usage = UsagePalette(mapping={UsageRole.cta: tuple(entries_in)})
    posterior, _ = reconcile(usage, [], alpha=0.0)

    entries = posterior.mapping[UsageRole.cta]
    assert len(entries) == 1
    assert entries[0].color.hex == _color("#aaaaaa").hex
    assert entries[0].probability == 1.0


def test_dominant_undeclared_color_stays_dominant() -> None:
    # The 0.4.0 release-review regression: with an EPS-floored intent factor, a single
    # declared minor color annihilated a 95%-dominant undeclared one (a 95%-white page
    # whose surface palette contained no white). With 1/K uniform smoothing the missing
    # intent signal is a bounded penalty: white must remain present AND dominant.
    usage = UsagePalette(
        mapping={
            UsageRole.surface: (
                _entry("#ffffff", 0.95, components={ComponentType.page_bg: 1.0}),
                _entry("#2563eb", 0.05, components={ComponentType.hero_bg: 1.0}),
            )
        }
    )
    tokens = [_token("--brand", "#2563eb", {UsageRole.surface: 0.3})]
    posterior, _ = reconcile(usage, tokens, alpha=0.4)

    p_white = _prob_for(posterior, UsageRole.surface, "#ffffff")
    p_blue = _prob_for(posterior, UsageRole.surface, "#2563eb")
    assert p_white > p_blue
    assert p_white > 0.5


def test_empty_category_yields_empty_posterior_not_token_flood() -> None:
    # THE empty-category gate: a category with ZERO measured usage candidates must come
    # back empty — token-only intent is NOT injected. (The live-probe regression: with
    # no measured borders, 16 token-only colors all got the same eps usage factor,
    # survived pruning near-uniformly, and flooded usage.border with empty-components
    # noise.) The declared intent still surfaces through divergence.
    usage = UsagePalette(mapping={UsageRole.surface: (_entry("#ffffff", 1.0),)})
    tokens = [
        _token(f"--border-{i}", hexv, {UsageRole.border: 1.0})
        for i, hexv in enumerate(["#ff8182", "#a830e8", "#7ae9ff", "#c7e580"])
    ]
    posterior, divergence = reconcile(usage, tokens, alpha=0.4)

    assert posterior.mapping[UsageRole.border] == ()
    # Honest emptiness, but not silence: every declared border color raises divergence.
    unused = {d.color.hex for d in divergence if "unused" in d.note}
    assert unused == {_color(h).hex for h in ("#ff8182", "#a830e8", "#7ae9ff", "#c7e580")}


def test_unmatched_token_excluded_when_category_is_measured() -> None:
    # The flip side of the gate: a measured category's posterior holds exactly the
    # measured entries — an unmatched declared color is structurally excluded (not
    # merely crushed by pooling), and every surviving entry keeps non-empty components.
    usage = UsagePalette(
        mapping={
            UsageRole.border: (
                _entry("#d1d9e0", 0.9, components={ComponentType.border: 1.0}),
                _entry("#59636e", 0.1, components={ComponentType.border: 1.0}),
            )
        }
    )
    tokens = [_token("--border-exotic", "#ff8182", {UsageRole.border: 1.0})]
    posterior, _ = reconcile(usage, tokens, alpha=0.4)

    entries = posterior.mapping[UsageRole.border]
    hexes = {e.color.hex for e in entries}
    assert _color("#ff8182").hex not in hexes  # structurally excluded
    assert hexes == {_color("#d1d9e0").hex, _color("#59636e").hex}
    assert all(e.components for e in entries)


def test_used_but_undeclared_threshold_boundary() -> None:
    # UNDECLARED_MIN_PROB = 0.15 gates the used-but-undeclared report: a 0.14 entry
    # stays silent while a 0.16 entry is reported.
    below = "#2563eb"
    above = "#e11d48"
    usage = UsagePalette(
        mapping={
            UsageRole.cta: (
                _entry(above, 0.16),
                _entry(below, 0.14),
            )
        }
    )
    _, divergence = reconcile(usage, [], alpha=0.4)

    undeclared_hexes = {d.color.hex for d in divergence if d.note == "used but undeclared"}
    assert undeclared_hexes == {_color(above).hex}


def test_subthreshold_rendered_declared_color_is_not_reported_unused() -> None:
    """'declared ... unused in render' is tested against the PRE-prune inventory.

    A declared color that genuinely rendered — just below every category's prune
    threshold — must not be misreported as unused (release-review fix: usage entries are
    post-prune, so they alone cannot answer "did this render?").
    """
    declared = "#e11d48"
    usage = UsagePalette(mapping={UsageRole.surface: (_entry("#ffffff", 1.0),)})
    tokens = [_token("--brand", declared, {UsageRole.surface: 1.0})]

    # Fallback path (no inventory): the pruned-away color looks unused.
    _, fallback_div = reconcile(usage, tokens)
    assert any("unused in render" in item.note for item in fallback_div)

    # With the full measured inventory containing the rendered color: no unused report.
    _, divergence = reconcile(usage, tokens, measured_colors=[_color("#ffffff"), _color(declared)])
    assert not any("unused in render" in item.note for item in divergence)
