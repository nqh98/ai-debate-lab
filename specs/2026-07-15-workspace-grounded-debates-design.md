# Workspace-Grounded Debates — Design Spec

Status: proposed
Date: 2026-07-15
Predecessor: `specs/2026-07-15-synthesis-phase-design.md` (completed the merge
mechanism; this spec grounds what gets merged)

## Purpose

Let agents read and inspect the code they are debating about, as if each had
been launched from a shell inside the target repository — and let them run
verification commands (tests, linters) so claims are checked against reality
before they are asserted, critiqued, or voted on.

Today a debate about a repository is a debate about a *description* of a
repository: `cmd_new` inlines whatever `--context` files the human thought to
attach (`cli.py:40-46`), and no agent can look at anything else. The three
ready agents — `claude`, `codex`, `agy` — are agentic coding CLIs with their
own file tools; the lab just never points them at a checkout. This spec adds
the pointing and nothing an agent platform already does better itself.

Scope: a `--repo` flag on `debate new`, a per-debate disposable git worktree,
working-directory and argv plumbing in `CliAgent`, per-agent opt-in timeouts
replacing the flat mandatory one, a grounding preamble in prompts, and live
run status with stall alerts in the terminal and the viewer. No changes to
the phase sequence, voting rules, quorum, retry policy, or the transcript
event contract beyond two additive event/file types named below.

## Decisions taken (with their alternatives)

**Capability: read + run verification commands.** Not read-only (an agent
that cannot run `pytest` can still assert "this test passes"), not full
agentic editing (the debate's product is an answer, not a patch). Scratch
writes are absorbed by the disposable worktree and die with it.

**Access: CLI-backed agents only.** An API backend is a bare HTTP call with
no file tools. Building a tool-use loop over the provider APIs would be a
large new subsystem duplicating what the CLIs do natively; it is deferred
(§9). API-backed agents stay in the debate and are told plainly that they
cannot inspect the repo (§5), so their votes weigh cited evidence rather
than hallucinated file contents.

**Architecture: workspace passthrough.** The lab sets `cwd`, appends
user-configured sandbox flags, and gets out of the way. The rejected
alternatives — a lab-mediated tool protocol (perfect audit, huge build,
fights the CLIs' native tooling) and pre-baked evidence packs (static,
context-window-bound, no agent autonomy) — are recorded in §9.

**No proactive timeout cancellation.** Difficult problems legitimately need
hours. The lab never kills a running agent by default; ceilings are opt-in
per agent. Visibility replaces cancellation (§6), and the human is the
timeout of last resort: phases checkpoint only on completion
(`README.md:63-65`), so Ctrl-C plus re-`run` resumes with at worst one
phase's work redone.

## Motivating evidence

All three real debates in `debates/` were run without repo access, and the
gaps this spec closes are visible in them:

- `20260714-how-can-this-repository-be-improved-furt-2` halted: `claude` and
  `antigravity` both hit the flat 180s ceiling *in the propose phase of a
  repository-analysis question* — the exact workload this tool exists for —
  leaving one responder, below the 2-agent floor. The ceiling comes from
  `CliAgent`'s hardcoded default (`registry.py:29`, `cli_agent.py:13`).
- Every successful debate converged in round 1 with unanimous acceptance.
  Nothing forced a critic to check a claim; agreement was cheap because
  evidence was impossible.
- Consensus is measured by votes alone. An answer about code that no
  participant could read is "best" only by acclamation.

## §1 The `--repo` flag and the debate record

`debate new "<problem>" --repo PATH` marks a debate repo-grounded.
Validation at creation: `PATH` exists, is a directory, and
`git -C PATH rev-parse HEAD` succeeds. The debate records, in `state.json`
under a new top-level key:

```json
"workspace": {
  "source": "/abs/path/to/repo",
  "commit": "<full sha of HEAD at debate new>"
}
```

The commit is pinned at creation so every `file:line` citation in the
transcript is reproducible against one tree. Uncommitted changes in the
source repo are deliberately excluded; the README documents this. A debate
without `--repo` has no `workspace` key and behaves exactly as today.

## §2 Workspace lifecycle

Materialized lazily by `debate run`, not by `new` — a debate may be created
on one machine state and run later.

- **Create:** if `debates/<id>/workspace/` is absent, run
  `git -C <source> worktree add --detach <abs workspace path> <commit>`.
  Emit a `workspace_ready` transcript event carrying the commit sha. Failure
  (source repo moved/deleted, sha unreachable, `git` errors) halts the run
  with the existing `error` status and a message naming the source path —
  the same contract as the under-quorum halt.
- **Resume:** if the directory exists, reuse it as-is. Agents may have left
  scratch artifacts (`__pycache__`, build output); they are harmless in a
  disposable tree and recreating mid-debate would discard nothing of record.
- **Remove:** when a human decision lands (approve or reject), run
  `git -C <source> worktree remove --force <path>`; if the source repo is
  gone by then, fall back to deleting the directory and leaving the source's
  stale worktree bookkeeping for `git worktree prune`. Removal failure is a
  warning, never an error — the decision itself is already recorded.
- `debates/*/workspace/` is added to `.gitignore`; a checkout of another
  repository must never be committable into this one.

## §3 Agent plumbing (`CliAgent`, registry, `agents.yaml`)

Two new optional per-agent fields in `agents.yaml`:

- `workspace_args`: list of argv tokens appended to `command` only when the
  debate is repo-grounded. This is where each platform's sandbox posture
  lives (e.g. `codex` → `["--sandbox", "workspace-write"]`; `claude` →
  its permission flags). The lab does not interpret these tokens. An agent
  with no `workspace_args` still runs with `cwd` set — the worktree is the
  isolation layer; the flags are per-platform tuning the user controls.
- `stall_after`: soft alert threshold, §6. Int (seconds, all tasks) or map
  `{fast: int, deep: int}`. Defaults: deep 900, fast 300.

One changed field:

- `timeout`: now *opt-in*. Absent means no ceiling — `subprocess.run` gets
  `timeout=None` and a deep call may run for hours. An int applies to all
  tasks (existing files keep their meaning); a map `{fast: int|null,
  deep: int|null}` sets per-tier ceilings. `ErrorKind.TIMEOUT` and its
  retry path survive unchanged but fire only for agents that opted in.
  **Behavior change:** the previous implicit 180s default is gone; the
  registry default becomes `None`.
- Exception: `_discover_models` (`cli_agent.py:70-85`) currently reuses
  `self.timeout` and would hang forever under `None`. It gets a fixed 30s
  constant — model discovery is a metadata lookup, not real work.

`CliAgent` gains a `workdir: str | None` constructor parameter, passed as
`cwd=` to `subprocess.run` in `ask` (`cli_agent.py:26`), and appends
`workspace_args` in `_build_command` when `workdir` is set. `ApiAgent`
accepts and ignores the same construction-time information. The registry's
load path takes the workspace path from `debate run` (it already builds the
roster per-run, `cli.py:61`) and exposes on each agent a
`workspace_attached: bool` the orchestrator and startup banner read.

## §4 Prompts: the grounding preamble

The orchestrator composes a per-agent problem preface; the template
functions in `prompts.py` are unchanged and receive the already-prefaced
problem string.

For a workspace-attached agent, every phase prompt's problem block is
preceded by:

> The repository under discussion is checked out at your current working
> directory (commit `<sha>`). Read the code and run verification commands
> (tests, linters) to check claims — yours and the other agents' — before
> asserting them. Cite evidence as `file:line`.

All phases get the same `cwd`; the existing task tiers keep nominate/vote
cheap (FAST) while propose/critique/revise/synthesize (DEEP) dig. No new
parse requirements: citations are a prompt contract, not a validated format
— synthesis taught us what free-prose replies can and cannot promise
(`specs/2026-07-15-synthesis-phase-design.md`, "cannot be validated").

## §5 API-backed agents in a grounded debate

A workspace-unattached agent (API backend, or `auto` that fell back to the
API) gets this preface instead:

> The other agents in this debate can read the repository under discussion
> and run its tests; you cannot. Weigh their `file:line` citations and test
> results as evidence. Do not assert facts about file contents you have not
> seen quoted.

The `debate run` startup banner (which already prints skip warnings,
`cli.py:61-63`) states, per agent: attached or not, and with which
`workspace_args` — the human sees the blast radius before the debate
starts.

## §6 Live status and stall alerts

With no proactive cancellation, visibility does the job cancellation used
to do. Three mechanisms, all driven by one heartbeat loop in the run
process (interval: 60s):

**Terminal.** A line when each agent call starts; a heartbeat line per
still-running call (`⏳ claude · propose · 14m`); on first crossing an
agent's `stall_after` threshold, the line escalates once with a terminal
bell (`\a`):

```
⚠ claude · propose · 17m — exceeded stall threshold (15m); still waiting.
  Ctrl-C interrupts; `debate run` resumes from the last completed phase.
```

A stall alert is a heads-up, never a verdict and never a kill. Elapsed time
is the only universal signal — `claude -p`-style CLIs emit nothing until
their final answer, so output silence cannot distinguish thinking from
wedged.

**`live.json`.** Each heartbeat tick atomically rewrites
`debates/<id>/live.json` (write-temp-then-rename, the pattern `store.py`
already uses for `state.json`):

```json
{
  "updated": "<iso ts>", "round": 2, "phase": "critique",
  "calls": [
    {"agent": "claude", "task": "deep", "started": "<iso ts>",
     "elapsed_s": 1020, "stalled": true}
  ]
}
```

Deleted on run exit (any exit path). It is ephemeral run state, not record:
the transcript remains the sole source of truth, `live.json` is never read
on resume, and it joins `workspace/` under the debates entries in
`.gitignore`.

**Viewer.** `debate serve` polls `live.json` alongside its existing files.
Per-agent badge: `running 4m` → amber `stalled 17m`. Tab title gains `⚠`
while any call is stalled. A `live.json` whose `updated` is older than two
heartbeat intervals is itself the signal that the *run process* died or
hung — the viewer reports that state distinctly ("run process not
responding") — which covers the orchestrator being what is stuck, the one
failure the run process cannot report about itself.

Deliberately out: auto-kill, desktop-notification dependencies (the bell
and tab title are dependency-free; a notify hook is deferred, §9), reading
agent internals.

## §7 What this consciously accepts

- A wedged CLI holds its phase hostage until a human notices. The bell, the
  amber badge, and the tab title exist to make noticing cheap.
- A phase's wall-clock is bounded by its slowest agent. That is the price
  of "difficult problems need hours", and per-agent `timeout` ceilings
  remain available for rosters where it is not worth paying.
- Retrying after an opt-in timeout re-spends the whole ceiling. The
  existing backoff-then-abstain policy is unchanged; a roster that opts
  into ceilings accepts their retry cost.
- Verification commands run with the user's OS privileges. The worktree
  absorbs writes; network and out-of-tree access are governed by each
  CLI's own sandbox flags in `workspace_args`, which the user sets and the
  startup banner shows.

## §8 Testing

Unit, against a tmp git fixture repo where relevant:

- Registry: parses `workspace_args`, `stall_after`, int-or-map `timeout`;
  absent `timeout` yields `None`; a bare int still applies to all tasks.
- `CliAgent`: passes `cwd`; appends `workspace_args` only when attached;
  per-tier ceilings picked by task; `timeout=None` reaches
  `subprocess.run`; `_discover_models` uses the fixed 30s constant.
- Workspace lifecycle: create pins the recorded commit; resume reuses;
  remove tolerates a vanished source; creation failure halts with `error`.
- Prompts/orchestrator: attached preamble carries the sha; unattached
  agents get the §5 notice; non-grounded debates emit neither.
- Status: heartbeat marks `stalled` at the threshold; `live.json` is
  atomically rewritten and deleted on exit; viewer distinguishes stalled
  agent from stale `live.json`.

Integration: a stub CLI script that `cat`s a file from its `cwd` proves the
wiring end-to-end through `debate run` on a fixture repo — the stub's
"proposal" can only contain the file's contents if `cwd` was the worktree.

## §9 Deferred

- **Tool loop for API agents** (read/grep/run via provider tool-use), the
  rejected Approach B — revisit if an API-only agent becomes essential.
- **Evidence packs** (Approach C) as a cheap supplement for unattached
  agents.
- **A pre-vote verification phase** — a designated agent runs the
  candidate's checkable claims before the roster votes.
- **Desktop notification hook** (`notify-send` etc.) on stall, opt-in.
- **Devil's-advocate pressure** — round-1 unanimity suggests critique needs
  teeth; separate product question, separate spec.

## Build order

1. **Opt-in timeouts + model-discovery constant** (§3) — standalone, and by
   itself fixes the observed 180s halt.
2. **Workspace plumbing** (§1, §2, §3 rest) — `--repo`, worktree lifecycle,
   `cwd`/`workspace_args`, startup banner.
3. **Grounding prompts** (§4, §5) — preamble, citation contract, unattached
   notice.
4. **Live status + stall alerts** (§6) — heartbeat, bell, `live.json`,
   viewer badges.

Each step lands green independently; 2-4 are inert for debates created
without `--repo`.
