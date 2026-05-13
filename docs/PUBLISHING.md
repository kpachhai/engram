# Publishing engram-mcp to PyPI

**Audience:** the maintainer publishing a new release of `engram-mcp`. Skip this if you're a user — `pip install engram-mcp` is all you need.

## Versioning policy

`engram-mcp` follows [Semantic Versioning](https://semver.org/):

- **MAJOR** (v2.0.0): breaking changes to the MCP wire format or storage schema. Per the API stability commitment in `02-TECHNICAL_DESIGN.md`, the v1.x line is stable for the lifetime of v1; only non-breaking additions ship as MINOR / PATCH.
- **MINOR** (v0.X.0 → v0.(X+1).0): new MCP tools, new CLI subcommands, new doctor codes, new optional fields, new opt-in features. Backwards compatible.
- **PATCH** (v0.X.Y → v0.X.(Y+1)): bug fixes, documentation improvements, security fixes that don't change behavior, dependency bumps.

Pre-1.0 (v0.X.Y) is the current series; the v0 → v1 cut happens after the operational dogfood period (14-day single-user, 7-day multi-machine, 7-day team-vault) confirms no breaking changes are needed.

## Prerequisites

You need:

- A PyPI account with publish access to the `engram-mcp` project ([pypi.org/project/engram-mcp/](https://pypi.org/project/engram-mcp/)).
- A TestPyPI account with publish access to the same project name on [test.pypi.org](https://test.pypi.org/) (for dry-run publishes).
- API tokens for both, stored in `~/.pypirc` (mode `0600`):

  ```ini
  [distutils]
  index-servers =
      pypi
      testpypi

  [pypi]
  username = __token__
  password = pypi-<your-token>

  [testpypi]
  repository = https://test.pypi.org/legacy/
  username = __token__
  password = pypi-<your-testpypi-token>
  ```

- `uv` installed locally (`uv build` is the canonical build command).
- A clean working tree on `main` with all desired changes committed + signed.
- CI green on the commit you're about to release.

## Release checklist

### 1. Decide the version bump

Look at the changes since the last release:

- Did the MCP wire format change in a backwards-incompatible way? → MAJOR.
- Did you add a new tool / CLI subcommand / doctor code / optional field? → MINOR.
- Bug fixes / docs only? → PATCH.

Historical releases:

- **v0.4.0** (MINOR) — first public release. Team Brain feature set
  was additive: new `team-write` role, GPG-bound sender attribution,
  server-side `pre-receive` hook, `CaptureInputMetadata.vault` field,
  and a seven-tool MCP surface.
- **v0.5.0** (MINOR) — daemon mode. ``engram serve`` runs as a thin
  proxy that auto-spawns a per-vault daemon over UDS; N concurrent
  Claude Code sessions can attach to the same vault. New
  ``engram daemon {start,stop,status,logs}`` subcommand group. The
  MCP wire format is unchanged so existing client configurations
  need no edits.

### 2. Update version + CHANGELOG

```bash
cd ~/repos/github.com/kpachhai/engram

# Edit pyproject.toml AND src/engram/__init__.py to bump the version.
# Example for v0.5.0:
sed -i '' 's/^version = "0\.4\.0"$/version = "0.5.0"/' pyproject.toml
sed -i '' 's/^__version__ = "0\.4\.0"$/__version__ = "0.5.0"/' src/engram/__init__.py

# Relock so uv.lock matches the new project version:
uv sync

# In CHANGELOG.md, rename the [Unreleased] section header to:
#   ## [0.5.0] - 2026-05-13
# And add a fresh [Unreleased] section above it.
```

Verify both version files agree:

```bash
grep '^version' pyproject.toml             # confirms new version
grep '^__version__' src/engram/__init__.py  # confirms same value
head -20 CHANGELOG.md                      # confirms new section header
```

Both `engram --version` and `python -m engram --version` must report
the same string after the bump — the CI ``console-script-smoke`` job
enforces this.

### 3. Run the full validation gate

Releases ship green or they don't ship.

```bash
uv sync --all-extras --dev
uv run ruff format
uv run ruff check
uv run mypy
uv run pytest --cov=src --cov-fail-under=80
```

All four must pass. If any fails, fix it before continuing.

### 4. Build the wheel

```bash
uv build
ls -la dist/                            # expect engram_mcp-0.5.0-py3-none-any.whl + .tar.gz
```

The build artifacts go in `dist/`. Inspect the wheel to make sure nothing surprising shipped:

```bash
unzip -l dist/engram_mcp-0.5.0-py3-none-any.whl | head -30
```

Expected: `src/engram/**/*.py` only. No tests, no docs, no `.indexes/`, no random caches. If anything unexpected is in the wheel, audit `pyproject.toml` `[tool.hatch.build.targets.wheel]` (or whatever build backend is configured) and re-build.

### 5. Smoke-test the wheel in a clean venv

This catches packaging bugs that don't surface in the dev workflow.

```bash
python -m venv /tmp/engram-publish-smoke
/tmp/engram-publish-smoke/bin/pip install dist/engram_mcp-0.5.0-py3-none-any.whl
/tmp/engram-publish-smoke/bin/engram --version       # confirms install
/tmp/engram-publish-smoke/bin/python -m engram --version  # confirms `python -m engram` works (daemon spawn dance relies on this)
/tmp/engram-publish-smoke/bin/engram doctor --help   # confirms CLI wires up
/tmp/engram-publish-smoke/bin/engram daemon --help   # confirms daemon subcommand group wires up
mkdir -p /tmp/engram-test-vault
/tmp/engram-publish-smoke/bin/engram init /tmp/engram-test-vault
/tmp/engram-publish-smoke/bin/engram doctor --config /tmp/engram-test-vault/engram.config.yaml
rm -rf /tmp/engram-publish-smoke /tmp/engram-test-vault
```

If any step fails, do NOT publish. Fix the issue, re-build, re-test.

### 6. Publish to TestPyPI first

```bash
uv publish --index testpypi
# Or with twine if you prefer:
# python -m twine upload --repository testpypi dist/engram_mcp-0.4.0*
```

Wait ~30 seconds for TestPyPI to index the release. Then test it:

```bash
python -m venv /tmp/engram-testpypi-smoke
/tmp/engram-testpypi-smoke/bin/pip install \
  --index-url https://test.pypi.org/simple/ \
  --extra-index-url https://pypi.org/simple/ \
  engram-mcp==0.4.0
/tmp/engram-testpypi-smoke/bin/engram --version
/tmp/engram-testpypi-smoke/bin/engram doctor --help
rm -rf /tmp/engram-testpypi-smoke
```

The `--extra-index-url https://pypi.org/simple/` is required because TestPyPI doesn't host all transitive dependencies; pip needs to fall through to real PyPI for `fastembed`, `fastmcp`, etc.

If the TestPyPI install succeeds and the binary works, proceed to step 7.

### 7. Publish to real PyPI

```bash
uv publish
# Or:
# python -m twine upload dist/engram_mcp-0.4.0*
```

Wait ~30 seconds. Verify the release page lands:

```bash
curl -s https://pypi.org/pypi/engram-mcp/json | python -c "import json, sys; d=json.load(sys.stdin); print(d['info']['version'])"
# Expected: 0.4.0
```

### 8. Tag the release in git

```bash
cd ~/repos/github.com/kpachhai/engram
git tag -s v0.4.0 -m "Release v0.4.0 - Team Brain"
git push origin v0.4.0
```

The `-s` flag GPG-signs the tag (per the maintainer's git commit policy). Push the tag separately from `git push` so it lands as a distinct event in CI / GitHub.

### 9. Create a GitHub release

```bash
gh release create v0.4.0 \
  --title "v0.4.0 - Team Brain" \
  --notes-from-tag \
  dist/engram_mcp-0.5.0-py3-none-any.whl \
  dist/engram_mcp-0.4.0.tar.gz
```

Or use the GitHub web UI: Releases → Draft new release → pick the tag → paste the relevant CHANGELOG section as the release notes → attach the wheel + sdist as binary attachments.

### 10. Post-publish smoke

Wait 5 minutes (PyPI CDN propagation). Then run the canonical install flow on a clean machine (or fresh container):

```bash
docker run --rm -it python:3.11-slim bash
# Inside the container:
pip install engram-mcp
engram --version
engram doctor --help
```

If the canonical flow works, the release is live and the announcement can go out.

## Yanking a bad release

If a release ships with a critical bug:

```bash
# Yank it (mark as broken; pip refuses to install but existing installs continue working):
uv publish --yank "Critical bug; use 0.4.1+" engram-mcp==0.4.0

# Then publish a fix:
# (bump version in pyproject.toml to 0.4.1, fix bug, repeat the release checklist)
```

Yanking is preferred over deletion. Deletion permanently frees the version number and breaks reproducibility for any user who pinned `engram-mcp==0.4.0`. Yanking signals "this is broken; upgrade" without destroying history.

## Hash manifest refresh

If the release bumps the FastEmbed model (e.g. `BAAI/bge-small-en-v1.5` → a successor), refresh the SHA-256 manifest BEFORE publishing:

```bash
engram doctor --download-model --print-hashes > /tmp/new-hashes.txt
# Inspect /tmp/new-hashes.txt; paste into src/engram/embedding/model_hashes.py.
# Commit + push the manifest update before tagging the release.
```

Shipping with stale hashes either fails on legitimate upgrades or (worse) silently disables verification.

## Pre-release checklist condensed

Save this for the next release:

- [ ] CI green on the commit being released
- [ ] `pyproject.toml` version bumped
- [ ] `CHANGELOG.md` `[Unreleased]` → `[<version>] - <date>` rename
- [ ] `uv run ruff format && uv run ruff check && uv run mypy` clean
- [ ] `uv run pytest --cov=src --cov-fail-under=80` clean
- [ ] `uv build` produces a `dist/engram_mcp-<version>-py3-none-any.whl` that contains only `src/engram/**/*.py`
- [ ] Clean-venv smoke test passes
- [ ] TestPyPI publish + smoke passes
- [ ] Real PyPI publish + smoke passes
- [ ] `git tag -s v<version>` + `git push origin v<version>`
- [ ] GitHub release created with wheel + sdist attached
- [ ] 5-minute post-publish canonical install smoke passes
- [ ] Release announcement (if you do those) sent

## See also

- `docs/QUICKSTART.md` — what users see when they install your release.
- `CHANGELOG.md` — release-note source of truth.
- `pyproject.toml` — version + entry-points + dependencies.
- `.github/workflows/ci.yml` — gating CI matrix.
