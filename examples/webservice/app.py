"""A palette-extraction web service for UNTRUSTED, user-supplied URLs.

This app is a line-by-line reference implementation of the SECURITY.md checklist — every
control it demands when untrusted URLs can reach ``analyze`` from a server appears here,
most of them now as library knobs:

* **Pre-call validation** (§1 "Allowlist"): :func:`url_guard.validate_target_url` gates
  scheme/userinfo/host before a browser is ever involved; an optional host allowlist comes
  from ``COLORSENSE_ALLOWED_HOSTS``. This stays app code — which navigations you accept is
  your policy.
* **Egress filtering** (§1 "Filter egress in-library"): the library-shipped
  :func:`colorsense.block_private_networks` is installed as the policy's
  ``request_filter``, so every request the rendered page makes — navigation, redirects,
  sub-resources, the page's own ``fetch`` calls — is checked against
  private/loopback/link-local/metadata ranges, failing closed.
* **Concurrency cap** (§2): ``PolitenessPolicy(max_concurrent_renders=...)`` bounds
  simultaneous renders process-wide (the policy instance is shared, so its semaphore is).
* **Overall deadline** (§2): ``analyze(..., max_total_seconds=...)`` bounds each call end
  to end and raises ``AnalysisTimeoutError`` (mapped to 504). Note the budget covers any
  wait for a render slot too — size deadline and concurrency together.
* **V8 heap cap** (§2): ``analyze(..., browser_args=...)`` passes
  ``--js-flags=--max-old-space-size=512`` verbatim to the Chromium launch, so a
  script-driven memory balloon dies inside the renderer. JS heap only — the container
  memory limit stays mandatory.
* **Politeness** (§3): robots stays respected, the User-Agent is identifiable, and
  same-host fetches are paced.

What this app deliberately does NOT give you (also per SECURITY.md): a URL-string filter
cannot fully defeat DNS rebinding (Chromium resolves hostnames itself — see the
``block_private_networks`` docstring), and there are no hard per-render memory/CPU bounds
here — the V8-heap cap bounds the JS heap only, and the library ships no in-process memory
watchdog by design (SECURITY.md §2). Run the service in a network-isolated,
resource-limited container; this code is the in-process half of the controls, not the
whole story.

Run from the repository root (needs the ``examples`` dependency group):

    uv sync --group examples --group dev
    uv run uvicorn examples.webservice.app:app

    curl -s -X POST localhost:8000/analyze \
        -H 'content-type: application/json' -d '{"url": "https://example.com"}'

Environment knobs: ``COLORSENSE_ALLOWED_HOSTS`` (comma-separated exact hostnames; unset =
any public host), ``COLORSENSE_MAX_CONCURRENCY`` (default 2),
``COLORSENSE_DEADLINE_SECONDS`` (default 60), and ``COLORSENSE_BROWSER_ARGS``
(comma-separated extra Chromium launch args; default caps each renderer's V8 heap at
512 MB).
"""

from __future__ import annotations

import os

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from colorsense import (
    AnalysisResult,
    AnalysisTimeoutError,
    PolitenessPolicy,
    RenderError,
    RobotsDisallowedError,
    UnsupportedSchemeError,
    analyze,
    block_private_networks,
)
from examples.webservice.url_guard import validate_target_url


def _allowed_hosts_from_env() -> frozenset[str] | None:
    """Optional exact-hostname allowlist, comma-separated; ``None`` = no allowlist."""
    raw = os.environ.get("COLORSENSE_ALLOWED_HOSTS", "")
    hosts = frozenset(host.strip().lower() for host in raw.split(",") if host.strip())
    return hosts or None


ALLOWED_HOSTS = _allowed_hosts_from_env()

# SECURITY.md §2: cap simultaneous renders. Each render is a full headless-browser page
# with JS execution — unbounded, the library will launch as many as asked. The cap lives
# on the shared policy below (max_concurrent_renders), so it bounds the whole process.
MAX_CONCURRENT_ANALYSES = int(os.environ.get("COLORSENSE_MAX_CONCURRENCY", "2"))

# SECURITY.md §2: an overall deadline per analyze call, over and above the library's own
# navigation timeout — a page can pass navigation and still stall in scripting. Passed as
# analyze(max_total_seconds=...); covers render-slot queueing too, so size it with the
# concurrency cap in mind.
ANALYZE_DEADLINE_SECONDS = float(os.environ.get("COLORSENSE_DEADLINE_SECONDS", "60"))

# SECURITY.md §2: cap each renderer process's V8 heap in-browser, passed verbatim to the
# Chromium launch via analyze(browser_args=...). This bounds the JS heap only — not total
# renderer memory — so it complements, never replaces, the container/cgroup memory limit
# (the enforceable cap; the library ships no in-process memory watchdog by design).
# Override with COLORSENSE_BROWSER_ARGS (comma-separated; empty string = no extra args).
BROWSER_ARGS = tuple(
    arg.strip()
    for arg in os.environ.get(
        "COLORSENSE_BROWSER_ARGS", "--js-flags=--max-old-space-size=512"
    ).split(",")
    if arg.strip()
)

# One policy for the process: its render cache, per-host rate limiter, robots cache, and
# render-concurrency semaphore are all per-policy state, so sharing the instance is what
# makes them effective.
# - identifiable UA: site operators can attribute and contact (SECURITY.md §3);
# - robots respected (the default): kept explicit here because disabling it for
#   user-supplied targets would be exactly the unaccountable choice SECURITY.md warns about;
# - request_filter: the library-shipped egress gate over every browser request (§1).
#   The navigation-host allowlist is enforced pre-call (validate_target_url); the egress
#   filter deliberately gets no allowlist, since an allowed page legitimately loads
#   sub-resources from other (public) hosts;
# - max_concurrent_renders: the §2 concurrency cap, in-library.
_policy = PolitenessPolicy(
    user_agent=(
        "colorsense-example-webservice/0.1 "
        "(+https://github.com/cassidyhhaas/colorsense/tree/main/examples)"
    ),
    respect_robots=True,
    min_interval=1.0,
    request_filter=block_private_networks(),
    max_concurrent_renders=MAX_CONCURRENT_ANALYSES,
)

app = FastAPI(
    title="colorsense palette service",
    description="Reference implementation of the SECURITY.md controls for untrusted URLs.",
)


class AnalyzeRequest(BaseModel):
    """POST body: the page to analyze. Validation happens in the endpoint, not the model,
    so rejections produce a 400 with a reason rather than a generic 422."""

    url: str


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


def _shape_response(result: AnalysisResult) -> AnalyzeResponse:
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


@app.post("/analyze")
async def analyze_palette(request: AnalyzeRequest) -> AnalyzeResponse:
    """Analyze one untrusted URL and return its trimmed palette."""
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
            politeness=_policy,
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
    return _shape_response(result)
