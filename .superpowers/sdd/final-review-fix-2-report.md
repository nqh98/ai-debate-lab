# Final Review Fix 2 Report

Date: 2026-07-15

## Findings Fixed

- `run_config` now retains candidate provenance and text only when it records a resume from `loaded_status == "running"`; new attempts continue to clear stale candidate data.
- Approved results now derive `reason` after deriving `answer`: it is `null` only for an actual answer, otherwise it is `"approved without a candidate"`.
- `cmd_run` now has a total status-to-exit-code mapping: 0 only for `awaiting_human`, 3 for `error`, and 1 for every other status.

## TDD Evidence

RED:

```text
$ /home/bossbaby/Desktop/fix-me/ai-debate-lab/.venv/bin/python -m pytest tests/test_result.py::test_resume_running_preserves_candidate_for_later_approval tests/test_cli.py::test_result_requires_an_answer_even_when_the_result_is_approved tests/test_cli.py::test_run_exits_one_for_a_predecided_debate -q
FFFF                                                                     [100%]
4 failed in 0.16s
```

GREEN:

```text
$ /home/bossbaby/Desktop/fix-me/ai-debate-lab/.venv/bin/python -m pytest tests/test_result.py::test_resume_running_preserves_candidate_for_later_approval tests/test_cli.py::test_result_requires_an_answer_even_when_the_result_is_approved tests/test_cli.py::test_run_exits_one_for_a_predecided_debate -q
....                                                                     [100%]
4 passed in 0.06s
```

Focused verification:

```text
$ /home/bossbaby/Desktop/fix-me/ai-debate-lab/.venv/bin/python -m pytest tests/test_result.py tests/test_cli.py tests/test_integration.py tests/test_replay_differential.py -q
88 passed in 1.08s
```

Full verification:

```text
$ /home/bossbaby/Desktop/fix-me/ai-debate-lab/.venv/bin/python -m pytest -q
310 passed in 4.00s
```

Formatting verification:

```text
$ git diff --check
exit 0
```
