# Usage guide

This guide covers `analyze`'s options, the structure of the result it returns, the errors
it raises, and the fetch policy. For installation and a minimal example, see the
[README](../README.md); for design-token auditing and tuning the classifier, see the
[advanced guide](advanced.md).

## Calling `analyze`

```python
import asyncio
from colorsense import analyze, LIGHT_AND_DARK, PolitenessPolicy, Viewport

result = asyncio.run(
    analyze(
        "https://example.com",
        viewport=Viewport(width=1440, height=900, device_scale_factor=2.0),
        themes=LIGHT_AND_DARK,                          # default is light only
        politeness=PolitenessPolicy(min_interval=2.0),  # see "Fetch policy" below
        config_path="my_palette_config.yaml",           # see the advanced guide
        max_total_seconds=60.0,                         # overall deadline; default: none
        browser_args=("--js-flags=--max-old-space-size=512",),  # extra Chromium flags
    )
)
```

`analyze` is async-native: the requested themes render concurrently in one shared headless
Chromium, and CPU-bound work runs in worker threads so the event loop stays responsive.
Inside an async application (e.g. a FastAPI `async def` endpoint), `await analyze(url)`
directly — no threadpool hop required.

### Themes

By default `analyze` renders **light mode only** — most sites have no dark mode, and a
second theme roughly doubles the render cost. Pass `themes=LIGHT_AND_DARK` (equivalently
`themes=(Theme.light, Theme.dark)`) to also analyze dark mode. Sites that ignore
`prefers-color-scheme` (near-identical light/dark renders) collapse to a single reported
theme; `result.metadata` records when that happened.

The first theme in the tuple is "primary" and supplies the top-level `tokens`,
`divergence`, and `fit_score` fields.

### Overall deadline

`max_total_seconds` bounds the **entire call** — every theme render plus the CPU
classification — via `asyncio.timeout`. On expiry, in-flight renders are cancelled, the
shared browser is closed, and `AnalysisTimeoutError` is raised (a `TimeoutError` subclass
carrying the URL and budget). There is no deadline by default; set one wherever a stalling
page must not stall you (see [SECURITY.md](../SECURITY.md) §2). Must be positive when set.

### Browser launch arguments

`browser_args` is a tuple of extra command-line arguments appended to the library's own
launch arguments and passed **verbatim** to the Chromium launch; every render of the call
launches with them (all themes share one browser). The canonical use case caps each
renderer process's V8 heap:

```python
result = await analyze(url, browser_args=("--js-flags=--max-old-space-size=512",))
```

This bounds the **JS heap only**, not total renderer memory — hard per-render memory/CPU
caps are the container/cgroup layer's job (see [SECURITY.md](../SECURITY.md) §2). The
library does not validate the flags themselves; non-string entries (or a bare string
instead of a tuple) raise `TypeError` before any render.

### Viewport

The default viewport is 1280×800 at 1× scale. A custom `Viewport` (e.g. mobile dimensions)
captures a different layout, which can yield a different palette.

## Command line

The package installs a `colorsense` command — a convenience wrapper around `analyze` for
quick evaluation. The typed API is the contract: the human-readable output is **not
stable** across releases, while `--json` emits the full `AnalysisResult` schema
(`model_dump_json`) and follows the library's compatibility story.

```bash
colorsense https://example.com
colorsense https://a.example https://b.example --dark --json
```

URLs are analyzed sequentially through one shared `PolitenessPolicy` (paced per host,
rendered pages cached). Exit status: 0 when every URL succeeded, 1 when any failed
(the error goes to stderr and remaining URLs are still processed), 2 on bad arguments.
stdout carries data only; warnings and errors go to stderr.

| Flag | Effect |
| --- | --- |
| `--dark` / `--no-dark` | Also render dark mode (`themes=LIGHT_AND_DARK`). Default: light only. |
| `--viewport WxH` | Render viewport (default `1280x800`). |
| `--scale FLOAT` | Device scale factor (default `1.0`). |
| `--config PATH` | Palette config YAML overriding the bundled default (`config_path`). |
| `--max-total-seconds FLOAT` | Overall deadline per URL; unset by default. |
| `--browser-arg TEXT` | Extra Chromium launch argument, passed verbatim (`browser_args`); repeatable. E.g. `--browser-arg='--js-flags=--max-old-space-size=512'` caps each renderer's V8 heap (JS heap only; see [SECURITY.md](../SECURITY.md) §2). |
| `--min-interval FLOAT` | Seconds between same-host fetches (default `1.0`). |
| `--user-agent TEXT` | Wire User-Agent. Default: a CLI-identifying UA (`colorsense-cli/<version> (+repo URL)`); pass your own to identify *your* application. |
| `--block-private-networks` | Install `block_private_networks()` as the policy's egress `request_filter`. |
| `--no-robots` | Disable the `robots.txt` check (and its `Crawl-delay` honoring). An explicit, accountable choice — only for sites you own or are authorized to crawl (see [SECURITY.md](../SECURITY.md) §3); the CLI warns on stderr when used. |
| `--json` | Emit the full `AnalysisResult` as JSON. stdout is always exactly one valid JSON document: one object for a single URL (`null` if it failed), an array of the successful results for multiple URLs (`[]` if all failed). |
| `--version` | Print the installed version and exit. |

The default (no `--json`) output prints, per theme, each role's best candidate — hex,
probability, area — plus the overall fit score, in the spirit of
[`examples/quickstart.py`](../examples/quickstart.py).

## The result

`analyze` returns an `AnalysisResult` — a frozen Pydantic model; `result.model_dump_json()`
round-trips. The fields most consumers use:

### `themes`

The payload: each rendered `Theme` mapped to its reconciled palette. Walk
`palette.roles.mapping[role]` — the mapping always contains every `PaletteRole`
(`primary`, `secondary`, `accent`, `neutral_light`, `neutral_dark`), with an empty tuple
when no candidate was detected. Each candidate carries:

- **`color`** — a `Color`: an sRGB `hex` string plus cached **OKLCH** coordinates
  (`lightness`, `chroma`, `hue`) of the composited color, and the source `alpha`. `hex` is
  what you paint with; the OKLCH coordinates make it easy to derive your own theme-matched
  colors — sort by perceptual lightness, build accessible tints/shades, or compute
  contrast — without re-parsing the hex.
- **`probability`** — confidence this color fills the role; candidates within a role rank
  by it, so `candidates[0]` is the best pick.
- **`area`** — the fraction of page area the color covers, i.e. its 60/30/10 dominance.

### `fit_score`

How well the measured palette matches the canonical 60/30/10 split, in `[0, 1]`. A quick
quality signal for the analysis as a whole.

### `status_colors`

Success/error/warning colors detected and deliberately **kept out** of the palette, so a
red error banner doesn't masquerade as a brand accent.

### `tokens` / `divergence`

The declared design tokens (CSS custom properties) with inferred semantic roles, and
declared-vs-rendered discrepancies. See the
[advanced guide](advanced.md#design-token-auditing).

### `metadata`

A typed `RunMetadata`: which themes were requested versus actually analyzed, whether the
run collapsed to a single theme, and the fetch policy in effect. Useful for logging and for
detecting the single-theme collapse.

## Errors

- **`RenderError`** — the page failed to render or navigate.
- **`RobotsDisallowedError`** — the target's `robots.txt` disallows the fetch and the
  active policy respects it.
- **`UnsupportedSchemeError`** — the URL scheme is not fetchable under the policy: only
  `http(s)` by default; `file://` requires `PolitenessPolicy(allow_file_urls=True)`, and
  every other scheme is always rejected.
- **`AnalysisTimeoutError`** — `max_total_seconds` was set and expired before the analysis
  finished. Subclasses the builtin `TimeoutError`, so `except TimeoutError` catches it;
  carries `url` and `max_total_seconds`.

## Fetch policy

colorsense fetches and renders third-party pages. The library provides **mechanism, not
policy** — whether a fetch is authorized is the consumer's decision, made *before* calling
`analyze`. `PolitenessPolicy` provides the controls:

- **`user_agent`** — an identifiable User-Agent, sent on the wire for both the
  `robots.txt` GET and the page render itself.
- **`respect_robots`** — on by default: a `robots.txt` disallow raises
  `RobotsDisallowedError`. Note that the check **fails open** (an unreachable `robots.txt`
  permits the fetch) — it is a politeness signal, not an authorization control.
- **`min_interval`** — per-host rate limit, in seconds between same-host fetches. When the
  site declares a `robots.txt` `Crawl-delay`, the effective interval is the larger of the
  two, with the crawl delay capped at **`max_crawl_delay`** (30 s by default) so a hostile
  directive cannot stall a pipeline.
- **`allow_file_urls`** — off by default; `file://` reads arbitrary local files, so it is
  an explicit opt-in (the test suite opts in to render its local fixtures).
- **`request_filter`** — an optional predicate over **every URL the browser requests**
  while rendering (the navigation *and* the page's own sub-resources), aborting any request
  it rejects. It may be **synchronous or asynchronous** (the `RequestFilter` type alias):
  a sync predicate is invoked inline on the event loop's request path and must not block
  (cheap string checks only); an async predicate is awaited, free to do slow work off the
  loop. The same filter also gates the policy's own server-side `robots.txt` GET:
  the robots URL and every redirect hop it follows are vetted before being requested, and
  a rejected hop aborts the robots fetch (which then fails open as "no rules") while the
  navigation itself stays filtered browser-side. Raising — sync or async — fails closed.
  This is the in-library SSRF mechanism; see [SECURITY.md](../SECURITY.md).
  `block_private_networks()` builds one (async) for the common case: it resolves each
  hostname and rejects URLs resolving to private/loopback/link-local (metadata)/CGNAT/other
  non-public addresses, failing closed, with an optional narrowing `allowed_hosts`
  allowlist. It does not fully defeat DNS rebinding (Chromium resolves hostnames
  independently); its DNS lookups run off the event loop on a worker thread (cached per
  hostname with a TTL; concurrent misses for one host share a single lookup) — network
  isolation remains the primary control.
- **`max_concurrent_renders`** — optional cap on simultaneous renders through the policy
  (unbounded by default — set it on servers; see [SECURITY.md](../SECURITY.md) §2). Cache
  hits and coalesced duplicate fetches never count against the cap, and a fetch waiting out
  the rate limiter holds no slot. Share one policy instance to make the cap process-wide.
- **`max_cache_entries`** — bound on the URL→render LRU cache (256 by default).

Choose your posture by where colorsense runs:

- **Server-side / batch** (you analyze sites you operate or are authorized to crawl): keep
  `respect_robots=True`, set a conservative `min_interval`, and use an identifiable
  User-Agent so site operators can contact you.

  ```python
  policy = PolitenessPolicy(
      user_agent="MyApp/1.0 (+https://myapp.example/bot)",
      min_interval=2.0,
  )
  ```

- **Embedded / on-demand** (a user pastes a URL into your product to theme a widget): you
  may legitimately analyze a page the user is entitled to view. You still own the decision
  to fetch — gate it on your own authorization, terms of service, and rate limits *before*
  calling `analyze`. Disabling `respect_robots` is an explicit, accountable choice, not a
  default.

If untrusted or user-supplied URLs can reach `analyze` from a server, you are exposed to
SSRF and resource-exhaustion risks beyond what the policy controls. The threat model and
the controls you must enforce are documented in [SECURITY.md](../SECURITY.md) — read it
before exposing `analyze` to untrusted input.
