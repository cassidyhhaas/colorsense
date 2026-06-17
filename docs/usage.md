# Usage guide

This guide covers `analyze`'s options, the structure of the result it returns, the errors
it raises, and the fetch policy. For installation and a minimal example, see the
[overview](index.md); for design-token auditing and tuning the classifier, see the
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
        include_tokens=True,                            # opt into the declared-token list
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
`themes=(Theme.LIGHT, Theme.DARK)`) to also analyze dark mode. Sites that ignore
`prefers-color-scheme` (near-identical light/dark renders) collapse to a single reported
theme; `result.metadata` records when that happened.

The first theme in the tuple is "primary": when light/dark renders are near-identical, it
is the one kept. Everything derived per theme (`colors`, `usage`, `divergence`, `tokens`)
lives on that theme's `ThemePalette` in `result.themes`.

### Overall deadline

`max_total_seconds` bounds the **entire call** — every theme render plus the CPU
classification — via `asyncio.timeout`. On expiry, in-flight renders are cancelled, the
shared browser is closed, and `AnalysisTimeoutError` is raised (a `TimeoutError` subclass
carrying the URL and budget). There is no deadline by default; set one wherever a stalling
page must not stall you (see [SECURITY.md](https://github.com/cassidyhhaas/colorsense/blob/main/SECURITY.md) §2). Must be positive when set.

### Browser launch arguments

`browser_args` is a tuple of extra command-line arguments appended to the library's own
launch arguments and passed **verbatim** to the Chromium launch; every render of the call
launches with them (all themes share one browser). The canonical use case caps each
renderer process's V8 heap:

```python
result = await analyze(url, browser_args=("--js-flags=--max-old-space-size=512",))
```

This bounds the **JS heap only**, not total renderer memory — hard per-render memory/CPU
caps are the container/cgroup layer's job (see [SECURITY.md](https://github.com/cassidyhhaas/colorsense/blob/main/SECURITY.md) §2). The
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
| `--browser-arg TEXT` | Extra Chromium launch argument, passed verbatim (`browser_args`); repeatable. E.g. `--browser-arg='--js-flags=--max-old-space-size=512'` caps each renderer's V8 heap (JS heap only; see [SECURITY.md](https://github.com/cassidyhhaas/colorsense/blob/main/SECURITY.md) §2). |
| `--min-interval FLOAT` | Seconds between same-host fetches (default `1.0`). |
| `--user-agent TEXT` | Wire User-Agent. Default: a CLI-identifying UA (`colorsense-cli/<version> (+repo URL)`); pass your own to identify *your* application. |
| `--tokens` | Include the declared design tokens per theme (`include_tokens=True`); the human output prints name, hex, and semantic role. |
| `--block-private-networks` | Install `block_private_networks()` as the policy's egress `request_filter`. |
| `--no-robots` | Disable the `robots.txt` check (and its `Crawl-delay` honoring). An explicit, accountable choice — only for sites you own or are authorized to crawl (see [SECURITY.md](https://github.com/cassidyhhaas/colorsense/blob/main/SECURITY.md) §3); the CLI warns on stderr when used. |
| `--json` | Emit the full `AnalysisResult` as JSON. stdout is always exactly one valid JSON document: one object for a single URL (`null` if it failed), an array of the successful results for multiple URLs (`[]` if all failed). |
| `--version` | Print the installed version and exit. |

The default (no `--json`) output prints, per theme, the color-keyed index first (each
color's prominence and the roles it appears in), then the role-keyed usage view (each
role's entries with hex, probability, and area), any divergence, and (under `--tokens`) the
declared tokens, in the spirit of
[`examples/quickstart.py`](https://github.com/cassidyhhaas/colorsense/blob/main/examples/quickstart.py).

## The result

`analyze` returns an `AnalysisResult` — a frozen Pydantic model; `result.model_dump_json()`
round-trips. The fields most consumers use:

### `themes`

The payload: each rendered `Theme` mapped to a `ThemePalette` carrying everything derived
for that theme.

#### `colors` — the canonical color-keyed index

How each measured color is used. `palette.colors` is a tuple of `ColorUsage`, sorted by
`prominence` descending (area-truth primary, vote-mass secondary, so dominant backgrounds
rank high while zero-area brand accents are not buried). Third-party-dominated colors are
excluded (they ride on `result.third_party_colors`). Each `ColorUsage` carries:

- **`color`** — a `Color` (sRGB `hex` plus cached OKLCH coordinates).
- **`prominence`** — the overall ranking signal in `[0, 1]`; the list is sorted by it.
- **`area`** — the raw screenshot area fraction the color covers.
- **`usages`** — a tuple of `Usage` slots, most-used first, each with the `role`
  ([`UsageRole`](#usage-the-role-keyed-projection)), its `property_family`
  (`background` / `text` / `border` — always `role.property_family`), this color's `weight`
  among its own usages (slots sum to ~1), and normalized `components` evidence.

```python
for color in result.themes[theme].colors:
    roles = ", ".join(f"{u.role}={u.weight:.2f}" for u in color.usages)
    print(color.color.hex, color.prominence, roles)
```

#### `usage` — the role-keyed projection

What colors paint each usage role, reconciled against the site's declared design tokens.
Walk `palette.usage.mapping[role]` — the mapping always contains every `UsageRole`
(`page`, `surface`, `banner`, `cta`, `action`, `text`, `link`, `border`), with an empty
tuple when nothing was detected. Every entry is backed by **measured** rendering evidence:
the reconciled view only ever re-weights colors that actually rendered in the role, so a
declared color with no measured match never appears as an entry (such intent can surface
through `divergence`, provided the declared color isn't perceptually matched by measured
usage in some other role), and `components` is never empty. Each `UsageEntry` carries:

- **`color`** — a `Color`: an sRGB `hex` string plus cached **OKLCH** coordinates
  (`lightness`, `chroma`, `hue`) of the composited color, and the source `alpha`. `hex` is
  what you paint with; the OKLCH coordinates make it easy to derive your own theme-matched
  colors — sort by perceptual lightness, build accessible tints/shades, or compute
  contrast — without re-parsing the hex.
- **`probability`** — the color's prominence within its role (entries of one role
  sum to ~1); entries rank by it, so `entries[0]` is the best pick.
- **`area`** — the raw fraction of page (screenshot) area the color covers, an auditable
  signal alongside the probability.
- **`components`** — normalized evidence: which `ComponentType`s contributed the color to
  this role (e.g. `{card_bg: 0.7, modal_bg: 0.3}`).

```python
from colorsense import UsageRole

for entry in result.themes[theme].usage.mapping[UsageRole.CTA]:
    print(entry.color.hex, entry.probability, entry.components)
```

#### `divergence`

Declared-vs-rendered discrepancies, keyed by `UsageRole`: high-intent tokens
**declared but unused** in the render, and prominent rendered colors **used but
undeclared**. See the [advanced guide](advanced.md#design-token-auditing).

#### `tokens` (opt-in)

The declared design tokens (CSS custom properties), as `DesignToken` (name, resolved
color, inferred semantic role) — only when `analyze(..., include_tokens=True)`. The field
distinguishes **`None`** (tokens were not requested — the default) from **`()`** (tokens
were requested but no usable color tokens were found: a page that declares no custom
properties and a page whose declarations are all non-color — e.g. `--spacing: 4px` — or
ignore-classified both yield `()`). Status tokens (success/error/warning) are kept
out of the palette views but appear here with `semantic_role=status`.

### `third_party_colors`

Colors dominated by third-party widgets (chat launchers, consent banners, …), kept out of
the usage and roles views and surfaced separately.

### `metadata`

A typed `RunMetadata`: which themes were requested versus actually analyzed
(`len(metadata.themes_analyzed) == 1` detects the single-theme collapse) and the fetch
policy in effect. Useful for logging.

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
- **`request_filter`** — an optional predicate over **every HTTP(S) URL the browser
  requests** while rendering (the navigation *and* the page's own sub-resources), aborting
  any request it rejects. The two paths route interception cannot see are closed outright
  rather than filtered: configuring a filter also refuses every WebSocket connection (the
  handshake never goes out), and service workers are always blocked at context creation.
  It may be **synchronous or asynchronous** (the `RequestFilter` type alias):
  a sync predicate is invoked inline on the event loop's request path and must not block
  (cheap string checks only); an async predicate is awaited, free to do slow work off the
  loop. The same filter also gates the policy's own server-side `robots.txt` GET:
  the robots URL and every redirect hop it follows are vetted before being requested, and
  a rejected hop aborts the robots fetch (which then fails open as "no rules") while the
  navigation itself stays filtered browser-side. Raising — sync or async — fails closed.
  This is the in-library SSRF mechanism; see [SECURITY.md](https://github.com/cassidyhhaas/colorsense/blob/main/SECURITY.md).
  `block_private_networks()` builds one (async) for the common case: it resolves each
  hostname and rejects URLs resolving to private/loopback/link-local (metadata)/CGNAT/other
  non-public addresses, failing closed, with an optional narrowing `allowed_hosts`
  allowlist. It does not fully defeat DNS rebinding (Chromium resolves hostnames
  independently); its DNS lookups run off the event loop on a small dedicated thread pool
  with a fail-closed per-lookup timeout (cached per hostname with a TTL; concurrent misses
  for one host share a single lookup; fan-out to distinct hosts beyond the pool size
  queues) — network isolation remains the primary control. Each predicate serves one event loop at a time:
  sequential reuse across loops (e.g. back-to-back `asyncio.run` calls) is supported —
  the predicate re-binds when idle and keeps its verdict cache — but *concurrent* use
  from multiple event loops raises `RuntimeError` (detected best-effort; it fails closed
  through `request_filter`). Create a separate predicate per event loop for that.
- **`max_concurrent_renders`** — optional cap on simultaneous renders through the policy
  (unbounded by default — set it on servers; see [SECURITY.md](https://github.com/cassidyhhaas/colorsense/blob/main/SECURITY.md) §2). Cache
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
the controls you must enforce are documented in [SECURITY.md](https://github.com/cassidyhhaas/colorsense/blob/main/SECURITY.md) — read it
before exposing `analyze` to untrusted input.
