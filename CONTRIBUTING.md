# Contributing to colorsense

Thanks for your interest in contributing! Bug reports, feature requests, and pull requests
are all welcome via [GitHub Issues](https://github.com/cassidyhhaas/colorsense/issues) and
pull requests.

## Development setup

The project uses [uv](https://docs.astral.sh/uv/) for environment and dependency
management, and requires Python 3.12+:

```bash
git clone https://github.com/cassidyhhaas/colorsense
cd colorsense
uv sync --group examples
uv run playwright install chromium
```

`uv sync` creates the virtualenv and installs the package plus the dev dependency group
(ruff, mypy, pytest, pre-commit, …) pinned by `uv.lock`; `--group examples` adds the
FastAPI/uvicorn dependencies of `examples/`, which the mypy gate type-checks (without
them, `uv run mypy` and the pre-commit mypy hook fail on the examples imports). The Playwright Chromium download
is needed for the browser-marked tests; on Linux, `uv run playwright install --with-deps
chromium` also pulls the OS libraries Chromium needs.

### Pre-commit hooks (recommended)

The hooks mirror the CI gates exactly (they invoke ruff/mypy through `uv run`, so they use
the locked versions — no drift between the hook and CI):

```bash
uv run pre-commit install              # ruff + format + mypy on every commit
uv run pre-commit install -t pre-push  # browserless tests on every push
```

## Checks

CI runs three required jobs — build, lint, and test — aggregated into a single `ci` status
check. Locally, the same gates are:

```bash
uv run ruff check .            # lint
uv run ruff format --check .   # formatting
uv run mypy                    # strict type checking (src + examples)
uv run pytest                  # full test suite
```

Mypy runs in `strict` mode and the code is fully typed (`py.typed` ships in the wheel) —
new code should keep it that way.

## Tests

The suite is **network-free**: live-page work renders saved fixture HTML under
`tests/fixtures/` served via `file://` (the test policy opts in with
`allow_file_urls=True`). Tests that launch a real Chromium are marked `browser`:

```bash
uv run pytest -m "not browser"   # fast, browserless subset (the pre-push hook)
uv run pytest -m browser         # render/harvest + end-to-end tests
```

CI runs the browserless subset first for fast failure, then the browser subset, and
enforces a **90% coverage floor** across the two runs combined — please add tests alongside
behavior changes.

### Golden snapshots

Integration tests in [`tests/test_integration_sites.py`](tests/test_integration_sites.py)
pin golden snapshots of the analysis output. If your change intentionally alters analysis
results, regenerate them and include the updated snapshots in your PR:

```bash
UPDATE_GOLDEN=1 uv run pytest tests/test_integration_sites.py
```

Unexplained snapshot churn is the most common review question — call out *why* the goldens
changed in your PR description.

## Pull requests

- Keep PRs focused; separate refactors from behavior changes where practical.
- Make sure `ruff`, `mypy`, and the full `pytest` suite pass locally before pushing.
- Update the documentation (`README.md`, `docs/`, `SECURITY.md`) when behavior or public
  API changes, and add a line to `CHANGELOG.md` under *Unreleased*. The `docs/` pages are
  also published as a [documentation site](https://cassidyhhaas.github.io/colorsense/)
  (MkDocs Material); preview it locally with `uv sync --group docs` and
  `uv run mkdocs serve`, and keep `uv run mkdocs build --strict` passing (CI checks it).
- The public API surface is `colorsense.__init__` — anything not exported there is
  internal and free to change; be deliberate about adding new exports.

## Security issues

Please do not open public issues for exploitable vulnerabilities — see
[SECURITY.md](SECURITY.md).
