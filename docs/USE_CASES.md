# engram - Use Cases

What does engram actually solve? Five real scenarios from people who use AI assistants daily, with concrete examples.

## 1. The Solo Knowledge Worker

**Profile:** A developer, solutions architect, researcher, or any AI-native knowledge worker. Runs Claude Code (or another MCP-aware assistant) every day. Has been keeping a "friction log" in a single markdown file because nothing else worked. Now drowning in it.

**Pain:** AI conversations contain real learning — debugging breakthroughs, design decisions, library quirks, "I was confidently wrong about X" moments. None of it survives the next session unless you copy-paste into a notes app, and that notes app has no search worth using.

**How engram solves it:**

Tell your AI:

> "From now on, capture lessons, frictions, and decisions to engram as we go."

Now every breakthrough lands in `~/.local/share/engram/personal/thoughts/lesson/` as a markdown file. The next time you debug something similar, your AI calls `search_thoughts("the error I hit when migrating SQLite to Postgres")` and surfaces the prior capture.

**Concrete example:**

```
[Friction] Vercel deployment failed because Next.js 15 dropped support for the `getStaticPaths` API.
The error message was misleading — it said "Cannot find module" but the actual cause was the API
removal. Fix: migrate to `generateStaticParams`.
```

Six months later, you hit a similar Next.js migration and your AI surfaces this thought before you even start debugging. The accumulated context compounds.

**Why engram specifically:**

- Markdown files outlive every vendor.
- Local embeddings + sqlite-vec means search works offline.
- No SaaS subscription, no API key in your AI provider's billing.

## 2. The Multi-Machine Personal User

**Profile:** Same person as #1 but runs across multiple personal machines — laptop, desktop, travel laptop, occasionally a partner's shared device.

**Pain:** Cloud-hosted memory tools assume a single network identity. Local-first tools assume a single device. Neither serves the multi-personal-device case without manual sync gymnastics.

**How engram solves it:**

Treat your personal vault as a git repo. Sync via git push/pull to a shared remote (typically GitHub):

```bash
# On your laptop, initialize the vault as a git repo + push to a remote
cd ~/.local/share/engram/personal
git init -b main
git remote add origin git@github.com:you/your-personal-vault.git
git add . && git commit -S -s -m "initial" && git push -u origin main

# On your desktop, clone the same repo
engram clone-vault git@github.com:you/your-personal-vault.git ~/.local/share/engram/personal
```

Engram's sync coordinator runs `git pull` on serve startup and debounces commits + pushes after every capture. When you capture on your desktop, the laptop sees the new thought within a sync cycle.

**Concrete example:** You're debugging on the desktop at 11pm; you capture three lessons; you go to sleep. Tomorrow morning at the office on your laptop, those lessons are already searchable when you ask Claude Code about a related topic.

**Why engram specifically:**

- Git is already your sync transport. No cloud sync subscription.
- Conflict resolution is per-file: simultaneous edits to the same thought (rare) trigger a clean git merge UI.
- Your personal vault stays out of work IT's monitoring envelope.

## 3. The Privacy-Bounded Worker

**Profile:** Same person as #1 but works at a regulated employer (finance, healthcare, government, defense). The work laptop blocks external MCP servers and most cloud services. The personal laptop has no such restrictions.

**Pain:** Your accumulated AI memory is on the personal device, but you can't reach it from the work device. Everything captured on the work laptop dies when the session ends. You're maintaining two parallel mental models of "stuff I learned this week."

**How engram solves it:**

Two SEPARATE vaults — one personal, one work — on physically separate devices. Engram on the work laptop has only the work vault configured. The personal vault is not present on the work disk.

The privacy boundary is enforced by the **physical absence** of the personal vault data on the work disk, not by software filters that could be misconfigured.

```yaml
# ~/.config/engram/config.yaml on the WORK laptop
default_user: alice
vaults:
  - name: work
    path: ~/.local/share/engram/work
    role: primary
```

```yaml
# ~/.config/engram/config.yaml on PERSONAL machines
default_user: alice
vaults:
  - name: personal
    path: ~/.local/share/engram/personal
    role: primary
```

**Concrete example:** Your work laptop captures `[Decision] We picked Postgres for the new ingest pipeline because of strict ordering requirements`. Your personal laptop captures `[Lesson] llama.cpp's GGUF format is finicky on Apple Silicon under certain quantization configs`. Neither leaks into the other. If a personal thought is genuinely relevant at work (or vice versa), you copy the markdown manually — boundary crossings are deliberate, not accidental.

**Why engram specifically:**

- No network egress on the work laptop.
- No SaaS that could phone home.
- The IT-compliance question "where does the data live?" has a flat answer: this directory, end of story.

## 4. The Trust-Network Knowledge Sharer

**Profile:** Two friends, colleagues, or peers who want to share curated chunks of their AI memory. "You mentioned you have great notes on Hedera mirror node behavior — can you send me those?" Today this is a copy-paste job into Slack or Notion.

**Pain:** No clean "subscribe to a friend's notes" pattern. Everything ends up flattened into a chat thread that's impossible to search.

**How engram solves it:**

Bundle export and bundle import.

The sharer:

```bash
engram export \
  --output ~/share/hedera-notes.tar.gz \
  --portability portable
```

(Engram's exporter currently filters by portability tier only; per-prefix filtering at export is a candidate future feature. The receiver can inspect the bundle's `manifest.json` and filenames before importing if they want a narrower set.)

The receiver:

```bash
engram import \
  ~/share/hedera-notes.tar.gz \
  --vault alice-hedera-shared \
  --allow-read-only
```

Now the imported thoughts are mounted as a `read-only` vault alongside the receiver's primary vault. Searches span both, with vault attribution preserved. Captures still target the receiver's primary vault (the friend's vault is read-only).

**Concrete example:** Alice has spent six months learning Hedera mirror node quirks. Bob is starting on a similar project. Alice exports her `[Pattern]` and `[Decision]` thoughts, sends Bob a tarball, and Bob imports it. When Bob asks Claude Code about mirror node pagination, Claude searches both vaults; the answer surfaces alongside Alice's earlier capture, with the source clearly attributed.

**Why engram specifically:**

- The bundle is a tarball, not a database dump. Bob can read every file in any text editor before importing.
- Portability filtering at export time keeps Alice's `sensitive` and `block` thoughts out of the bundle.
- The receiver's primary vault stays clean; the friend-vault is mounted alongside, never merged.

## 5. The Small Team Tech Lead

**Profile:** Tech lead of a 5-15 person engineering team. Wants institutional memory: postmortems, architectural decisions, debugging breakthroughs, on-call learnings. Today this lives in a wiki nobody reads (because nobody updates wikis).

**Pain:** Wikis fail because the curation step is manual and the lift is too high. AI memory tools fail at team scale because they have no permission model and no concept of "team-shared vs personal."

**How engram solves it:**

A team vault is just a third role on the same primitive. Each team member has their personal vault PLUS the shared team vault. Captures into the team vault are GPG-attributed and policy-gated; captures into the personal vault stay personal.

The steward (typically the tech lead) bootstraps:

```bash
engram team-vault setup ~/team-vaults/postmortems \
  --remote git@github.com:your-org/team-postmortems.git
```

Each team member joins:

```bash
git clone git@github.com:your-org/team-postmortems.git ~/team-vaults/postmortems
# Add to ~/.config/engram/config.yaml under vaults: with role: team-write
```

A capture during a debugging session can target the team vault explicitly:

```
"Capture this to the team vault: [Postmortem] Last night's database failover took 47 minutes
because the secondary's WAL was 14GB behind. Root cause: the application opened a long-running
read transaction at 3am and the WAL couldn't truncate."
```

Every team member's engram now finds this thought when searching `database failover postmortem`. The thought's `captured_by` field carries the author's GPG fingerprint, so attribution is verifiable.

**Concrete example:** Six weeks later a junior engineer hits the same WAL bloat issue. They ask Claude Code about it. Claude searches the team vault and surfaces the postmortem. The junior engineer skips a 47-minute outage they would otherwise have repeated.

**Why engram specifically:**

- The team vault is just a git repo. Hosted anywhere your team already trusts (GitHub, GitLab, Forgejo, Gitea, GitHub Enterprise).
- A `pre-receive` hook enforces the team's policy at push time: prefix allowlist, sender attribution, force-push refusal, `.indexes/` containment.
- Personal and team contexts stay separate. Your personal `[Friction]` about a teammate doesn't accidentally land in the team vault.
- See [TEAM_BRAIN_GUIDE.md](TEAM_BRAIN_GUIDE.md) for the full setup walkthrough including hook install per platform.

## What engram is NOT for

Be honest with yourself before adopting:

- **You have under 50 captured thoughts and are just experimenting:** a single markdown file + grep is enough. Engram is overkill until you cross the search-needs-semantics threshold.
- **You want a polished GUI for browsing thoughts:** point Obsidian or any text editor at the same `thoughts/` directory engram serves. Engram does not provide a GUI; it's the headless backend.
- **You need real-time multi-user editing of the same thought:** engram is async-first via git. Notion or a Google Doc fits the live-collaboration use case better.
- **You need formal compliance (HIPAA / FedRAMP / SOC 2):** engram is local-first by design but is not certified. Wait for a vendor that has done the audit work, or accept the risk in a regulated context.
- **You're on Windows and won't use WSL:** engram tests on macOS and Linux. Windows works under WSL; native Windows is best-effort.

## See also

- [docs/QUICKSTART.md](QUICKSTART.md) — five-minute install + first capture.
- [docs/COMPARISONS.md](COMPARISONS.md) — engram vs Mem0, Letta, basic-memory, Open Brain, Obsidian + Smart Connections.
- [docs/ARCHITECTURE.md](ARCHITECTURE.md) — how the storage + sync + MCP layers fit together.
