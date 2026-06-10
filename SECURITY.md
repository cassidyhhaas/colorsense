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
`PolitenessPolicy(request_filter=...)`: a predicate over **every** request URL the browser
makes (the navigation included), aborting any request it rejects (and failing closed if the
predicate itself errors). Deciding *which* destinations are safe remains your policy;
network isolation (below) remains the strong recommendation even with a filter in place.

**Redirects make this worse.** A URL that *looks* public can bounce to an internal one:
both the `robots.txt` fetch (`httpx` with `follow_redirects=True`) and the Chromium
navigation follow redirects. Allowlisting only the *initial* host is therefore insufficient.

### What you must do

If a user-supplied or otherwise untrusted URL can ever reach `analyze` from a server, **you
must enforce your own guard rails before and around the call**:

- **Allowlist** the schemes and hosts you are willing to fetch; reject everything else.
- **Block private, loopback, and link-local IP ranges** — resolve the host and check the
  resolved address, not just the literal string (to defeat DNS rebinding and decimal/hex IP
  encodings).
- **Pin redirects**: re-validate the destination on every hop, or disallow redirects to
  hosts/IPs outside your allowlist.
- **Filter egress in-library** with `request_filter` so the rendered page's sub-resource
  requests are subject to the same allowlist as the navigation.
- Prefer running the browser in a **network-isolated environment** (see §2) so that even a
  validation bypass cannot reach sensitive internal endpoints.

`colorsense` will not do any of this for you. If you accept untrusted URLs and skip these
steps, you have an SSRF vulnerability.

## 2. Resource exhaustion & denial of service

Each `analyze` call launches a **full headless browser** and renders an
attacker-influenceable page with **JavaScript execution** and **full-page screenshotting**.
A hostile or merely pathological target can try to exhaust your resources: huge or infinite
pages, heavy scripts, many sub-resource requests, large DOMs, memory balloons.

The library's built-in bounds are the **navigation timeout**, the per-host **rate limiter**
in `PolitenessPolicy` (including a capped `robots.txt` `Crawl-delay`), **capture dimension
caps** on the full-page screenshot (~20k x 10k px), and a **decode pixel cap** rejecting
decompression-bomb captures. There is still **no cap** on per-page memory, on the number of
sub-requests a page may make, or on overall render concurrency. The library does not save
downloaded files to disk — it captures an in-memory screenshot — but the *render itself* is
the cost, and it is unbounded by default.

### What you must do

In any server handling many or large or untrusted targets, budget for abuse:

- **Container / sandbox isolation** for the browser process, with **hard memory and CPU
  limits** (e.g. cgroup limits) so a single target cannot take down the host.
- **Concurrency caps** on simultaneous `analyze` calls — a queue or semaphore — sized to
  your resource budget. The library will gladly launch as many browsers as you ask it to.
- **Network egress restrictions** on the browser's environment (which also hardens §1).
- Conservative **timeouts** and per-host rate limits via `PolitenessPolicy`.

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
| **SSRF** | `http(s)` only by default (`file://` opt-in, other schemes rejected); no host/IP validation; follows redirects; optional `request_filter` over every browser request | Allowlist hosts, block private/loopback/link-local IPs, pin redirects, configure `request_filter`, isolate egress |
| **Resource / DoS** | Timeout, rate limiter (incl. capped `Crawl-delay`), capture dimension + decode pixel caps; no memory/concurrency caps | Container limits, concurrency caps, timeouts, network isolation |
| **`robots.txt`** | Respected by default (incl. `Crawl-delay`, capped at 30s), but fails open; can be disabled | Don't disable without authorization; gate authorization yourself before calling |

`colorsense` makes it easy to fetch and render considerately once **you** have decided a
fetch is authorized and safe. It never makes that decision for you. If you are unsure whether
your usage is exposed, assume it is, and apply the controls above.
