# Security model & consumer responsibilities

`colorsense` fetches and **fully renders arbitrary URLs** in a headless Chromium browser,
executing the page's JavaScript and screenshotting it. That is its job — and it is also a
real attack surface. The library deliberately provides *mechanism, not policy*: it does not
decide whether a fetch is authorized or whether a destination is safe. **Those decisions are
the consumer's responsibility.**

This document spells out the risks you take on when you call `analyze`, and what you must do
about each. If you only ever feed `colorsense` **trusted, hardcoded URLs** — e.g. analyzing
your own sites — most of this is moot. The risks below apply when an **untrusted or
user-supplied** URL can reach `analyze` from a server context.

## 1. Server-Side Request Forgery (SSRF)

**`colorsense` performs no host or IP validation.** Beyond a scheme gate (only `http(s)` is
fetchable by default), it will render whatever you give it, including destinations an
attacker would love to reach from inside your network:

- cloud instance metadata endpoints — e.g. `http://169.254.169.254/` (AWS/GCP/Azure
  credentials, instance roles);
- `localhost` / loopback services and admin panels;
- internal, non-routable RFC 1918 addresses (`10.0.0.0/8`, `172.16.0.0/12`,
  `192.168.0.0/16`) and link-local ranges.

`file://` URLs — which read arbitrary local files — are **disabled by default**: fetching
one raises `UnsupportedSchemeError` unless you explicitly opt in with
`PolitenessPolicy(allow_file_urls=True)` (the test suite opts in to render its local
fixtures). All other schemes (`ftp`, `data`, `javascript`, ...) are always rejected.

**Validating the navigation URL is NOT sufficient.** The rendered page's **own JavaScript
and sub-resource requests** (scripts, images, XHR/`fetch`) can reach internal endpoints
regardless of where the navigation pointed — a perfectly public page can still probe
`169.254.169.254` from inside the browser. The in-library mechanism for this is
`PolitenessPolicy(request_filter=...)`: a predicate — synchronous or asynchronous — over
every HTTP(S) request URL the browser makes (the navigation, redirects, sub-resources,
and the page's own `fetch`/XHR included), aborting any request it rejects (and failing
closed if the predicate itself errors); a sync predicate runs inline on the event loop and
must not block. The two browser network paths the route interceptor cannot see are closed
off rather than filtered: **WebSocket connections are refused outright** whenever a
`request_filter` is configured (their opening handshakes bypass Playwright's
`context.route`, so instead of vetting `ws://` URLs the library never connects them at
all), and **service workers are always blocked** at browser-context creation (their
requests would otherwise bypass the route interceptor; rendering for color extraction
never needs them). `block_private_networks()` is a shipped filter for the common
case (see "What you must do" below); deciding *which* destinations are safe remains your
policy, and network isolation (below) remains the strong recommendation even with a filter
in place.

**Redirects make this worse.** A URL that *looks* public can bounce to an internal one:
both the `robots.txt` fetch and the Chromium navigation follow redirects, so allowlisting
only the *initial* host is insufficient. When a `request_filter` is configured, the policy
applies it to **both** paths per hop: the browser-side route handler vets the navigation,
every redirect hop, and all sub-resources, and the policy's own server-side `robots.txt`
GET vets the robots URL and each redirect `Location` before requesting it (a rejected hop
aborts the robots fetch, which then fails open as "no rules" — the navigation itself stays
gated by the filter).

### What you must do

If a user-supplied or otherwise untrusted URL can ever reach `analyze` from a server, **you
must enforce your own guard rails before and around the call**:

- **Allowlist** the schemes and hosts you are willing to fetch; reject everything else.
- **Filter egress in-library** with `request_filter`, so the rendered page's sub-resource
  requests are subject to the same rules as the navigation (configuring one also turns on
  the outright WebSocket refusal above). The library ships an
  implementation: `block_private_networks()` builds an async filter that resolves each
  hostname — off the event loop, on a small thread pool the filter itself owns (never the
  loop's shared default executor), with a fail-closed per-lookup timeout, per-host verdict
  caching and single-flight coalescing — and rejects any URL resolving to a private, loopback,
  link-local (metadata), CGNAT, multicast, or otherwise non-public address — failing
  closed on resolution failure, with an optional narrowing host allowlist. It does **not**
  fully defeat DNS rebinding: the
  filter sees URL strings, while Chromium resolves hostnames independently when it
  connects, so a hostname can flip to an internal address between check and connection.
- **Pin redirects**: re-validate the destination on every hop, or disallow redirects to
  hosts/IPs outside your allowlist (`request_filter` sees every hop).
- Prefer running the browser in a **network-isolated environment** (see §2) so that even a
  validation bypass — DNS rebinding included — cannot reach sensitive internal endpoints.
  This remains the primary control; the egress filter is defense in depth.

If you accept untrusted URLs and skip these steps, you have an SSRF vulnerability. A
reference implementation wiring these controls together (plus the §2 concurrency cap and
deadline) lives in [`examples/webservice/`](https://github.com/cassidyhhaas/colorsense/tree/main/examples/webservice) — a starting point, not
a substitute for network isolation.

## 2. Resource exhaustion & denial of service

Each `analyze` call launches a **full headless browser** and renders an
attacker-influenceable page with **JavaScript execution** and **full-page screenshotting**.
A hostile or merely pathological target can try to exhaust your resources: huge or infinite
pages, heavy scripts, many sub-resource requests, large DOMs, memory balloons.

The library's built-in bounds are the **navigation timeout**, **per-operation timeouts on
every post-navigation harvest step** (in-page evaluations, the CDP hover-probe pass — so a
page whose JS wedges the renderer after the load event fails bounded instead of hanging a
deadline-less `analyze()` forever), the per-host **rate limiter** in `PolitenessPolicy`
(including a capped `robots.txt` `Crawl-delay`), **capture dimension caps** on the
full-page screenshot (~20k x 10k px, additionally shrunk to fit the decode budget at the
session's device scale factor), a **decode pixel cap** rejecting decompression-bomb
captures, **harvest payload caps** bounding what crosses from the renderer into the host
Python process (10,000 element records / 5,000 token declarations per render — a hostile
page synthesizing millions of elements or custom properties cannot balloon the embedding
service's memory), and **caps on the policy's own `robots.txt` fetch** (request
timeout, 20-redirect cap, 512 KiB body cap — so a hostile robots host cannot stream
unbounded data into the server-side loader). Two further bounds exist but are **off by default — you must
set them**: `PolitenessPolicy(max_concurrent_renders=...)` caps simultaneous renders
through a policy, and `analyze(..., max_total_seconds=...)` deadlines a whole call (raising
`AnalysisTimeoutError`, a `TimeoutError` subclass). There is still **no cap** on the
*renderer process's* memory or on the number of sub-requests a page may make. The library
does not save downloaded files to disk — it captures an in-memory screenshot — but the
*render itself* is the cost, and it is unbounded unless you bound it.

**Hard per-render memory/CPU caps are container-level by design.** The library does not —
and will not — enforce them in-process: a userland watchdog cannot hold under exactly the
memory pressure it is supposed to guard against, and shipping one would invite false
confidence. Hard caps are the container/cgroup layer's job. What the library ships instead:
`max_total_seconds` bounds how *long* an abusive render runs, and
`analyze(..., browser_args=("--js-flags=--max-old-space-size=512",))` passes launch
arguments verbatim to Chromium — that one caps each renderer process's V8 heap at 512 MB.
The V8 flag bounds the **JS heap only**, not total renderer memory (DOM, images, GPU
buffers); the container limit remains the enforceable bound.

### What you must do

In any server handling many or large or untrusted targets, budget for abuse:

- **Container / sandbox isolation** for the browser process, with **hard memory and CPU
  limits** (e.g. cgroup limits) so a single target cannot take down the host. This is the
  enforceable cap; the library deliberately does not duplicate it in-process (above).
- **Cap the V8 heap in-browser**: pass
  `analyze(..., browser_args=("--js-flags=--max-old-space-size=512",))` so a script-driven
  memory balloon dies inside the renderer instead of growing until the container kills the
  whole browser. JS heap only — keep the container limit regardless.
- **Concurrency caps** on simultaneous renders: set
  `PolitenessPolicy(max_concurrent_renders=...)` on a shared policy instance, sized to your
  resource budget. Unset, the library will gladly launch as many browsers as you ask it to.
- **An overall deadline** per call: set `analyze(..., max_total_seconds=...)` — a page can
  pass the navigation timeout and still stall in scripting.
- **Network egress restrictions** on the browser's environment (which also hardens §1).
- Per-host rate limits via `PolitenessPolicy(min_interval=...)`.

## 3. `robots.txt` is respected by default — and fails open

By default (`respect_robots=True`) `colorsense` consults the target's `robots.txt`, raises
`RobotsDisallowedError` on a disallow, and honors a declared `Crawl-delay` in its per-host
rate limiter — capped at `max_crawl_delay` (30s by default) so a hostile or typo'd directive
cannot stall your pipeline; raise the cap to honor longer delays. Two caveats:

- **It fails open.** An unreachable, missing, or erroring `robots.txt` is treated as "no
  rules", which **permits** the fetch. This is the conventional interpretation, but it means
  you cannot rely on `robots.txt` as a security or authorization control — only as a
  politeness signal.
- **`respect_robots=False` disables the check entirely** — and the `Crawl-delay` honoring
  with it (no `robots.txt` is ever fetched), leaving only the scheme gate, `min_interval`
  rate limiter, and any `request_filter` you configured.

### What you must do

- **Do not set `respect_robots=False` unless you own the target site/surface, or have been
  explicitly authorized by its owner to crawl it.** Disabling robots is an explicit,
  accountable choice — not a default and not a shortcut.
- Treat **authorization as your own responsibility**, enforced *before* you call `analyze`
  (your terms of service, the requesting user's entitlement to view the page, your own rate
  limits). `robots.txt` does not establish that a fetch is permitted.

---

## Summary

| Risk | Library's stance | Your responsibility |
| --- | --- | --- |
| **SSRF** | `http(s)` only by default (`file://` opt-in, other schemes rejected); no host/IP validation unless configured; follows redirects; optional `request_filter` (sync or async) over every HTTP(S) browser request and the policy's own `robots.txt` fetch (each redirect hop included); WebSocket connections refused outright when a filter is configured (handshakes bypass route interception); service workers always blocked; `block_private_networks()` is the shipped filter — async, resolving DNS off the event loop (does not fully defeat DNS rebinding) | Allowlist hosts, configure `request_filter` (e.g. `block_private_networks()`), pin redirects, isolate egress — network isolation stays primary |
| **Resource / DoS** | Timeout, rate limiter (incl. capped `Crawl-delay`), capture dimension + decode pixel caps, robots-fetch caps (timeout, redirects, body size); opt-in `max_concurrent_renders` and `max_total_seconds` (both unset by default); opt-in V8-heap cap via `browser_args`; no in-process memory/CPU cap (by design) | Container limits (the enforceable cap), set `max_concurrent_renders` + `max_total_seconds`, cap the V8 heap via `browser_args`, network isolation |
| **`robots.txt`** | Respected by default (incl. `Crawl-delay`, capped at 30s), but fails open; can be disabled | Don't disable without authorization; gate authorization yourself before calling |

`colorsense` makes it easy to fetch and render considerately once **you** have decided a
fetch is authorized and safe. It never makes that decision for you. If you are unsure whether
your usage is exposed, assume it is, and apply the controls above.
