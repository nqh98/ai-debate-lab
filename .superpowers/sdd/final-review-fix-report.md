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
