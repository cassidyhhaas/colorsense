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
  sub-resources, the page's own ``fetch`` calls — and the policy's own ``robots.txt``
  fetch (each of its redirect hops vetted before being requested) is checked against
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

Where each control lives: ``settings.py`` reads the environment knobs, ``policy.py`` holds
the shared :class:`~colorsense.PolitenessPolicy` (egress filter, concurrency cap, UA,
robots), ``url_guard.py`` is the pre-call navigation gate, ``schemas.py`` the API models
and response trimming, and ``routes.py`` the endpoint with its exception→HTTP-status
mapping. This module only assembles the app.

Run from the repository root (needs the ``examples`` dependency group):

    uv sync --group examples --group dev
    uv run uvicorn examples.webservice.main:app

    curl -s -X POST localhost:8000/analyze \
        -H 'content-type: application/json' -d '{"url": "https://example.com"}'

The environment knobs are documented (and parsed) in ``settings.py``.
"""

from __future__ import annotations

from fastapi import FastAPI

from examples.webservice.routes import router

app = FastAPI(
    title="colorsense palette service",
    description="Reference implementation of the SECURITY.md controls for untrusted URLs.",
)
app.include_router(router)
