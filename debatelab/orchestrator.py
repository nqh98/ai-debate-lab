"""Runs a debate: fans out phase prompts to all agents, applies the protocol,
checkpoints state after every phase so interrupted runs resume."""
import concurrent.futures as cf
from fractions import Fraction

from . import prompts, protocol
from .agents import models
from .agents.base import AgentError
from .store import render_summary


class DebateHalted(Exception):
    """Too few agents responded to continue the debate."""


class Orchestrator:
    def __init__(self, store, agents, progress=lambda msg: None):
        if len(agents) < 2:
            raise ValueError("a debate needs at least 2 agents")
        self.store = store
        self.agents = {a.name: a for a in agents}
        self.order = [a.name for a in agents]
        self.progress = progress

    def run(self, debate_id: str, max_rounds: int | None = None,
            quorum: Fraction | None = None) -> str:
        state = self.store.read_state(debate_id)
        if state["status"] in ("awaiting_human", "approved", "rejected"):
            return state["status"]
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
                    })
                    break
                state["round"] = rnd
                state["abstained"] = []
                self.progress(f"round {rnd}/{state['max_rounds']}: {phase}")
                getattr(self, f"_phase_{phase}")(debate_id, state, problem)
                state["last_completed_phase"] = phase
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

    def _fanout(self, debate_id, state, phase, prompt_for,
                task=models.DEEP) -> dict:
        """Ask every agent concurrently. One retry per agent; a second failure
        records an abstention. Raises DebateHalted if fewer than 2 responded."""
        results = {}

        def call(name):
            prompt = prompt_for(name)
            try:
                return self.agents[name].ask(prompt, task)
            except AgentError:
                return self.agents[name].ask(prompt, task)

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
            raise DebateHalted(
                f"only {len(results)} agent(s) responded in phase "
                f"'{phase}' — need at least 2"
            )
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
