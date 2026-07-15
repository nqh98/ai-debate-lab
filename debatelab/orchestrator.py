"""Runs a debate: fans out phase prompts to all agents, applies the protocol,
checkpoints state after every phase so interrupted runs resume."""
import concurrent.futures as cf
import hashlib
import json
import random
import time
from fractions import Fraction

from . import prompts, protocol, retry
from .agents import models
from .agents.base import AgentError
from .store import render_summary

DEFAULT_SLEEP = time.sleep


def _state_sha256(state):
    payload = json.dumps(
        state, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class DebateHalted(Exception):
    """Too few agents responded to continue the debate."""

    def __init__(self, failed_phase: str, responders: int):
        self.failed_phase = failed_phase
        super().__init__(
            f"only {responders} agent(s) responded in phase "
            f"'{failed_phase}' — need at least 2"
        )


class Orchestrator:
    def __init__(self, store, agents, progress=lambda msg: None,
                 sleep=None, rng=None):
        if len(agents) < 2:
            raise ValueError("a debate needs at least 2 agents")
        self.store = store
        self.agents = {a.name: a for a in agents}
        self.order = [a.name for a in agents]
        self.progress = progress
        self.sleep = sleep or DEFAULT_SLEEP
        self.rng = rng or random.Random()

    def run(self, debate_id: str, max_rounds: int | None = None,
            quorum: Fraction | None = None) -> str:
        state = self.store.read_state(debate_id)
        if state["status"] in ("awaiting_human", "approved", "rejected"):
            return state["status"]
        loaded_state_sha256 = _state_sha256(state)
        loaded_status = state["status"]
        if max_rounds is not None:
            state["max_rounds"] = max_rounds
        if quorum is not None:
            state["quorum"] = str(quorum)
        state.setdefault("quorum", str(protocol.DEFAULT_QUORUM))
        recorded = state.get("roster")
        if recorded is not None and recorded != self.order:
            self.store.append_event(debate_id, {
                "round": state["round"], "phase": "run", "agent": None,
                "type": "roster_changed",
                "content": f"roster changed from {recorded} to {self.order}",
            })
        state["roster"] = list(self.order)
        state["status"] = "running"
        # Every run, not only when something changed — that is precisely the
        # bug in roster_changed above, which fires on a difference and so
        # records nothing on a first run. The roster is the denominator
        # check_consensus divides by; a reader that has to guess it can reach
        # a different verdict than this run did.
        self.store.append_event(debate_id, {
            "round": state["round"], "phase": "run", "agent": None,
            "type": "run_config",
            "content": (
                f"roster {self.order}, max_rounds {state['max_rounds']}, "
                f"quorum {state['quorum']}"
            ),
            "roster": list(self.order),
            "max_rounds": state["max_rounds"],
            "quorum": state["quorum"],
            "last_completed_phase": state["last_completed_phase"],
            "loaded_status": loaded_status,
            "loaded_state_sha256": loaded_state_sha256,
        })
        problem = self.store.read_problem(debate_id)
        try:
            while True:
                rnd, phase = protocol.next_phase(
                    state["round"], state["last_completed_phase"]
                )
                if rnd > state["max_rounds"]:
                    state["status"] = "no_consensus"
                    self.store.append_event(debate_id, {
                        "round": state["round"], "phase": "end", "agent": None,
                        "type": "no_consensus",
                        "content": (
                            "no consensus reached within the configured round limit"
                        ),
                        "tally": protocol.tally(
                            state["votes"],
                            len(state["roster"]),
                            Fraction(state["quorum"]),
                        ),
                    })
                    break
                state["round"] = rnd
                state["abstained"] = []
                if phase == "vote":
                    state["candidate"] = None
                self.progress(f"round {rnd}/{state['max_rounds']}: {phase}")
                # Brackets the two assignments a reader must reproduce. Without
                # phase_completed, a phase that raised DebateHalted is
                # indistinguishable from one that finished; without
                # phase_started, a halted debate's round cannot be recovered,
                # because state["round"] is assigned before the phase runs.
                self.store.append_event(debate_id, {
                    "round": rnd, "phase": phase, "agent": None,
                    "type": "phase_started", "content": "",
                })
                getattr(self, f"_phase_{phase}")(debate_id, state, problem)
                state["last_completed_phase"] = phase
                self.store.append_event(debate_id, {
                    "round": rnd, "phase": phase, "agent": None,
                    "type": "phase_completed", "content": "",
                })
                quorum_frac = Fraction(state["quorum"])
                roster_size = len(state["roster"])
                if phase == "vote" and protocol.check_consensus(
                    state["votes"], roster_size, quorum_frac
                ):
                    state["status"] = "awaiting_human"
                    self.store.append_event(debate_id, {
                        "round": rnd, "phase": "vote",
                        "agent": state["candidate"]["agent"],
                        "type": "consensus",
                        "content": state["candidate"]["text"],
                        "tally": protocol.tally(
                            state["votes"], roster_size, quorum_frac
                        ),
                    })
                    self._checkpoint(debate_id, state)
                    break
                self._checkpoint(debate_id, state)
        except DebateHalted as e:
            state["status"] = "error"
            self.store.append_event(debate_id, {
                "round": state["round"], "phase": "end", "agent": None,
                "type": "error", "content": str(e),
                "failed_phase": e.failed_phase,
            })
        self._checkpoint(debate_id, state)
        self.store.rebuild_index()
        return state["status"]

    def _checkpoint(self, debate_id, state):
        self.store.write_state(debate_id, state)
        self.store.write_summary(debate_id, render_summary(state))

    def _abstain(self, debate_id, state, phase, name, content, reason):
        """Record an agent as abstaining for this phase."""
        state["abstained"] = sorted(set(state["abstained"]) | {name})
        self.store.append_event(debate_id, {
            "round": state["round"], "phase": phase, "agent": name,
            "type": "abstained", "content": content, "reason": reason,
        })

    def _record_call(self, debate_id, state, phase, name, task):
        """Build the on_attempt hook that logs one agent_call per attempt."""
        def on_attempt(attempt, duration_ms, error):
            event = {
                "round": state["round"], "phase": phase, "agent": name,
                "type": "agent_call", "task": task, "attempt": attempt,
                "duration_ms": duration_ms, "ok": error is None,
                "content": "",
            }
            if error is not None:
                event["kind"] = error.kind.value
                event["content"] = str(error)
            self.store.append_event(debate_id, event)

        return on_attempt

    def _reask(self, debate_id, state, phase, name, prompt, parse, required,
               task):
        """Ask one agent again after an unparseable reply.

        Returns (value, text); (None, None) when the agent errors out.
        Re-asks run serially because they are rare, cheap FAST requests.
        """
        try:
            text = retry.call_with_retry(
                lambda: self.agents[name].ask(
                    prompts.reask(prompt, required), task
                ),
                rng=self.rng,
                sleep=self.sleep,
                on_attempt=self._record_call(
                    debate_id, state, phase, name, task
                ),
            )
        except AgentError:
            return None, None
        return parse(text), text

    def _fanout(self, debate_id, state, phase, prompt_for,
                task=models.DEEP) -> dict:
        """Ask every agent concurrently, retrying transient failures."""
        results = {}

        def call(name):
            prompt = prompt_for(name)
            return retry.call_with_retry(
                lambda: self.agents[name].ask(prompt, task),
                rng=self.rng,
                sleep=self.sleep,
                on_attempt=self._record_call(
                    debate_id, state, phase, name, task
                ),
            )

        with cf.ThreadPoolExecutor(max_workers=len(self.order)) as ex:
            futures = {ex.submit(call, name): name for name in self.order}
            for fut in cf.as_completed(futures):
                name = futures[fut]
                try:
                    results[name] = fut.result()
                except AgentError as e:
                    state["abstained"] = sorted(set(state["abstained"]) | {name})
                    self.store.append_event(debate_id, {
                        "round": state["round"], "phase": phase, "agent": name,
                        "type": "abstained", "content": str(e),
                    })
        if len(results) < 2:
            raise DebateHalted(phase, len(results))
        return results

    def _phase_propose(self, debate_id, state, problem):
        results = self._fanout(
            debate_id, state, "propose",
            lambda name: prompts.propose_prompt(name, problem),
        )
        state["proposals"] = results
        for name, text in results.items():
            self.store.append_event(debate_id, {
                "round": state["round"], "phase": "propose", "agent": name,
                "type": "proposal", "content": text,
            })

    def _phase_critique(self, debate_id, state, problem):
        proposals = state["proposals"]
        reject_reasons = {
            name: v["reason"]
            for name, v in state.get("votes", {}).items()
            if v["vote"] == "reject"
        }

        def prompt_for(name):
            others = {n: t for n, t in proposals.items() if n != name}
            return prompts.critique_prompt(
                name, problem, others, reject_reasons or None
            )

        results = self._fanout(debate_id, state, "critique", prompt_for)
        state["critiques"] = results
        for name, text in results.items():
            self.store.append_event(debate_id, {
                "round": state["round"], "phase": "critique", "agent": name,
                "type": "critique", "content": text,
            })

    def _phase_revise(self, debate_id, state, problem):
        def prompt_for(name):
            own = state["proposals"].get(name, "(no previous proposal)")
            return prompts.revise_prompt(name, problem, own, state["critiques"])

        results = self._fanout(debate_id, state, "revise", prompt_for)
        state["proposals"] = {**state["proposals"], **results}
        for name, text in results.items():
            self.store.append_event(debate_id, {
                "round": state["round"], "phase": "revise", "agent": name,
                "type": "revision", "content": text,
            })

    def _phase_vote(self, debate_id, state, problem):
        proposals = state["proposals"]
        names = list(proposals)
        nom_raw = self._fanout(
            debate_id, state, "vote",
            lambda name: prompts.nominate_prompt(name, problem, proposals, names),
            task=models.FAST,
        )
        nominations = {}
        for name, text in nom_raw.items():
            self.store.append_event(debate_id, {
                "round": state["round"], "phase": "vote", "agent": name,
                "type": "nomination", "content": text,
            })
            nominee = prompts.parse_nomination(text, names)
            if nominee is None:
                nominee, retry_text = self._reask(
                    debate_id,
                    state,
                    "vote",
                    name,
                    prompts.nominate_prompt(name, problem, proposals, names),
                    lambda t: prompts.parse_nomination(t, names),
                    prompts.NOMINATE_REQUIRED,
                    models.FAST,
                )
                if retry_text is not None:
                    text = retry_text
                    self.store.append_event(debate_id, {
                        "round": state["round"], "phase": "vote", "agent": name,
                        "type": "nomination_retry", "content": retry_text,
                        "nominee": nominee,
                    })
            if nominee == name:
                self.store.append_event(debate_id, {
                    "round": state["round"], "phase": "vote", "agent": name,
                    "type": "nomination_dropped", "content": text,
                    "reason": "self-nomination",
                })
                continue
            if nominee:
                nominations[name] = nominee
        order_with_proposals = [n for n in self.order if n in proposals]
        winner, was_fallback = protocol.select_candidate(
            nominations, order_with_proposals, f"{debate_id}:{state['round']}"
        )
        if was_fallback:
            self.store.append_event(debate_id, {
                "round": state["round"], "phase": "vote", "agent": winner,
                "type": "fallback_candidate",
                "content": (
                    "no valid nominations; candidate chosen by seeded draw"
                ),
            })
        state["candidate"] = {"agent": winner, "text": proposals[winner]}
        self.store.append_event(debate_id, {
            "round": state["round"], "phase": "vote", "agent": winner,
            "type": "candidate", "content": proposals[winner],
        })
        vote_raw = self._fanout(
            debate_id, state, "vote",
            lambda name: prompts.vote_prompt(
                name, problem, winner, proposals[winner]
            ),
            task=models.FAST,
        )
        votes = {}
        for name, text in vote_raw.items():
            verdict = prompts.parse_vote(text)
            if verdict is None:
                verdict, retry_text = self._reask(
                    debate_id,
                    state,
                    "vote",
                    name,
                    prompts.vote_prompt(
                        name, problem, winner, proposals[winner]
                    ),
                    prompts.parse_vote,
                    prompts.VOTE_REQUIRED,
                    models.FAST,
                )
                if retry_text is not None:
                    text = retry_text
            if verdict is None:
                self._abstain(
                    debate_id, state, "vote", name, text, "unparseable vote"
                )
                continue
            votes[name] = {"vote": verdict, "reason": text}
            self.store.append_event(debate_id, {
                "round": state["round"], "phase": "vote", "agent": name,
                "type": "vote", "verdict": verdict, "content": text,
            })
        state["votes"] = votes
