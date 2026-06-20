"""Environment-derived configuration for the example service.

Every knob here implements a SECURITY.md item (the §1 host allowlist and the §2 resource
bounds); the values are read from the environment once, at import time, and consumed by
``policy.py`` and ``routes.py``. Plain module-level constants on purpose — this is an
example, and the parsing fits in a screenful.

Environment knobs: ``COLORSENSE_ALLOWED_HOSTS`` (comma-separated exact hostnames; unset =
any public host), ``COLORSENSE_MAX_CONCURRENCY`` (default 2),
``COLORSENSE_DEADLINE_SECONDS`` (default 60), and ``COLORSENSE_BROWSER_ARGS``
(whitespace-separated, shell-quoted (shlex) extra Chromium launch args; default caps each
renderer's V8 heap at 512 MB).
"""

from __future__ import annotations

import os
import shlex


def allowed_hosts_from_env() -> frozenset[str] | None:
    """Read the optional comma-separated exact-hostname allowlist; ``None`` = no allowlist.

    Returns:
        The lowercased exact-hostname allowlist, or ``None`` when the env var is unset
        or empty (meaning any public host is allowed).

    """
    raw = os.environ.get("COLORSENSE_ALLOWED_HOSTS", "")
    hosts = frozenset(host.strip().lower() for host in raw.split(",") if host.strip())
    return hosts or None


ALLOWED_HOSTS = allowed_hosts_from_env()

# SECURITY.md §2: cap simultaneous renders. Each render is a full headless-browser page
# with JS execution — unbounded, the library will launch as many as asked. The cap lives
# on the shared policy in policy.py (max_concurrent_renders), so it bounds the whole
# process.
MAX_CONCURRENT_ANALYSES = int(os.environ.get("COLORSENSE_MAX_CONCURRENCY", "2"))

# SECURITY.md §2: an overall deadline per analyze call, over and above the library's own
# navigation timeout — a page can pass navigation and still stall in scripting. Passed as
# analyze(max_total_seconds=...); covers render-slot queueing too, so size it with the
# concurrency cap in mind.
ANALYZE_DEADLINE_SECONDS = float(os.environ.get("COLORSENSE_DEADLINE_SECONDS", "60"))

# SECURITY.md §2: cap each renderer process's V8 heap in-browser, passed verbatim to the
# Chromium launch via analyze(browser_args=...). This bounds the JS heap only — not total
# renderer memory — so it complements, never replaces, the container/cgroup memory limit
# (the enforceable cap; the library ships no in-process memory watchdog by design).
# Override with COLORSENSE_BROWSER_ARGS (whitespace-separated, with shell-style quoting
# via shlex; empty string = no extra args). Flags containing commas or spaces are
# expressible by quoting — e.g. --host-resolver-rules='MAP a 1.2.3.4, MAP b 5.6.7.8'
# stays one argument. Unbalanced quotes raise ValueError at import/startup: a loud
# failure for a misconfigured env var, deliberately preferred over silently mangled args.


def browser_args_from_env() -> tuple[str, ...]:
    """Extra Chromium launch args from the env, shlex-split (see comment above).

    Returns:
        The shlex-split extra Chromium launch args (defaulting to the 512 MB V8 heap cap).

    Raises:
        ValueError: If ``COLORSENSE_BROWSER_ARGS`` has unbalanced shell quoting.

    """
    raw = os.environ.get("COLORSENSE_BROWSER_ARGS", "--js-flags=--max-old-space-size=512")
    return tuple(shlex.split(raw))


BROWSER_ARGS = browser_args_from_env()
