# colorsense v0.1.0 — Pre-Release Review

*Independent, verify-don't-trust review. Six areas investigated by dedicated agents; every
finding below was reproduced by running code, building/installing the wheel, or executing
the suite. Orchestrator independently re-confirmed the two highest-impact findings.*

---

## Release recommendation: **SHIP AFTER FIXES** (no hard blockers)

The library is in genuinely good shape. The core color math and determinism are correct
and verified, the test suite is real (102 tests pass; ruff + strict mypy clean), the wheel
builds and installs cleanly with bundled config + `py.typed` resolving correctly from an
installed package, and the README is unusually accurate.

There are **no release blockers** — nothing is broken or unshippable. But there is a tight
set of **majors** worth fixing first, and critically, **a few are public-contract decisions
that are expensive to walk back after a public v0.1.0**. Fix those now; the rest can follow
in a 0.1.x.

**Fix-before-publish shortlist (cheap, high-value):**
1. Drop 4 unused heavy dependencies (`scikit-learn`, `scikit-image`, `tinycss2`, `pydantic-settings`) — every install currently pulls scipy + the scikit stack for dead code.
2. Fix or document the README's headline example, which crashes on a blank/failed render.
3. Wrap raw Playwright exceptions in a documented typed error (or document what escapes `analyze`).
4. Fix robots agent-specific matching (the default UA defeats `User-agent: colorsense` disallows).
5. Lock in the hard-to-change API shapes now: `metadata` typing, `Viewport` field names, export `Theme`.

---

## Executive summary

| Area | Verdict |
|---|---|
| Packaging & PyPI readiness | Wheel/sdist correct, config + py.typed resolve from install ✅ — but 4 unused heavy deps |
| Public API & contracts | Clean and importable ✅ — one crashing README example; some shapes to lock in now |
| Core correctness & determinism | Strong ✅ — verified deterministic, no RNG; only minor edge cases |
| Rendering, networking & safety | Browser lifecycle solid ✅ — robots UA gap, raw exceptions leak, SSRF/file:// undocumented |
| Tests & quality gates | All gates pass ✅ — missing-golden auto-create can mask regressions |
| Docs accuracy | Very accurate ✅ — one overstated config claim |

**What's verified-good (don't re-litigate):** browser/context/page teardown always runs
(zero leaked Chromium confirmed across exception + DNS-failure paths); rate limiter correctly
serializes concurrent same-host renders; cache key includes theme+viewport (no light/dark
collision); `delta_e` is consistent OKLab everywhere; role probabilities sum to 1.0 and
`fit_score ∈ [0,1]` (300 trials); JSON round-trips including enum-keyed dicts; bundled YAML
loads via `importlib.resources` (zipimport-safe); all README imports resolve.

---

## Blockers

*None.*

---

## Majors

### M1 — Four declared dependencies are entirely unused (drag in scipy + scikit stack)
- **Area:** packaging · **Location:** [pyproject.toml:30-33](pyproject.toml#L30)
- **Evidence:** `grep -rnE "sklearn|skimage|tinycss2|pydantic_settings|BaseSettings|scipy" src tests` → **no matches** (orchestrator-confirmed). Installing the wheel pulls `scikit-learn`, `scikit-image`, `tinycss2`, `pydantic-settings` plus their transitive stack (scipy, networkx, imageio, tifffile, joblib, threadpoolctl, lazy-loader) — hundreds of MB for code that never runs.
- **Why it matters:** Every install pays download/compile time and CVE surface for dead weight. `pydantic-settings` is declared but `BaseSettings` is never used.
- **Fix:** Remove all four from `[project].dependencies`; rebuild + re-import to confirm (it won't break). **Confidence: high.**

### M2 — README's headline example crashes on a blank/failed render (contract gap)
- **Area:** api/docs · **Location:** [README.md:46](README.md#L46); [palette/roles.py](src/colorsense/palette/roles.py); [palette/reconcile.py](src/colorsense/palette/reconcile.py); [models.py:229](src/colorsense/models.py#L229)
- **Evidence:** `roles.mapping` is `dict[PaletteRole, list[PaletteCandidate]]` with default `{}`. `assign_roles([])` → `mapping={}`, and `reconcile` only adds a role `if candidates`. So a page that renders blank / fails to harvest / has no detectable colors yields an empty mapping, and the README's `roles.mapping[PaletteRole.primary][0]` raises `KeyError`; even when populated, `[0]` on an empty list is an `IndexError`. The contract nowhere guarantees all five roles are present or non-empty.
- **Why it matters:** The single most-copied snippet crashes on a legitimate input. This is also a **contract-shape decision that's expensive to change post-1.0**: either guarantee all roles always map to a (possibly empty) list, or commit to sparse keys and document defensive access.
- **Fix:** Pick one and state it in the `RoleResults` docstring; update the README to `cands = roles.mapping.get(PaletteRole.primary, []); if cands: ...`. **Confidence: high.**

### M3 — Raw Playwright exceptions leak out of `analyze()`; only `RobotsDisallowedError` is documented
- **Area:** safety · **Location:** [harvest/__init__.py:48](src/colorsense/harvest/__init__.py#L48); [harvest/render.py:196](src/colorsense/harvest/render.py#L196); [pipeline.py:123](src/colorsense/pipeline.py#L123)
- **Evidence:** A DNS failure surfaces as `playwright._impl._errors.Error: net::ERR_NAME_NOT_RESOLVED`, propagating unchanged through `fetch` → `analyze`. `analyze`'s `Raises:` section lists only `RobotsDisallowedError`.
- **Why it matters:** A public library that fetches third-party pages will constantly hit DNS/timeout/TLS/non-HTML failures. Consumers are forced to catch a version-private Playwright type (`playwright._impl._errors.*`) that isn't part of any contract and can change between Playwright versions.
- **Fix:** Wrap navigation/render failures in a documented typed error (e.g. `RenderError`) and list it under `Raises`. **Confidence: high.**

### M4 — robots.txt agent-specific disallows are silently ignored under the default UA
- **Area:** safety · **Location:** [net/politeness.py:29-31, 169](src/colorsense/net/politeness.py#L29)
- **Evidence:** Default UA is `Mozilla/5.0 (compatible; colorsense/0.1; ...)`. `RobotFileParser.can_fetch` matches on the first `/`-delimited token → `mozilla`. Orchestrator-confirmed: a `User-agent: colorsense` + `Disallow: /` rule returns `can_fetch=True` (**not** blocked) with the default UA, but `False` when the token is `colorsense`. Wildcard `User-agent: *` rules **do** still apply (the common case), limiting blast radius.
- **Why it matters:** The library's central claim is politeness-by-default, yet the exact mechanism a site uses to block *this* crawler — naming its product token — is defeated.
- **Fix:** Pass the product token (`colorsense`) to `parser.can_fetch()` while still sending the full UA on the wire. **Confidence: high.**

### M5 — Missing golden file auto-creates a new baseline and passes silently
- **Area:** tests · **Location:** [tests/test_integration_sites.py:96](tests/test_integration_sites.py#L96)
- **Evidence:** `if os.environ.get("UPDATE_GOLDEN") or not path.exists(): ...write_text...; return`. Deleting `tests/golden/ds_site.json` and running the test → **PASS**, silently regenerating the file from current output.
- **Why it matters:** A golden that's deleted, gitignored, or absent from an sdist causes a real regression to be captured as the new "correct" answer with a green test.
- **Fix:** Only write when `UPDATE_GOLDEN` is explicitly set; if a golden is missing and `UPDATE_GOLDEN` is unset, **fail** with a clear message. **Confidence: high.**

---

## Hard-to-walk-back after a public v0.1.0 (decide now)

These aren't bugs, but they freeze on first publish — cheap now, breaking later.

- **`metadata: dict[str, str]` with stringified values** ([pipeline.py:230](src/colorsense/pipeline.py#L230), [models.py:266](src/colorsense/models.py#L266)) — booleans become `"true"`/`"false"`, theme lists become comma-joined strings. Awkward for a "fully typed result." Consider a typed `RunMetadata` model before 1.0. *(minor severity, high cost-to-change)*
- **`Viewport(w=, h=)` / `Rect(w=, h=)` field names** ([models.py:105-123](src/colorsense/models.py#L105)) — terse `w`/`h` alongside verbose `device_scale_factor`; part of the public constructor and JSON shape. Lock in `w/h` vs `width/height` now. *(nit, high cost-to-change)*
- **`Theme` is not top-level exported** ([__init__.py:23](src/colorsense/__init__.py#L23)) — yet both the README (line 87) and `analyze`'s docstring tell users to pass `themes=(Theme.light, Theme.dark)`. `from colorsense import Theme` raises ImportError. Adding the export is purely additive; do it in 0.1.0 so the guidance matches. *(nit, trivial fix)*

---

## Quick wins (minor)

- **Unpinned dependency floors** ([pyproject.toml:24-33](pyproject.toml#L24)) — `httpx`, `coloraide`, `numpy`, `pillow`, `pyyaml` have no lower bounds. Add conservative floors reflecting tested versions. *(packaging)*
- **`rgb()` out-of-gamut values are perceptually mapped, not CSS-clamped** ([color/primitives.py:72-90](src/colorsense/color/primitives.py#L72)) — `rgb(300,0,0)` → `#ff6c5b` instead of `#ff0000`. Browsers clamp; rare in real computed-style harvests. Clamp sRGB channels before `.fit()` or document. *(correctness)*
- **Unbounded caches grow forever** ([net/politeness.py:131-133](src/colorsense/net/politeness.py#L131)) — `_cache` (full Harvests), `_robots_cache`, `_last_fetch` never evict. Slow memory leak in the documented long-running FastAPI server. Add an LRU bound. *(safety)*
- **Implicit 30s navigation timeout, not configurable** ([harvest/render.py:196](src/colorsense/harvest/render.py#L196)) — `page.goto` sets no explicit timeout. Make it explicit and overridable. *(safety)*
- **SSRF / arbitrary local-file read is undocumented** ([README.md:90-122](README.md#L90); [harvest/render.py:196](src/colorsense/harvest/render.py#L196)) — a single string arg reaches `file:///etc/passwd`, `http://169.254.169.254/...`, `http://localhost/...`. The README covers authorization (legal/politeness) but never flags the SSRF/file-read surface for the "user pastes a URL" posture it promotes. Add a security note. *(safety)*
- **Config "single source of truth" is overstated** ([README.md:126](README.md#L126); [palette/roles.py:52-101](src/colorsense/palette/roles.py#L52)) — usage-side role-scoring weights and the component→role affinity map are hardcoded module constants in `roles.py`, not in the YAML. Scope the claim to the token vocabulary + classifier weights. *(docs)*
- **Two integration goldens are nearly hollow** ([tests/golden/legacy_site.json](tests/golden/legacy_site.json), [cards_site.json](tests/golden/cards_site.json)) — pin only `fit_score` (±0.05); `tokens:{}`, `status_colors:[]` can't regress. The real coverage is in the inline invariants. Add structural fields to the digest or document the intent. *(tests)*
- **Screenshot/states harvest under-tested** ([harvest/screenshot.py](src/colorsense/harvest/screenshot.py) 62%, [harvest/states.py](src/colorsense/harvest/states.py) 79% even *with* browser tests) — the most environment-sensitive code, with untested error paths. Add synthetic-PIL unit tests + one harvest failure-path test. *(tests)*
- **`test_value_objects_are_frozen` swallows any exception** ([tests/test_models.py:82-89](tests/test_models.py#L82)) — `try/except Exception/else: raise` asserts "something raised," not "frozen." Use `pytest.raises(ValidationError)`. *(tests)*

---

## Nits

- `Development Status :: 4 - Beta` for a first 0.1.0 — typically `3 - Alpha` ([pyproject.toml:12](pyproject.toml#L12)).
- `pipeline.py` docstring twice calls the result a "frozen `AnalysisResult`," but it's intentionally mutable (contradicts [models.py:9](src/colorsense/models.py#L9)). Change "frozen" → "typed" ([pipeline.py:8-9](src/colorsense/pipeline.py#L8)).
- Unstable tie-breaks in `_third_party_colors` `max(mix, ...)` and screenshot-bin sort ([pipeline.py:191,209](src/colorsense/pipeline.py#L191)) — deterministic today only via upstream insertion order; add explicit `.value`/hex tie-breaks to harden against future refactors.
- `_near_identical` collapse is asymmetric ([pipeline.py:189-199](src/colorsense/pipeline.py#L189)) — could drop a genuinely different dark theme whose top-N happens to contain the light theme's dominant colors. Make symmetric or document.
- Aggregate models have no `validate_assignment`; `result.tokens.append("junk")` succeeds ([models.py:255](src/colorsense/models.py#L255)). Consistent with the "mutable for assembly" note; add a one-line doc caveat.
- No in-flight de-dup for concurrent identical cache keys ([net/politeness.py:196-212](src/colorsense/net/politeness.py#L196)) — `analyze` never triggers it; latent inefficiency only.
- Dev group lacks `pytest-cov` / `pytest-randomly`; no coverage floor or order randomization ([pyproject.toml](pyproject.toml)).
- README regen command shows the test path ([README.md:157](README.md#L157)) while the test docstring omits it — both work; trivial inconsistency.
- Verify the GitHub repo slug `cassidyhhaas/colorsense` matches the PyPI Trusted Publisher config, or the first publish will be rejected.

---

## Verification log

- `uv build` → wheel + sdist; `unzip -l` confirms `palette_config.yaml` + `py.typed` in the **wheel**; fresh-venv `pip install` + import + `load_default_config()` from an unrelated dir → OK.
- `uv run pytest -q` → **102 passed**. `-m 'not browser'` → 88 passed / 14 deselected. `-m browser` → 14 passed (Chromium present). Suite stable across 3 runs.
- `uv run ruff check .` → clean. `uv run ruff format --check .` → 33 files clean. `uv run mypy src` (strict) → **no issues, 21 files**.
- Coverage: 78% browserless, 92% with browser tests.
- Orchestrator re-confirmed M1 (grep: no matches) and M4 (robots `can_fetch` behavior) directly.

*No project code was modified during this review.*
