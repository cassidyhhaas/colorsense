# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
- `PolitenessPolicy` — configurable User-Agent, `robots.txt` gate with capped
  `Crawl-delay`, per-host rate limiting, LRU render cache, scheme gate (`file://` opt-in),
  and a per-request egress `request_filter`.
- Bundled, overridable palette configuration (`config_path=` / `load_config`).
- Fully typed (`py.typed`), Python 3.12+.

[Unreleased]: https://github.com/cassidyhhaas/colorsense/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/cassidyhhaas/colorsense/releases/tag/v0.1.0
