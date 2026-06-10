# Examples

Two ends of the trust spectrum, per [SECURITY.md](../SECURITY.md):

- **[`quickstart.py`](quickstart.py)** — the trusted-URL path: analyze hardcoded URLs you
  chose yourself, with a polite, identifiable fetch policy, and print each theme's palette
  roles. If this is your usage, most of SECURITY.md is moot.
- **[`webservice/`](webservice/)** — the untrusted-URL path: a FastAPI service that accepts
  user-supplied URLs and wires up the SECURITY.md checklist around `analyze` — pre-call URL
  validation ([`webservice/url_guard.py`](webservice/url_guard.py), the one control that
  stays app code), the library's `block_private_networks()` egress `request_filter`, a
  `max_concurrent_renders` cap, a `max_total_seconds` deadline, and a `browser_args`
  V8-heap cap ([`webservice/app.py`](webservice/app.py)), with library errors mapped to
  HTTP statuses.

> **The webservice is a reference implementation, not a security guarantee.** It shows how
> to configure the in-process controls [SECURITY.md](../SECURITY.md) requires, but a
> URL-string filter cannot fully defeat DNS rebinding (Chromium resolves hostnames
> independently), and nothing here hard-bounds per-render memory or CPU — the `browser_args`
> V8 cap bounds the JS heap only. Run the browser in a network-isolated, resource-limited
> environment regardless.

## Setup

From the repository root:

```bash
uv sync --group examples --group dev   # fastapi/uvicorn live in the `examples` group
uv run playwright install chromium     # one-time browser install
```

(Outside this repo: `pip install colorsense fastapi uvicorn && playwright install chromium`.)

## Running

```bash
# Quickstart — defaults to https://example.com; pass URLs to override:
uv run python examples/quickstart.py
uv run python examples/quickstart.py https://your-site.example

# Webservice — from the repo root, then POST a URL:
uv run uvicorn examples.webservice.app:app
curl -s -X POST localhost:8000/analyze \
    -H 'content-type: application/json' -d '{"url": "https://example.com"}'
```

The webservice reads `COLORSENSE_ALLOWED_HOSTS` (comma-separated hostname allowlist;
unset = any public host), `COLORSENSE_MAX_CONCURRENCY` (default 2),
`COLORSENSE_DEADLINE_SECONDS` (default 60), and `COLORSENSE_BROWSER_ARGS`
(whitespace-separated extra Chromium launch args with shell-style quoting, parsed via
`shlex.split`; default `--js-flags=--max-old-space-size=512`, the V8-heap cap; set to an
empty string for none). Quote flags that contain spaces or commas so they stay one
argument — e.g. `--host-resolver-rules='MAP a 1.2.3.4, MAP b 5.6.7.8'`.

The pre-call validation in `webservice/url_guard.py` is plain stdlib and unit-tested
without a browser or FastAPI — see `tests/test_examples_url_guard.py`. The address-level
egress filtering is the library's `block_private_networks`, tested in `tests/test_guard.py`.
