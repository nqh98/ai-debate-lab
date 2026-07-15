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
