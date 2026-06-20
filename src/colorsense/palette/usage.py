"""Role/component taxonomy: the shared mapping from component types to usage roles.

This module defines the authoritative usage-role → component-type collapse used across
the pipeline. It is a fixed code-level convention — not a tunable weight — exactly like
the inventory's component → property-family routing (``ComponentType.property_family``):
it describes what the taxonomy *means*.

``cta_text`` and ``third_party`` are deliberately absent from every role:
``cta_text`` is the button-label sink (a button-styled element's text color is part of
the CTA, not an independent palette role, so it carries no usage); ``third_party`` flows
to ``AnalysisResult.third_party_colors`` instead. The inverse
(`USAGE_ROLE_BY_COMPONENT_TYPE`) is built once and asserted to partition every routed
component to exactly one role.

The role-keyed and color-keyed **views** of the palette (previously ``build_usage`` and
``build_color_index``) now live in `palette/detect.py`, produced directly from the
per-``(color, role)`` evidence records that `palette/fusion.py` accumulates.

The split into surface-ranked vs element-ranked scoring signal is captured in
`_AREA_RANKED_ROLES`: page/surface/banner are scored by screenshot area; every other role
is scored by peak-instance salience (see `detect.py`).
"""

from __future__ import annotations

from colorsense.models import (
    ComponentType,
    UsageRole,
)

__all__ = ["COMPONENT_TYPES_BY_USAGE_ROLE"]

CT = ComponentType

# Usage-role -> component-type collapse. A fixed code-level convention (see the module
# docstring). ``cta_text`` and ``third_party`` map to NO role and are excluded from both
# usage views; third-party widget colors surface via ``AnalysisResult.third_party_colors``.
COMPONENT_TYPES_BY_USAGE_ROLE: dict[UsageRole, tuple[ComponentType, ...]] = {
    UsageRole.PAGE: (CT.PAGE_BG,),
    UsageRole.SURFACE: (CT.CARD_BG, CT.MODAL_BG, CT.HERO_BG, CT.INPUT_BG),
    UsageRole.BANNER: (CT.HEADER_BG, CT.NAV_BG, CT.FOOTER_BG),
    UsageRole.CTA: (CT.CTA_BG,),
    UsageRole.ACTION: (CT.BUTTON_SECONDARY, CT.BADGE),
    UsageRole.TEXT: (
        CT.PAGE_TEXT,
        CT.HEADER_TEXT,
        CT.NAV_TEXT,
        CT.FOOTER_TEXT,
        CT.HERO_TEXT,
        CT.CARD_TEXT,
    ),
    UsageRole.LINK: (CT.LINK,),
    UsageRole.BORDER: (CT.BORDER,),
}


def _build_usage_role_by_component_type() -> dict[ComponentType, UsageRole]:
    """Invert `COMPONENT_TYPES_BY_USAGE_ROLE`, asserting it partitions every routed component once.

    A component appearing under two roles (or `COMPONENT_TYPES_BY_USAGE_ROLE` drifting from the
    taxonomy) would be a silent routing bug; the assertion turns it into a load-time
    failure. ``cta_text`` and ``third_party`` are intentionally unrouted.

    Returns:
        The component-type → usage-role mapping (the inverse of
        `COMPONENT_TYPES_BY_USAGE_ROLE`).

    Raises:
        AssertionError: If any component type is routed to more than one usage role.

    """
    inverse: dict[ComponentType, UsageRole] = {}
    for role, component_types in COMPONENT_TYPES_BY_USAGE_ROLE.items():
        for component_type in component_types:
            assert component_type not in inverse, (
                f"{component_type} routed to both {inverse[component_type]} and {role}"
            )
            inverse[component_type] = role
    return inverse


# Component-type -> usage-role routing (the inverse of `COMPONENT_TYPES_BY_USAGE_ROLE`), built and
# partition-checked once at import.
USAGE_ROLE_BY_COMPONENT_TYPE: dict[ComponentType, UsageRole] = _build_usage_role_by_component_type()

# Roles whose role-keyed prominence is the cluster's screenshot ``area_weight`` rather than
# its peak-instance salience. These name the structural *surfaces* of a layout — the page canvas,
# raised surfaces (cards/modals/hero/inputs), and header/nav/footer bands — where the right
# question is "how much screen does this color cover".
#
# Every OTHER role names an *element* color (cta/action button fills, text, links, borders),
# ranked by peak-instance salience (see `detect.py`). These paint negligible screenshot area,
# so area is the wrong signal.
_AREA_RANKED_ROLES: frozenset[UsageRole] = frozenset(
    {UsageRole.PAGE, UsageRole.SURFACE, UsageRole.BANNER}
)
