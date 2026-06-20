"""Fusion: per-``(canonical color, role)`` evidence records for detection-plus-ranking.

This module is the **designated successor to** `colorsense.palette.inventory`
(redesign §5.3): it establishes the same canonical color identities the inventory does, but
instead of collapsing each cluster to a single summed ``component_mass`` it preserves the
**per-instance salience distribution** — the object the detection stage consumes.

To stay byte-faithful to the shipping color-identity logic it **reuses** inventory's helpers
rather than reimplementing them. Importing inventory's ``_``-prefixed names across modules is
deliberate and acceptable here precisely because fusion is that module's successor: the
near-white / near-black guards, the per-family join radii, the union-find grouping, and the
cluster-representative rule must behave identically, so both modules call the exact same code.
``build_inventory`` is untouched; this module only *adds* a new entry point.

The math is the salience model of `colorsense.palette.salience` (tuning-spec §1-§2): for each
element instance contributing color ``c`` to role ``r``,

    sigma_i(r) = p_role(i, r) * pi_i(r)

where ``p_role`` is the share of the instance's component distribution mapping to ``r`` and
``pi_i`` is its bounded instance prominence (area carrier, position/sibling/contrast modulators;
modulators disabled for surface roles). The per-``(color, role)`` saliences are then carried into
`colorsense.models.RoleEvidence` records — sorted descending, with the cluster's summed
screenshot area and the contributing evidence streams.
"""

from __future__ import annotations

from collections import defaultdict
from statistics import median
from typing import TYPE_CHECKING

from colorsense.color.match import nearest_within
from colorsense.color.primitives import contrast_ratio, is_painting
from colorsense.models import (
    EvidenceStream,
    PropertyFamily,
    RoleEvidence,
    UsageRole,
)
from colorsense.palette.inventory import (
    _EXACT_COLOR_FAMILIES,
    _MATCH_BY_FAMILY,
    CTA_ACTION_BG_COMPONENTS,
    _bg_fill_colors,
    _cluster_groups,
    _Entry,
    _group_representative,
    _nearest_mergeable_near_black_entry,
    _nearest_mergeable_near_white_entry,
)
from colorsense.palette.salience import area_fraction, instance_prominence, vertical_fraction
from colorsense.palette.usage import _AREA_RANKED_ROLES, USAGE_ROLE_BY_COMPONENT_TYPE

if TYPE_CHECKING:
    from colorsense.config import Config
    from colorsense.models import (
        ClassifiedElement,
        ComponentType,
        Harvest,
        HarvestedElement,
        Viewport,
    )

__all__ = ["build_evidence"]


def _family_contrast(element: HarvestedElement, family: PropertyFamily) -> float | None:
    """WCAG contrast of the element's painted color (per family) against its effective bg.

    The family selects which painted color drives the contrast modulator: the background fill
    for the background family, the text color for the text family, the border color for the
    border family. When either the painted color or ``effective_bg`` is missing (or paints
    nothing), contrast is unavailable and ``None`` is returned so the salience model applies a
    neutral ``m_con = 1.0``.

    Args:
        element: The harvested element whose contrast is measured.
        family: The [`PropertyFamily`][colorsense.PropertyFamily] selecting the painted color.

    Returns:
        The WCAG contrast ratio, or ``None`` when it cannot be computed.

    """
    if family is PropertyFamily.TEXT:
        painted = element.text
    elif family is PropertyFamily.BORDER:
        painted = element.border
    else:
        painted = element.bg
    background = element.effective_bg
    if not is_painting(painted) or not is_painting(background):
        return None
    assert painted is not None and background is not None  # narrowed by is_painting
    return contrast_ratio(painted, background)


def _role_share(
    distribution: dict[ComponentType, float],
) -> dict[UsageRole, float]:
    """Bucket a component-mass mapping into per-usage-role mass (``p_role`` numerator).

    Sums each component's mass into the usage role it maps to under
    `USAGE_ROLE_BY_COMPONENT_TYPE`; unrouted components (``cta_text``, ``third_party``) are
    dropped, matching the usage view.

    Args:
        distribution: A per-component mass mapping (a full or routed sub-distribution).

    Returns:
        Per-usage-role summed mass; roles with no contributing component are absent.

    """
    per_role: dict[UsageRole, float] = defaultdict(float)
    for component, mass in distribution.items():
        role = USAGE_ROLE_BY_COMPONENT_TYPE.get(component)
        if role is not None:
            per_role[role] += mass
    return per_role


def _median_sibling_area_by_role(
    classified: list[ClassifiedElement], viewport: Viewport
) -> dict[UsageRole, float]:
    """Median instance area fraction per role — the DOM-tree-free sibling-size proxy.

    The harvest is a *flat* element list with no parent/child links, so "the typical size of
    sibling interactive elements" (the redesign's ``m_sib`` denominator) is approximated by the
    median area fraction across **all** element instances whose component distribution places any
    mass in the role. This is an intentional approximation: it stands in for true DOM siblings
    with the population of same-role elements on the page.

    Args:
        classified: The classified DOM elements.
        viewport: The rendering viewport (anchors the area fractions).

    Returns:
        Per-role median area fraction; a role with no contributing element is absent (callers
        pass ``0.0`` for "no comparison possible").

    """
    areas_by_role: dict[UsageRole, list[float]] = defaultdict(list)
    for classification in classified:
        if not classification.component_distribution:
            continue
        a_i = area_fraction(classification.element.bounding_box, viewport)
        for role in _role_share(classification.component_distribution):
            areas_by_role[role].append(a_i)
    return {role: median(areas) for role, areas in areas_by_role.items()}


def build_evidence(
    harvest: Harvest,
    classified: list[ClassifiedElement],
    config: Config,
    viewport: Viewport,
) -> list[RoleEvidence]:
    """Fuse area-truth and element semantics into per-``(canonical color, role)`` evidence.

    The successor to `colorsense.palette.inventory.build_inventory` (redesign §5.3): it
    establishes the *same* canonical color identities — reusing inventory's helpers so the
    near-white / near-black guards, join radii, union-find grouping, and representative rule
    behave identically — but preserves the per-instance salience distribution
    (``sigma_i = p_role * pi_i``, tuning-spec §1-§2) instead of collapsing instances to a
    summed mass.

    The attribution loop mirrors ``build_inventory``'s structure (the exact-color,
    near-black-split, and plain-join branches, the gradient even-split, and the alpha/fill-count
    weight) and, for each route, additionally records the per-role salience onto the target
    entry's ``role_instances``. Each family pool is then grouped and turned into one
    `RoleEvidence` per ``(canonical color, role)`` pair.

    Args:
        harvest: The page `Harvest`; ``screenshot_bins`` seed the background pool's area truth.
        classified: The classified DOM elements supplying per-component semantic mass.
        config: The loaded configuration; ``config.detection`` carries the salience modulators.
        viewport: The rendering viewport, anchoring all area/position fractions.

    Returns:
        The flat list of `RoleEvidence` across all three families, sorted deterministically by
        ``(role.value, -peak, color.hex)``.

    """
    median_sibling_area = _median_sibling_area_by_role(classified, viewport)

    pools: dict[PropertyFamily, list[_Entry]] = {
        PropertyFamily.BACKGROUND: [
            _Entry(bin_.color, bin_.area_fraction) for bin_ in harvest.screenshot_bins
        ],
        PropertyFamily.TEXT: [],
        PropertyFamily.BORDER: [],
    }

    for classification in classified:
        if not classification.component_distribution:
            continue
        element = classification.element

        # Per-instance geometry (shared across families/roles): area carrier and position.
        a_i = area_fraction(element.bounding_box, viewport)
        y_frac = vertical_fraction(element.bounding_box, viewport)

        # Split the distribution into per-family sub-distributions via the shared routing
        # convention, exactly as build_inventory does.
        family_distributions: dict[PropertyFamily, dict[ComponentType, float]] = {
            PropertyFamily.BACKGROUND: {},
            PropertyFamily.TEXT: {},
            PropertyFamily.BORDER: {},
        }
        for component, mass in classification.component_distribution.items():
            family_distributions[component.property_family][component] = mass

        for family, colors in (
            (PropertyFamily.BACKGROUND, _bg_fill_colors(element)),
            (PropertyFamily.TEXT, [element.text]),
            (PropertyFamily.BORDER, [element.border]),
        ):
            family_distribution = family_distributions[family]
            if not family_distribution:
                continue
            fills = [color for color in colors if color is not None and is_painting(color)]
            if not fills:
                continue

            pool = pools[family]
            contrast = _family_contrast(element, family)

            is_exact_color_family = family in _EXACT_COLOR_FAMILIES
            vote_has_cta_action_mass = family is PropertyFamily.BACKGROUND and any(
                component in CTA_ACTION_BG_COMPONENTS for component in family_distribution
            )
            radius = _MATCH_BY_FAMILY[family]
            fill_count = len(fills)
            for color in fills:
                weight = (
                    color.alpha
                    if family in (PropertyFamily.BACKGROUND, PropertyFamily.BORDER)
                    else 1.0
                ) / fill_count

                # Resolve routing identically to build_inventory: same branches, same helper
                # calls in the same order, against the same pre-update pool.
                if is_exact_color_family:
                    routes = [
                        (
                            family_distribution,
                            _nearest_mergeable_near_white_entry(color, pool, radius),
                        )
                    ]
                elif vote_has_cta_action_mass:
                    cta_action_mass = {
                        c: m
                        for c, m in family_distribution.items()
                        if c in CTA_ACTION_BG_COMPONENTS
                    }
                    non_cta_mass = {
                        c: m
                        for c, m in family_distribution.items()
                        if c not in CTA_ACTION_BG_COMPONENTS
                    }
                    routes = [
                        (
                            cta_action_mass,
                            _nearest_mergeable_near_black_entry(color, pool, radius),
                        )
                    ]
                    if non_cta_mass:
                        routes.append(
                            (
                                non_cta_mass,
                                nearest_within(color, pool, radius, key=lambda e: e.color),
                            )
                        )
                else:
                    routes = [
                        (
                            family_distribution,
                            nearest_within(color, pool, radius, key=lambda e: e.color),
                        )
                    ]

                shared_new_entry: _Entry | None = None
                for route_mass, nearest_index in routes:
                    if nearest_index is None:
                        if shared_new_entry is None:
                            shared_new_entry = _Entry(color, 0.0)
                            pool.append(shared_new_entry)
                        target = shared_new_entry
                    else:
                        target = pool[nearest_index]

                    # Mirror build_inventory's vote-mass accumulation EXACTLY. This is not used
                    # for the evidence math, but the cluster-time near-black CTA/action guard
                    # (`_entry_has_cta_action_mass`) reads `vote_mass`, so the grouping is only
                    # identical to the inventory path if the same mass is written here.
                    for component, mass in route_mass.items():
                        target.vote_mass[component] += mass * weight

                    # Record the per-role salience for this route. per_role[r] is this route's
                    # share of the role; pi_i depends on r (surface flag + the family's contrast),
                    # so prominence is computed per role.
                    for role, role_mass in _role_share(route_mass).items():
                        surface = role in _AREA_RANKED_ROLES
                        pi_i = instance_prominence(
                            a_i,
                            y_frac=y_frac,
                            median_sibling_area=median_sibling_area.get(role, 0.0),
                            contrast=contrast,
                            surface=surface,
                            cfg=config.detection,
                        )
                        sigma = role_mass * pi_i * weight
                        target.role_instances[role].append(sigma)

                    # Accumulate the raw component mass that routed to each role (the public
                    # diagnostic carried onto RoleEvidence.components). Mirrors the vote-mass
                    # accumulation above but bucketed per role: each routed component's
                    # ``mass * weight`` is added under the role it maps to.
                    for component, mass in route_mass.items():
                        component_role = USAGE_ROLE_BY_COMPONENT_TYPE.get(component)
                        if component_role is not None:
                            target.role_components[component_role][component] += mass * weight

    records: list[RoleEvidence] = []
    for family in (PropertyFamily.BACKGROUND, PropertyFamily.TEXT, PropertyFamily.BORDER):
        for group in _cluster_groups(pools[family], family):
            representative = _group_representative(group, family)
            area = sum(entry.area_weight for entry in group)

            # Collect, per role, every member instance salience and the summed component mass.
            saliences_by_role: dict[UsageRole, list[float]] = defaultdict(list)
            components_by_role: dict[UsageRole, dict[ComponentType, float]] = defaultdict(
                lambda: defaultdict(float)
            )
            for entry in group:
                for role, saliences in entry.role_instances.items():
                    saliences_by_role[role].extend(saliences)
                for role, component_mass in entry.role_components.items():
                    for component, mass in component_mass.items():
                        components_by_role[role][component] += mass

            for role, saliences in saliences_by_role.items():
                # Drop exact zeros so a phantom instance never pads the distribution, then sort
                # descending as RoleEvidence requires (peak first).
                instance_saliences = tuple(sorted((s for s in saliences if s > 0.0), reverse=True))
                streams: list[EvidenceStream] = []
                if area > 0.0:
                    streams.append(EvidenceStream.SCREENSHOT)
                if instance_saliences:
                    streams.append(EvidenceStream.DOM)
                # A role whose every instance salience vanished and which carries no area is pure
                # noise — never emit an empty record for it.
                if not streams:
                    continue
                records.append(
                    RoleEvidence(
                        color=representative.color,
                        role=role,
                        instance_saliences=instance_saliences,
                        area=area,
                        components={
                            component: mass
                            for component, mass in components_by_role.get(role, {}).items()
                            if mass > 0.0
                        },
                        streams=tuple(streams),
                    )
                )

    records.sort(key=lambda r: (r.role.value, -r.peak, r.color.hex))
    return records
