# `.githooks/`

Gate scripts for this repo. Everything here except `verify-gates.sh`, `README.md`
and `vendor.lock` is **vendored** from the maintainer's dotfiles
(`~/.claude/scripts/`) so that a fresh clone has working gates with no external
dependency.

## Do not edit the vendored files in place

Editing a vendored copy to satisfy a local lint creates silent upstream drift:
the repo copy and its source diverge, and neither side knows. This repo shipped
exactly that failure - the bundled `pii-scan.sh` fell three fixes behind its
source, and the stale copy let `git mv` move PII past the gate, treated an
invalid regex in the pattern file as "nothing matched", and read the working
tree instead of the index.

To take an upstream change:

```sh
cp ~/.claude/scripts/pii-scan.sh ~/.claude/scripts/pii-patterns.conf \
   ~/.claude/scripts/planning-vocab-scan.sh \
   ~/.claude/scripts/planning-vocab-patterns.conf \
   ~/.claude/scripts/planning-vocab-ratchet.sh .githooks/
chmod +x .githooks/*.sh
shasum -a 256 .githooks/pii-scan.sh .githooks/pii-patterns.conf \
  .githooks/planning-vocab-scan.sh .githooks/planning-vocab-patterns.conf \
  .githooks/planning-vocab-ratchet.sh | awk '{print $1"  "$2}' > .githooks/vendor.lock
./.githooks/verify-gates.sh
```

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
