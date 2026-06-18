# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# Conventions
Source of truth for style is `pyproject.toml` ([tool.ruff]). PEP 8 + PEP 257 apply.
- Mypy is strict and the code is fully typed (`py.typed` ships); keep new code that way.
- This project is an SSRF surface by nature (it fetches and renders arbitrary URLs). Security-relevant changes (anything in `net/`, fetch/redirect behavior, URL handling) must keep `SECURITY.md` accurate; `examples/webservice/` is the reference implementation of its controls.
- Update `README.md`/`docs/`/`SECURITY.md` on behavior or API changes, and add a `CHANGELOG.md` line under *Unreleased*.

YOU MUST:
- Run `ruff format` and `ruff check --fix` on any file you edit, before finishing.
- When you read existing code that diverges from these conventions, call it out
  explicitly in your summary — file, line, which rule, and whether it's auto-fixable
  vs. a judgment call (naming, docstring content, structure). Do not silently fix
  unrelated code while doing something else.
- Keep formatting-only changes in separate commits from logic changes.

## What this is

colorsense renders a website in headless Chromium (Playwright), harvests its design tokens and computed element colors, and classifies them by usage — what colors paint surfaces, text, interactive elements, and borders — plus a derived 60/30/10 roles view, returned as a frozen Pydantic `AnalysisResult`. Python 3.12+, src layout, `uv`-managed.

## Commands

```bash
uv sync --group examples             # install deps (examples group is needed for mypy to pass)
uv run playwright install chromium   # one-time browser download for browser-marked tests

uv run ruff check .                  # lint
uv run ruff format --check .         # formatting
uv run mypy                          # strict; targets src + examples from pyproject [tool.mypy] files
uv run pytest                        # full suite
uv run pytest -m "not browser"       # fast browserless subset (pre-push hook runs this)
uv run pytest -m browser             # tests that launch real Chromium
uv run pytest tests/test_guard.py -k name   # single file / test
```

CI (`.github/workflows/ci.yml`) runs build, lint (ruff + strict mypy), and test, with a **90% coverage floor** across the browserless and browser runs combined. Pre-commit hooks mirror CI exactly via `uv run` (`uv run pre-commit install`, plus `-t pre-push` for tests).

Golden snapshots: `tests/test_integration_sites.py` pins analysis output. If a change intentionally alters results, regenerate with `UPDATE_GOLDEN=1 uv run pytest tests/test_integration_sites.py` and explain the churn in the PR.

## Architecture

`analyze()` in `src/colorsense/pipeline.py` is the single async entry point wiring the stages, per requested theme (light by default; dark opt-in via `themes=LIGHT_AND_DARK`; near-identical renders collapse to one theme):

1. **harvest/** — everything that touches a live page. `render.py` (`RenderSession`/`SharedBrowser`) drives Playwright; `dom.py` walks visible elements with computed colors; `tokens.py` enumerates declared CSS custom properties via CSSOM; `states.py` probes hover/focus color changes via CDP; `screenshot.py` quantizes a masked full-page screenshot into area-weighted bins.
2. **classify/** — `tokens.py` maps declared tokens to semantic roles + usage priors (precedence: relational → name rule → scale detection); `components.py` scores DOM elements into component types. All weights/vocabulary come from the config YAML, nothing hard-coded.
3. **palette/** — `inventory.py` fuses screenshot area-truth with element semantics into `ColorCluster`s; `usage.py` builds the primary usage-keyed view (probability-ranked entries per usage category, measured evidence only); `reconcile.py` pools that measured usage against declared token intent (log-linear) and reports divergences; `roles.py` derives the measured-only 60/30/10 roles view with its `fit_score`.
4. **net/** — `politeness.py` (`PolitenessPolicy`) is the *only* place networking policy lives: robots.txt gate, per-host rate limit, render cache, scheme gate (`file://` is opt-in), and the `request_filter` egress hook. `guard.py` ships `block_private_networks()`, the SSRF egress filter applied to every browser request and the robots fetch itself.

Key boundary: **networking lives entirely behind `PolitenessPolicy`/`harvest_page`; everything downstream is pure given a `Harvest`**, which is why the pipeline/classify/palette layers are testable without a network or browser. Per-theme CPU work runs in `asyncio.to_thread`.

Other load-bearing pieces:

- `models.py` — the shared contracts for all stages; treated as frozen: change contracts centrally and re-validate every dependent module, never patch locally.
- `config.py` + `data/palette_config.yaml` — the YAML is the single source of truth for all classifier weights, vocabularies, and priors; `config.py` only models and loads it. The YAML must ship in the wheel (CI verifies).
- `colorsense/__init__.py` — the **canonical public API**. Anything not re-exported there is internal and free to change; be deliberate about new exports.
- `cli.py` — `colorsense` console script. Human-readable output is unstable by design; `--json` follows the library's compatibility story. stdout is data-only; warnings/errors go to stderr.

## Testing conventions

- The suite is **network-free**: live-page tests render fixture HTML from `tests/fixtures/` over `file://`, opting in via the `file_policy()` helper in `tests/conftest.py` (`PolitenessPolicy(allow_file_urls=True)`).
- Tests launching real Chromium are marked `browser` (a `--strict-markers` marker).
- `examples/` is not installed — `conftest.py` puts the repo root on `sys.path` so its tests import it directly. It is linted, type-checked (mypy strict), and tested like library code.
