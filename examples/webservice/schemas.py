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


class CandidateOut(BaseModel):
    """One palette candidate, trimmed to what API consumers paint with."""

    hex: str
    probability: float
    area: float


class AnalyzeResponse(BaseModel):
    """Trimmed response: per-theme role candidates plus the overall fit score.

    ``themes`` maps theme name -> role name -> ranked candidates (best first; empty list
    when the role was not detected).
    """

    url: str
    fit_score: float
    themes: dict[str, dict[str, list[CandidateOut]]]


def shape_response(result: AnalysisResult) -> AnalyzeResponse:
    """Trim ``result.model_dump()`` to the response shape.

    The full dump carries tokens, divergence, evidence trails, and OKLCH coordinates —
    valuable to library consumers, noise to a palette API. Keep hex/probability/area per
    candidate and the fit score.
    """
    dump = result.model_dump(mode="json")
    themes: dict[str, dict[str, list[CandidateOut]]] = {
        theme: {
            role: [
                CandidateOut(
                    hex=candidate["color"]["hex"],
                    probability=candidate["probability"],
                    area=candidate["area"],
                )
                for candidate in candidates
            ]
            for role, candidates in palette["roles"]["mapping"].items()
        }
        for theme, palette in dump["themes"].items()
    }
    return AnalyzeResponse(url=dump["url"], fit_score=dump["fit_score"], themes=themes)
