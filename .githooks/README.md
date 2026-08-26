# `.githooks/`

Gate scripts for this repo.

`verify-gates.sh`, `revendor.sh`, `README.md` and `vendor.lock` are first-party:
written for this repo, changed here. Everything else is **vendored** - copied in
from the maintainer's personal developer-tooling scripts, which live outside this
repo and are not a dependency you install. They are committed here so that a
fresh clone has working gates with nothing to fetch and nothing to configure.

## What this means if you are contributing

You do not need the upstream, and you do not need to re-vendor anything. The
copies in this directory are what runs, `./.githooks/verify-gates.sh` checks
them, and CI runs the same check on every push.

If a gate is wrong - a false positive you cannot mark, a pattern that should
exist, a bug in a scanner - open an issue describing the behaviour rather than
editing the file. A vendored file changed here is a file that diverges from its
source, and neither side finds out.

## Do not edit the vendored files in place

Editing a vendored copy to satisfy a local lint creates silent upstream drift:
the repo copy and its source diverge, and neither side knows. This repo shipped
exactly that failure - the bundled `pii-scan.sh` fell three fixes behind its
source, and the stale copy let `git mv` move PII past the gate, treated an
invalid regex in the pattern file as "nothing matched", and read the working
tree instead of the index.

## Taking an upstream change

Point `revendor.sh` at a directory holding the current gate scripts. It copies
the vendored set, re-pins `vendor.lock`, and runs the verifier:

```sh
./.githooks/revendor.sh --source /path/to/gate/scripts
# or: ENGRAM_GATE_SOURCE_DIR=/path/to/gate/scripts ./.githooks/revendor.sh
```

It refuses rather than half-applying: a source directory that does not exist, or
that is missing any file in the vendored set, exits 2 having copied nothing.

## `vendor.lock`

`sha256  path`, one per line. **The hash is the identity; the path is metadata.**
Matching a vendored item by path or filename instead of by content lets a
first-party file that happens to share a name be silently exempted, and a
path-suffix match degrades into a name match as soon as the directory convention
and the recorded path share a shape.

## `verify-gates.sh`

Two independent checks, because neither alone is sufficient:

- **Integrity** - every locked file matches its recorded hash. A lockfile pinning
  zero entries fails rather than reporting success.
- **Detection** - each gate is run against planted input it must flag. A hash
  check cannot tell you that a correctly-vendored scanner still *works*, and a
  scan reporting "clean" is indistinguishable from a scan that did nothing. The
  planted fixtures are generated at runtime and never committed, so they cannot
  rot and the scanners have nothing to special-case.

Every gate call is wrapped in `timeout` and passes `</dev/null`. A gate that
hangs is failing, and is reported as `HUNG` rather than dying at the CI job
limit where it reads as an infrastructure flake.

The ratchet gets its own probe, because the ratchet - not the bare scanner -
is what CI runs for planning vocabulary. It plants a finding the probe baseline
does not accept and requires the ratchet to flag it.

It also reports whether the PII scan is running with its identity patterns
loaded. Those come from a machine-local file (`~/.config/devkit/identity.json`)
that only the maintainer has; on a fork and in CI the scan runs on structural
patterns alone - paths, keys, emails by shape - and says so rather than
reporting a full pass.

## Known gate gap (2026-08-25)

`planning-vocab-ratchet.sh` discards the scanner's exit status
(`bash "$SCAN" ... 2>/dev/null || true`), so a scanner that crashed or produced
nothing reports `no new findings (0 total, all baselined)` and exits 0 - the same
line and the same status as a clean tree. Its awk comparison has a second edge:
an empty baseline file makes it read the current findings as the baseline, so a
repo with no accepted debt is not checked at all.

Both are in a vendored file, so the fix is upstream and then a re-vendor; open an
issue rather than editing the copy here. Until then two first-party controls stand
in: `verify-gates.sh` plants an unbaselined finding and requires the ratchet to
flag it, and the CI ratchet step refuses a run that reports zero findings against
a baseline that pins some. Delete this section when the vendored copy fails on its
own scanner's status and on a vacuous comparison.
