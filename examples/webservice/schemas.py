"""Request/response models for the ``/analyze`` endpoint, and the response shaping."""

from __future__ import annotations

from pydantic import BaseModel, Field

from colorsense import AnalysisResult


class AnalyzeRequest(BaseModel):
    """POST body: the page to analyze. Validation happens in the endpoint, not the model,
    so rejections produce a 400 with a reason rather than a generic 422."""

    # Bounding untrusted input: cap the URL length (2083 is well above any legitimate
    # URL) so a multi-megabyte string never reaches urlsplit/resolution.
    url: str = Field(max_length=2083)


class EntryOut(BaseModel):
    """One usage entry (or role candidate), trimmed to what API consumers paint with."""

    hex: str
    probability: float
    area: float


class ThemeOut(BaseModel):
    """One analyzed theme: the usage view first, then the derived 60/30/10 roles view.

    ``usage`` maps usage category (surface/text/interactive/border) -> ranked entries;
    ``roles`` maps role name -> ranked candidates (best first; empty list when nothing
    was detected). ``fit_score`` describes how 60/30/10-like the design is.
    """

    usage: dict[str, list[EntryOut]]
    roles: dict[str, list[EntryOut]]
    fit_score: float


class AnalyzeResponse(BaseModel):
    """Trimmed response: per-theme usage view plus the derived roles view."""

    url: str
    themes: dict[str, ThemeOut]


def shape_response(result: AnalysisResult) -> AnalyzeResponse:
    """Trim the typed result to the response shape.

    The full result carries divergence, component evidence, and OKLCH coordinates —
    valuable to library consumers, noise to a palette API. Keep hex/probability/area per
    entry, for both the usage view (primary) and the roles view (derived), plus the
    per-theme fit score. Both UsageEntry and PaletteCandidate expose
    color/probability/area, so one trimming shape covers them.
    """
    themes = {
        theme.value: ThemeOut(
            usage={
                category.value: [
                    EntryOut(hex=e.color.hex, probability=e.probability, area=e.area)
                    for e in entries
                ]
                for category, entries in palette.usage.mapping.items()
            },
            roles={
                role.value: [
                    EntryOut(hex=c.color.hex, probability=c.probability, area=c.area)
                    for c in candidates
                ]
                for role, candidates in palette.roles.mapping.items()
            },
            fit_score=palette.fit_score,
        )
        for theme, palette in result.themes.items()
    }
    return AnalyzeResponse(url=result.url, themes=themes)
