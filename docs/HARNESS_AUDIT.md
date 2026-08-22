# Harness and gate audit

Audit of engram's enforcement surface against four external sources on agent/skill
harness design, plus Claude Code's primary documentation. Method: one deep reader per
source, then a second adversarial agent per source whose only job was to disprove the
first and read what it skipped. Every number below came from a command whose output was
kept; nothing here is estimated in-context.

The corrections are the most valuable part of this document. A single reader under a
"be thorough" prompt produces plausible detail that does not survive checking, and this
run produced corrections both to the sources and to the auditor's own first conclusions.

## Sources

| Source | What it is |
|---|---|
| agentskills.io/specification | Cross-vendor Agent Skills standard |
| aihero.dev/skills | Matt Pocock's skill-authoring doctrine (index plus linked articles) |
| software-factory (cloned) | A generate/evaluate agent build loop |
| cigar (cloned, private reference) | A context-governance and evidence runtime |
| code.claude.com/docs | Claude Code primary docs: skills, memory, hooks, context-window |

Both GitHub sources were cloned locally before reading. A working tree exposes hooks,
CI, tests and config that a rendered README omits.

## Corrections the verification pass produced

### Against the brief's own hypotheses

**1. REVERSED - `disable-model-invocation` and the description budget.** The brief
recorded this as an unverified claim whose mechanism "Claude Code's docs do not say".
The docs do say it. code.claude.com/docs/en/context-window, describing the startup
skill-description listing:

> "Skills with `disable-model-invocation: true` are not in this list. They stay
> completely out of context until you invoke them with `/name`. Unlike the rest of the
> startup content, this listing is not re-injected after `/compact`. Only skills you
> actually invoked get preserved."

Pocock's v1 changelog ("v1: 63% Token Reduction", 18 June 2026) attributes the saving
to exactly that flag. So the MECHANISM is documented and confirmed; only the 63%
MAGNITUDE is unverified here, and it should not be treated as a transferable rate - it
is one author's measurement of one skill set. A peer session measuring this
independently found the skill listing is itself budget-capped, with over-cap entries
silently degrading to name-only, which means the percentage depends entirely on where
the measured set sat relative to that cap.

**2. REFINED - `name` and the parent directory.** The brief states `name` must equal its
parent directory. Claude Code documents `name` as OPTIONAL: "Display name shown in skill
listings. Defaults to the directory name." The hard equality is a packaging/tooling
constraint, not a Claude Code loading rule - the same distinction the brief correctly
draws for the six-field frontmatter limit.

**3. CONFIRMED verbatim - the compaction cliff.** From the same page:

> | Invoked skill bodies | Re-injected, capped at 5,000 tokens per skill and 25,000 tokens total; oldest dropped first |

and

> "Truncation keeps the start of the file, so put the most important instructions near
> the top of `SKILL.md`."

Ordering decides what survives, exactly as the brief said.

**4. CONFIRMED - path-scoped rules do not survive compaction.**

> | Rules with `paths:` frontmatter | Lost until a matching file is read again |
> "If a rule must persist across compaction, drop the `paths:` frontmatter or move it to
> the project-root CLAUDE.md."

Project-root CLAUDE.md and unscoped rules are "Re-injected from disk".

### Against the sources themselves

**5. software-factory's `skills-lock.json` has no verifier.** It records
`{source, sourceType, skillPath, computedHash}` per skill - identity by hash, path as
metadata, which is the right schema. But `git grep 'skills-lock\|computedHash'` across
the whole tree returns only the lockfile's own two lines. There is no CI (`no .github`
directory), no npm script, and no code that reads it. It is a lockfile nothing checks.
The pattern to copy is the schema; the verification step has to be built.

**6. software-factory's build loop stops on a model judgment, not a mechanical gate.**
The Evaluator agent writes `findings.json` with `"verdict": "PASS" | "FAIL"`. It is
instructed to "Verify empirically: run the code, hit the app URLs listed in the run
parameters, check real output. Never pass on a read-through alone" - but nothing
enforces that it did. The mechanical backstop is a hard `MAX_PASSES` cap. Transferable
parts: the hard iteration cap, per-pass directories that are never overwritten, stable
finding ids reused across passes as identities, and resume state rebuilt from disk.

**7. A peer session's cited precedent did not survive checking.** A neighbouring repo's
doc-consistency script was offered as the in-stack content-hash precedent. It is not: it
scrapes an upstream git commit SHA out of a README and re-fetches that commit over the
network. It answers "is the upstream commit still the one I reasoned about", not "has my
local vendored copy been modified". It is a good STALENESS precedent - it fails when the
pin is over ~60 days old unless a documented freeze is present - and not an INTEGRITY
one.

### Against the auditor's own first conclusions

**8. "Empty pattern file makes the scanner exit 0" was right about the code and wrong
about the behaviour.** On this machine `#!/usr/bin/env bash` resolves to GNU bash 3.2.57,
which errors on `${patterns[*]}` under `set -u` for an empty array, so it crashes at
line 59 with rc=1 before reaching the `exit 0`. On Linux CI (bash 5) it reaches the guard
and exits 0. The real finding is sharper than the original: the gate is
PLATFORM-DIVERGENT, failing closed on macOS and open on CI.

**9. A "jq absent" probe measured nothing.** `/usr/bin/jq` ships with macOS 15, so
stripping `/usr/local/bin` from PATH does not remove jq. The faithful test flips only the
predicate under test (`command -v jq` -> `false`) on a copy.

**10. Six probe results were harness artifacts, not findings.** zsh `noclobber` made a
`2>` redirect fail inside a probe function, so the commands never executed and the table
reported stale output from a previous iteration. Every row read rc=1. Only a known-good
baseline case in the same table exposed it. Always include a case whose expected result
you already know; it is the only part of a harness that can report that the harness is
broken.

**11. A first pass of the vendored-scanner audit missed that the mitigations already
existed upstream.** The repo copy and its canonical source had diverged, and several
"defects" were already fixed in the canonical version. Diffing before reporting turned
seven independent defect write-ups into one finding: the vendored copy is stale.

**12. The audit read a moving tree, and said so.** Implementation began before the
last verification agent finished, so later hunters saw commits the earlier ones did
not, and the pooled input contradicted itself on whether CI ran any gate. The synthesis
pass caught this, re-derived every line number against HEAD, and flagged that quoted
offsets predating the `gates` job were off by 9-11 lines. Recorded because a reader
should not trust any file:line in a report that overlapped its own remediation - re-run
the check instead.

## What this changed in the repo

Six commits, each with its own mechanical verifier and each mutation-tested before
landing.

| Commit | Change | Verified by |
|---|---|---|
| `e6ac03c` | pre-commit wrapper fails closed | `chmod -x` the scanner: rc 0 -> 2 |
| `a5b6c78` | source comments state rules without spec labels | vocab scan of `src/`: 0 findings |
| `8fa6922` | `uv sync --locked` in CI | passes now; rc=1 on an injected dependency |
| `6071e1e` | CONTRIBUTING CI claim corrected | `grep bench .github/workflows/ci.yml` -> no match |
| `a56b296` | gate scripts re-vendored and hash-pinned | `verify-gates.sh`; hand-edit a vendored file -> rc=1 |
| `7b27fd7` | repo gates run in CI at all | planted PII in changed files -> job fails |
| `2de82f3` | two docs corrected to match shipped behaviour | `engram sync compact --help`; `engram reindex --help` |
| `8567237` | tests covering the gates | stub a scanner to `exit 0`, re-hash honestly -> test fails |
| `4aa452f` | CI deselects one network test instead of ignoring 22 | full suite 1608 passed, coverage 83.39% vs an 80% gate |
| `e483d20` | model-mismatch errors name a flag that exists | `engram reindex --help` lists no `--model` |

The vendored scanner had fallen three fixes behind its source. Measured, stale copy
versus current:

| Defect | stale copy | current |
|---|---|---|
| invalid regex in the pattern file | rc=0, scan reports clean for every file | rc=2 |
| PII in the index, clean working copy | rc=0, commits through | rc=1 |
| `git mv` rename (`--diff-filter=AM`) | rc=0, PII walks past | rc=1 |
| zero loaded patterns | rc=0 "clean" | rc=2 |
| unrecognised flag (a typo) | rc=0, scanned nothing | rc=2 |
| file argument, non-TTY stdin | hangs (rc=124) | rc=1 |

## Closed after the audit

Each was fixed in a later session, one commit per finding, and each fix was watched
failing before it was trusted: the guard was disabled, the test confirmed red, then
restored and confirmed green.

| Finding | Mechanical verifier |
|---|---|
| The consolidation LLM path did not escape the prompt frame | `tests/consolidate/test_llm.py::TestPromptFraming` + `tests/llm/test_prompt_framing.py`; restore the raw interpolation -> both fail |
| Portability was not re-verified at consolidation apply time | `tests/consolidate/test_apply.py::TestSafetyGuards::test_portability_retag_skips_proposal`; disable the comparison -> fails |
| `engram doctor` reported OK for LLM checks it never measured | `tests/test_doctor_cli_smoke.py` against the installed binary; drop the wiring call -> both tests fail |
| The tree carried PII-gate matches the changed-files CI scan cannot see | `tests/test_gates.py::test_the_whole_tracked_tree_passes_the_pii_scan`; plant a path in any tracked file -> fails |
| `.claude/settings.local.json` pre-approved `Bash(claude mcp *)` by prefix | narrowed to `list` + `get *`; that file is gitignored, so this was a local edit, not a commit |

The username class needed an owner decision and got one: genericize everywhere,
`docs/archive/` included. Test fixtures carry neutral values, docs carry
`your-username`, and the synthetic 40-hex fixtures that trip the GPG-fingerprint
pattern carry `pii-allow` markers rather than being rewritten. `LICENSE` keeps the
attribution line - it is the sanctioned exception - and is the one file the whole-tree
scan skips.

## What else the same finding turned up

The PII backlog was a symptom of a wider one: several things in the repo only worked on
the machine that wrote them. Each is now closed with a test that fails when the property
is broken.

| Finding | Mechanical verifier |
|---|---|
| `.githooks/README.md` told contributors to re-vendor by copying out of the maintainer's home directory | `tests/test_gates.py::test_githooks_readme_instructions_are_followable_by_anyone`; `.githooks/revendor.sh` takes a `--source` directory and refuses rather than half-applying |
| The PII gate silently ran without its identity patterns on any machine but one | `tests/test_gates.py::test_verify_gates_announces_degraded_identity_mode`, with the identity-present case as its control |
| Docs cited a gitignored spec directory and paths under the maintainer's home | `tests/docs/test_reachable_references.py`; `docs/archive/` and `CHANGELOG.md` are exempt as records |
| Docs named optional machine-local config as though every reader had it | `tests/docs/test_reachable_references.py::test_machine_local_files_are_described_as_optional` |

End-to-end check: a fresh `git clone` installed with `uv sync --locked` under an empty
`HOME` runs the suite and both gate scripts green, and `verify-gates.sh` reports the
degraded PII mode a contributor actually gets.

## Outstanding, not landed

Nothing. Every finding above is closed.

## Implementation plan

One task per finding. Every task names real paths and a mechanical verifier - a command
whose exit code decides pass or fail. Nothing is considered done on a reading.

### Ordering constraints (getting these wrong makes a gate lie)

1. **The source rewrites must precede the vocabulary baseline.** The ratchet freezes
   whatever it finds when the baseline is written. Writing it first would record the five
   source-comment violations as permanently accepted debt, and the gate would then pass
   forever on exactly the residue it exists to remove.
2. **The re-vendor and hash pin must precede the CI job and the gate test.** Both
   reference the vendored files and their hashes; landing them first pins a hash the
   re-vendor immediately invalidates.
3. **The re-vendor waits on the upstream fix.** The canonical scanner was repaired
   while this audit ran. Vendoring before that landed would have pinned a hash that went
   stale within the hour and inherited three defects about to be fixed.
4. **The CI gates job should precede the CONTRIBUTING correction.** The corrected wording
   claims CI runs the repo gates; that is only true once the job exists.

### Tasks

| Finding | Files | Mechanical verifier |
|---|---|---|
| Vendored scanner is a stale fork missing three fixed bypasses | `.githooks/pii-scan.sh`, `.githooks/pii-patterns.conf`, `.githooks/vendor.lock` | `./.githooks/verify-gates.sh` exits 0; hand-edit a vendored file without relocking -> rc=1 |
| pre-commit wrapper skips silently when the scanner is missing or non-executable | `.pre-commit-config.yaml` | `chmod -x` the scanner, run the wrapper -> rc=2, previously rc=0 |
| The repo gates never ran in CI at all | `.github/workflows/ci.yml` | `gates` job runs the verifier, the ratchet, and a changed-files scan; planted PII -> job fails |
| CI `uv sync` omits `--locked`, so a stale lockfile cannot fail the build | `.github/workflows/ci.yml` | `uv sync --all-extras --dev --locked --dry-run` -> rc=0; with an injected dependency -> rc=1 |
| Five planning-vocabulary residues in shipped source, two scanner false positives | `src/engram/cli/llm.py`, `src/engram/config/loader.py`, `src/engram/migration/open_brain.py`, `src/engram/daemon/server.py`, `src/engram/team/members.py`, `src/engram/team/server_hooks/pre_receive.py` | vocabulary scan of `src/` -> zero findings |
| No mechanical enforcement of the repo's own "no delivery-phase labels in source" rule | `.githooks/`, `.planning-vocab-baseline`, `.github/workflows/ci.yml` | `planning-vocab-ratchet.sh --check .` -> rc=0; add a phase label to a source comment -> rc=1 |
| Zero tests cover the repo's only content-safety gate | `tests/` | a test running `verify-gates.sh` passes; stub the scanner to `exit 0` -> test fails |
| CONTRIBUTING claims CI runs benchmarks and "the same gates"; it ran neither | `CONTRIBUTING.md` | `grep bench .github/workflows/ci.yml` -> no match, so the claim must not appear |

### Deliberately not done

- **No `.claude/rules/` directory.** Path-scoped rules are "Lost until a matching file is
  read again" after compaction, and the docs say outright that a rule which must persist
  belongs in the project-root CLAUDE.md. This repo's rule-worthy content is its pinned
  invariants, which are safety-critical and already there. A path-scoped mirror would put
  a safety rule in the one place that does not survive compaction. The one candidate that
  did fit - "this directory is vendored, do not edit in place" - is better served by the
  hash verifier, which catches the mistake deterministically instead of asking a model to
  remember not to make it.
- **No skills.** This repo ships none, so the six-field frontmatter rule, the
  1,536-character description cap, the 5,000/25,000 compaction budget and
  `disable-model-invocation` have no surface here. Reported as not-applicable with
  measurements rather than converted into invented findings.
