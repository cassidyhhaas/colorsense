# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **`block_private_networks()`** — a library-shipped egress `request_filter` factory (new
  public export) that resolves each hostname and rejects URLs resolving to any non-public
  address (loopback, RFC 1918, link-local/cloud-metadata, CGNAT, multicast, reserved, and
  IPv6 equivalents), failing closed, with a per-hostname TTL+LRU verdict cache and an
  optional narrowing `allowed_hosts` allowlist. Implements the SECURITY.md §1 egress-filter
  item; does not fully defeat DNS rebinding — network isolation remains the primary control.
- **`PolitenessPolicy(max_concurrent_renders=...)`** — an opt-in semaphore bounding
  simultaneous renders through a policy (unbounded by default). Cache hits and coalesced
  duplicate fetches never take a slot, and rate-limit waits happen outside the slot.
- **`analyze(..., max_total_seconds=...)`** — an opt-in overall deadline on the whole call
  (renders plus classification) via `asyncio.timeout`; expiry cancels in-flight renders,
  closes the shared browser, and raises the new **`AnalysisTimeoutError`** (a public
  export subclassing the builtin `TimeoutError`, carrying the url and budget).

### Changed

- The `examples/webservice/` reference implementation now uses the new library knobs
  (`block_private_networks`, `max_concurrent_renders`, `max_total_seconds`) instead of
  hand-rolled equivalents; only the pre-call navigation-URL validation remains app code.

## [0.2.0] - 2026-06-09

First release to include the SSRF hardening work; also the first to ship the restructured
documentation set.

### Added

- **SSRF controls.** A URL scheme gate — only `http(s)` is fetched by default; `file://`
  requires an explicit `PolitenessPolicy(allow_file_urls=True)` opt-in, and every other
  scheme raises the new `UnsupportedSchemeError` (now a public export). A new
  `request_filter` predicate gates **every** request the browser makes (the navigation and
  the page's own sub-resources), the in-library defense against sub-resource SSRF. (#14)
- **`SECURITY.md`** documenting the threat model — SSRF, resource exhaustion / DoS, and the
  fail-open `robots.txt` gate — and the controls consumers must enforce. (#13)
- Capped `robots.txt` `Crawl-delay` honoring via a new `max_crawl_delay` policy knob (30 s
  default) so a hostile or typo'd directive cannot stall the pipeline. (#14)
- Screenshot capture safeguards: dimension caps and a decode pixel cap that rejects
  decompression-bomb captures. (#14)
- Dependabot for GitHub Actions and Python dependencies. (#12)

### Changed

- Themes now render concurrently through a single shared headless Chromium launch instead
  of one browser per theme. (#14)
- The configured User-Agent is now sent on the page render itself, not just the
  `robots.txt` GET, so the render is attributable to the same identity. (#14)
- Documentation restructured into a slim README plus `docs/usage.md`, `docs/advanced.md`,
  `CONTRIBUTING.md`, and this changelog. (#15)

### Removed

- Dead code paths in the harvest / screenshot layers. (#14)

## [0.1.0] - 2026-06-09

Initial public release.

- `analyze(url)` — async pipeline that renders a page in headless Chromium, harvests
  design tokens and computed element colors, and classifies them into a 60/30/10 palette
  with ranked, scored candidates per role.
- Typed, frozen Pydantic result (`AnalysisResult`) with per-theme palettes, OKLCH-bearing
  colors, declared-vs-rendered token divergence, status-color filtering, fit scoring, and
  run metadata.
- Optional dark-mode analysis (`themes=LIGHT_AND_DARK`) with single-theme collapse for
  sites that ignore `prefers-color-scheme`.
- `PolitenessPolicy` — configurable User-Agent, `robots.txt` gate, per-host rate limiting,
  and an LRU render cache.
- Bundled, overridable palette configuration (`config_path=` / `load_config`).
- Fully typed (`py.typed`), Python 3.12+.

[Unreleased]: https://github.com/cassidyhhaas/colorsense/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/cassidyhhaas/colorsense/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/cassidyhhaas/colorsense/releases/tag/v0.1.0
