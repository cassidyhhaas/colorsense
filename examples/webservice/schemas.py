"""Request/response models for the ``/analyze`` endpoint, and the response shaping."""

from __future__ import annotations

from pydantic import BaseModel, Field

from colorsense import AnalysisResult


class AnalyzeRequest(BaseModel):
    """POST body: the page to analyze.

    Validation happens in the endpoint, not the model, so rejections produce a 400
    with a reason rather than a generic 422.
    """

    # Bounding untrusted input: cap the URL length (2083 is well above any legitimate
    # URL) so a multi-megabyte string never reaches urlsplit/resolution.
    url: str = Field(max_length=2083)


class EntryOut(BaseModel):
    """One role-keyed usage entry, trimmed to what API consumers paint with."""

    hex: str
    probability: float
    area: float


class UsageOut(BaseModel):
    """One usage slot of a color in the color-keyed index.

    Carries the role, its property family, and this color's share of its own usages.
    """

    role: str
    property_family: str
    weight: float


class ColorOut(BaseModel):
    """One color in the canonical color-keyed index.

    Carries its hex, overall prominence, and the usage roles it appears in (most-used first).
    """

    hex: str
    prominence: float
    area: float
    usages: list[UsageOut]


class ThemeOut(BaseModel):
    """One analyzed theme, mirroring the library payload's two views.

    ``colors`` is the canonical color-keyed index ("how each color is used"); ``usage``
    maps usage role (page/surface/banner/cta/action/text/link/border) -> ranked entries
    ("which colors paint each role").
    """

    colors: list[ColorOut]
    usage: dict[str, list[EntryOut]]


class AnalyzeResponse(BaseModel):
    """Trimmed response: per-theme color index and role-keyed usage view."""

    url: str
    themes: dict[str, ThemeOut]


def shape_response(result: AnalysisResult) -> AnalyzeResponse:
    """Trim the typed result to the response shape.

    The full result carries divergence, fine component evidence, and OKLCH coordinates —
    valuable to library consumers, noise to a palette API. Keep hex/probability/area per
    entry for the role-keyed usage view, plus the color-keyed index trimmed to
    hex/prominence/area and its usage-role slots.

    Args:
        result: The full analysis result to trim.

    Returns:
        The trimmed per-theme response payload.
    """
    themes = {
        theme.value: ThemeOut(
            colors=[
                ColorOut(
                    hex=cu.color.hex,
                    prominence=cu.prominence,
                    area=cu.area,
                    usages=[
                        UsageOut(
                            role=u.role.value,
                            property_family=u.property_family.value,
                            weight=u.weight,
                        )
                        for u in cu.usages
                    ],
                )
                for cu in palette.colors
            ],
            usage={
                role.value: [
                    EntryOut(hex=e.color.hex, probability=e.probability, area=e.area)
                    for e in entries
                ]
                for role, entries in palette.usage.mapping.items()
            },
        )
        for theme, palette in result.themes.items()
    }
    return AnalyzeResponse(url=result.url, themes=themes)
