## Final Review Fixes

- Replay now preserves prior-round critiques and votes when a later critique
  or vote phase halts before emitting a replacement result. The result
  dictionaries reset lazily on their first matching event, while abstained
  still resets at every phase_started.
- Added unit and Orchestrator differential regressions for later-round
  critique and vote halts.
- fsck now searches backward through checkpoint-boundary prefixes for the
  newest replay that equals state.json; later events are reported as
  in-flight. Added interruption regressions for phase_completed and
  human_decision.
- Divergence output now distinguishes an absent state key from a nullable
  value and reports missing human_decision.

### Verification

$ /home/bossbaby/Desktop/fix-me/ai-debate-lab/.venv/bin/python -m pytest tests/test_replay.py -q
.......................                                                  [100%]
23 passed in 0.03s

$ /home/bossbaby/Desktop/fix-me/ai-debate-lab/.venv/bin/python -m pytest tests/test_replay_differential.py -q
..........                                                               [100%]
10 passed in 0.20s

$ /home/bossbaby/Desktop/fix-me/ai-debate-lab/.venv/bin/python -m pytest tests/test_cli.py -q
...............................                                          [100%]
31 passed in 0.30s

$ /home/bossbaby/Desktop/fix-me/ai-debate-lab/.venv/bin/python -m pytest -q
........................................................................ [ 28%]
........................................................................ [ 56%]
........................................................................ [ 84%]
........................................                                 [100%]
256 passed in 3.74s

$ /home/bossbaby/Desktop/fix-me/ai-debate-lab/.venv/bin/python -c "<replay import-purity AST check>"
IMPORTS: []
BANNED: []

$ git diff --check
(no output; exit 0)

## Third Final Re-review Fixes

- Replay now discards a superseded completed attempt when a resumed run emits
  no_consensus before starting another phase. This preserves the restored
  checkpoint and resumed run_config when a lower max_rounds cap ends the run
  immediately, while retaining the existing pending-checkpoint handling for
  continuous runs.
- Orchestrator now clears candidate before every vote phase_started event, and
  replay mirrors that transition. A halted later vote attempt therefore cannot
  persist a candidate from an earlier attempt.
- Added transcript-only coverage for the lower-cap superseded-checkpoint case
  and vote-attempt candidate reset. Added differential regressions for a
  later-round critique crash followed by a lower cap, and for a resumed vote
  whose nomination fanout halts before selecting a candidate.

### Third Re-review Verification

$ /home/bossbaby/Desktop/fix-me/ai-debate-lab/.venv/bin/python -m pytest tests/test_replay.py -q
............................                                             [100%]
28 passed in 0.03s

$ /home/bossbaby/Desktop/fix-me/ai-debate-lab/.venv/bin/python -m pytest tests/test_replay_differential.py -q
...............                                                          [100%]
15 passed in 0.24s

$ /home/bossbaby/Desktop/fix-me/ai-debate-lab/.venv/bin/python -m pytest tests/test_cli.py -q
...............................                                          [100%]
31 passed in 0.26s

$ /home/bossbaby/Desktop/fix-me/ai-debate-lab/.venv/bin/python -m pytest -q
........................................................................ [ 27%]
........................................................................ [ 54%]
........................................................................ [ 81%]
..................................................                       [100%]
266 passed in 3.62s

$ /home/bossbaby/Desktop/fix-me/ai-debate-lab/.venv/bin/python -c "<replay import-purity AST check>"
IMPORTS: ['copy']
BANNED: []

$ git diff --check
(no output; exit 0)

## Second Final Re-review Fixes

- Replay now creates a fresh result stage for every `phase_started` event.
  Critiques and votes are replaced only when that exact attempt emits
  `phase_completed`, including valid empty replacements. Candidate events
  update the working attempt state and therefore remain visible on a halt,
  while still rolling back with an abandoned attempt on resume.
- Replay now maintains separate working and checkpoint-safe state snapshots.
  A later phase start, `no_consensus`, or `human_decision` proves the prior
  checkpoint durable. A later `run_config` restores the checkpoint-safe state
  and discards a completed but uncheckpointed attempt when the same
  round/phase is resumed.
- Added the exact crash-before-critique-checkpoint regression followed by a
  resumed critique halt. The final state and replay both retain
  `last_completed_phase=propose`, empty critiques, and `status=error`.
- Added same-round/same-phase retry coverage with changed responders, plus a
  two-round differential case where every final vote response is unparseable
  and the completed vote correctly replaces prior votes with `{}`.

### Second Re-review Verification

$ /home/bossbaby/Desktop/fix-me/ai-debate-lab/.venv/bin/python -m pytest tests/test_replay.py -q
..........................                                               [100%]
26 passed in 0.03s

$ /home/bossbaby/Desktop/fix-me/ai-debate-lab/.venv/bin/python -m pytest tests/test_replay_differential.py -q
.............                                                            [100%]
13 passed in 0.34s

$ /home/bossbaby/Desktop/fix-me/ai-debate-lab/.venv/bin/python -m pytest tests/test_cli.py -q
...............................                                          [100%]
31 passed in 0.34s

$ /home/bossbaby/Desktop/fix-me/ai-debate-lab/.venv/bin/python -m pytest -q
........................................................................ [ 27%]
........................................................................ [ 54%]
........................................................................ [ 82%]
..............................................                           [100%]
262 passed in 4.02s

$ /home/bossbaby/Desktop/fix-me/ai-debate-lab/.venv/bin/python -c "<replay import-purity AST check>"
IMPORTS: ['copy']
BANNED: []

$ /home/bossbaby/Desktop/fix-me/ai-debate-lab/.venv/bin/python -c "<14-key replay state check>"
STATE_KEYS: 14

$ git diff --check
(no output; exit 0)

## Latest Final Re-review Fixes

- `run_config` now records the loaded checkpoint's
  `last_completed_phase` and pre-run `loaded_status`, in addition to its
  existing `round`. These event-only identity fields do not change the
  `state.json` shape.
- Replay now compares an unresolved completed-phase or `DebateHalted`
  checkpoint candidate with that loaded-state identity as soon as the next
  `run_config` arrives. Matching candidates are promoted before applying the
  new run configuration; non-matching candidates are discarded in favor of
  the older durable checkpoint.
- Legacy `run_config` events retain phase-based checkpoint resolution. When a
  lower cap emits `no_consensus` without a new phase, replay uses the legacy
  event's existing round to distinguish the candidate where possible.
- Added differential regressions for interruption after the round-2 critique
  checkpoint write returns from storage, a normal round-2 critique halt, and
  a candidate selected during a halted round-2 vote. Each resumes with a
  lower cap and no new phase attempt. The existing crash-before-write case
  remains covered and discards its abandoned candidate.
- Added transcript-level durable-versus-abandoned lower-cap coverage and
  assertions for the new `run_config` identity fields.

### Latest Re-review Verification

$ /home/bossbaby/Desktop/fix-me/ai-debate-lab/.venv/bin/python -m pytest tests/test_replay.py -q
.............................                                            [100%]
29 passed in 0.03s

$ /home/bossbaby/Desktop/fix-me/ai-debate-lab/.venv/bin/python -m pytest tests/test_replay_differential.py -q
..................                                                       [100%]
18 passed in 0.46s

$ /home/bossbaby/Desktop/fix-me/ai-debate-lab/.venv/bin/python -m pytest tests/test_cli.py -q
...............................                                          [100%]
31 passed in 0.37s

$ /home/bossbaby/Desktop/fix-me/ai-debate-lab/.venv/bin/python -m pytest -q
........................................................................ [ 26%]
........................................................................ [ 53%]
........................................................................ [ 80%]
......................................................                   [100%]
270 passed in 3.72s

$ /home/bossbaby/Desktop/fix-me/ai-debate-lab/.venv/bin/python -c "<replay import-purity AST check>"
IMPORTS: ['copy']
BANNED: []

$ git diff --check
(no output; exit 0)

## Loaded-State Identity Final Re-review Fix

- Orchestrator now computes `loaded_state_sha256` from the complete state dict
  immediately after loading `state.json`, before applying `max_rounds`,
  `quorum`, roster, or status changes. The canonical stdlib encoding uses
  sorted keys, UTF-8, `ensure_ascii=False`, and compact separators.
- Every new `run_config` event carries that digest in addition to all existing
  fields. The `state.json` shape is unchanged.
- Replay independently computes the same canonical digest for unresolved
  checkpoint candidates and uses it as the authoritative match when present.
  Events without the digest retain the prior round/phase/status best-effort
  fallback.
- Added differential regressions for repeated halted vote attempts on both
  sides of the second halt's checkpoint write. A lower-cap resume now retains
  the first candidate/abstentions after a crash-before-write and promotes the
  second candidate/abstentions after a durable write.
- Added unit coverage proving that states with equal round, completed phase,
  and status do not match when their full loaded-state digests differ, plus
  coverage that new Orchestrator events include the digest.

### Loaded-State Identity Verification

$ /home/bossbaby/Desktop/fix-me/ai-debate-lab/.venv/bin/python -m pytest tests/test_replay.py -q
..............................                                           [100%]
30 passed in 0.04s

$ /home/bossbaby/Desktop/fix-me/ai-debate-lab/.venv/bin/python -m pytest tests/test_replay_differential.py -q
....................                                                     [100%]
20 passed in 0.43s

$ /home/bossbaby/Desktop/fix-me/ai-debate-lab/.venv/bin/python -m pytest tests/test_cli.py -q
...............................                                          [100%]
31 passed in 0.33s

$ /home/bossbaby/Desktop/fix-me/ai-debate-lab/.venv/bin/python -m pytest -q
........................................................................ [ 26%]
........................................................................ [ 52%]
........................................................................ [ 79%]
.........................................................                [100%]
273 passed in 3.90s

$ /home/bossbaby/Desktop/fix-me/ai-debate-lab/.venv/bin/python -c "<replay import-purity AST check>"
IMPORTS: ['copy', 'hashlib', 'json']
BANNED: []

$ git diff --check
(no output; exit 0)
