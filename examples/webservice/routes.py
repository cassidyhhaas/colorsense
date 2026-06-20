"""The ``/analyze`` endpoint: pre-call validation, the bounded render, error mapping."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from colorsense import (
    AnalysisTimeoutError,
    RenderError,
    RobotsDisallowedError,
    UnsupportedSchemeError,
    analyze,
)
from examples.webservice.policy import POLICY
from examples.webservice.schemas import AnalyzeRequest, AnalyzeResponse, shape_response
from examples.webservice.settings import ALLOWED_HOSTS, ANALYZE_DEADLINE_SECONDS, BROWSER_ARGS
from examples.webservice.url_guard import validate_target_url

router = APIRouter()


@router.post("/analyze")
async def analyze_palette(request: AnalyzeRequest) -> AnalyzeResponse:
    """Analyze one untrusted URL and return its trimmed palette.

    Args:
        request: The POST body carrying the user-supplied URL to analyze.

    Returns:
        The trimmed per-theme palette for the analyzed URL.

    Raises:
        HTTPException: 400 if the URL fails pre-call validation or uses an
            unsupported scheme, 403 if the target's robots.txt disallows the fetch,
            502 if the page fails to load or render, or 504 if the analysis exceeds
            the configured deadline.

    """
    # Pre-call validation first: nothing browser-shaped happens for input that fails the
    # cheap checks (scheme, userinfo, allowlist). Address-level checks are NOT done here —
    # they would only cover the initial URL; the policy's request_filter covers every hop.
    try:
        validate_target_url(request.url, allowed_hosts=ALLOWED_HOSTS)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err

    # The §2 bounds are library knobs now: the policy's max_concurrent_renders caps
    # simultaneous renders, max_total_seconds deadlines the whole call, and browser_args
    # caps each renderer's V8 heap at the Chromium launch.
    try:
        result = await analyze(
            request.url,
            politeness=POLICY,
            max_total_seconds=ANALYZE_DEADLINE_SECONDS,
            browser_args=BROWSER_ARGS,
        )
    except AnalysisTimeoutError as err:
        raise HTTPException(
            status_code=504, detail="analysis exceeded the configured deadline"
        ) from err
    except RobotsDisallowedError as err:
        # The target's robots.txt disallows the fetch; the client cannot fix that.
        raise HTTPException(status_code=403, detail=str(err)) from err
    except UnsupportedSchemeError as err:
        # Defense in depth: validate_target_url should have caught this already.
        raise HTTPException(status_code=400, detail=str(err)) from err
    except RenderError as err:
        # The upstream page failed to load/render (DNS, TLS, nav timeout, abort by the
        # request_filter, ...): a bad gateway, not a client error.
        raise HTTPException(status_code=502, detail=str(err)) from err
    return shape_response(result)
