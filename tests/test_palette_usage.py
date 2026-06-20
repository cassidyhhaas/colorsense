"""Unit tests for the role/component taxonomy in palette/usage.py."""

from __future__ import annotations

from colorsense.models import (
    ComponentType,
    UsageRole,
)
from colorsense.palette.usage import (
    _AREA_RANKED_ROLES,
    COMPONENT_TYPES_BY_USAGE_ROLE,
    USAGE_ROLE_BY_COMPONENT_TYPE,
)

# ---------------------------------------------------------------------------
# COMPONENT_TYPES_BY_USAGE_ROLE / partition
# ---------------------------------------------------------------------------


def test_role_components_partitions_every_routed_component_once() -> None:
    # USAGE_ROLE_BY_COMPONENT_TYPE is the exact inverse of COMPONENT_TYPES_BY_USAGE_ROLE:
    # one role per routed component.
    flat = [c for comps in COMPONENT_TYPES_BY_USAGE_ROLE.values() for c in comps]
    assert len(flat) == len(set(flat))  # no component routed twice
    assert set(USAGE_ROLE_BY_COMPONENT_TYPE) == set(flat)
    for role, comps in COMPONENT_TYPES_BY_USAGE_ROLE.items():
        for comp in comps:
            assert USAGE_ROLE_BY_COMPONENT_TYPE[comp] is role


def test_cta_text_and_third_party_are_unrouted() -> None:
    # Both are deliberately absent from every role and from the inverse map.
    assert ComponentType.CTA_TEXT not in USAGE_ROLE_BY_COMPONENT_TYPE
    assert ComponentType.THIRD_PARTY not in USAGE_ROLE_BY_COMPONENT_TYPE
    # Everything else IS routed.
    assert set(USAGE_ROLE_BY_COMPONENT_TYPE) == set(ComponentType) - {
        ComponentType.CTA_TEXT,
        ComponentType.THIRD_PARTY,
    }


def test_area_ranked_roles_are_the_three_surface_roles() -> None:
    # The area-ranked roles are exactly the three structural-surface roles.
    assert {UsageRole.PAGE, UsageRole.SURFACE, UsageRole.BANNER} == _AREA_RANKED_ROLES


def test_cta_action_are_not_area_ranked() -> None:
    # Guards against silent drift: element-color roles must never enter the area-ranked set.
    assert UsageRole.CTA not in _AREA_RANKED_ROLES
    assert UsageRole.ACTION not in _AREA_RANKED_ROLES
    assert UsageRole.TEXT not in _AREA_RANKED_ROLES
    assert UsageRole.LINK not in _AREA_RANKED_ROLES
    assert UsageRole.BORDER not in _AREA_RANKED_ROLES


def test_inverse_map_round_trips() -> None:
    # For every (role, component) pair in the forward map, the inverse map resolves back
    # to the same role — the two dicts are consistent inverses of each other.
    for role, components in COMPONENT_TYPES_BY_USAGE_ROLE.items():
        for comp in components:
            assert USAGE_ROLE_BY_COMPONENT_TYPE[comp] is role
