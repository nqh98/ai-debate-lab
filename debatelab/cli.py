"""The `debate` command-line interface."""
import argparse
import functools
import http.server
import json
import sys
from fractions import Fraction
from importlib import resources
from pathlib import Path

from . import replay as replay_mod
from .agents import models, registry
from .result import build_result, render_final
from .store import DebateStore, LockError, render_summary


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def quorum_fraction(value: str) -> Fraction:
    try:
        q = Fraction(value)
    except (ValueError, ZeroDivisionError):
        raise argparse.ArgumentTypeError(
            f"not a fraction: {value!r} (try 2/3)"
        )
    if not 0 < q <= 1:
        raise argparse.ArgumentTypeError("must be greater than 0 and at most 1")
    return q


def get_store() -> DebateStore:
    return DebateStore(Path.cwd() / "debates")


def cmd_new(args):
    store = get_store()
    workspace = None
    if args.repo:
        from . import workspace as workspace_mod
        try:
            workspace = workspace_mod.pin(args.repo)
        except workspace_mod.WorkspaceError as e:
            sys.exit(f"--repo: {e}")
    contexts = []
    for f in args.context or []:
        p = Path(f)
        contexts.append((p.name, p.read_text()))
    title = args.problem.strip().splitlines()[0][:60]
    print(store.create(title, args.problem, contexts, workspace=workspace))


def cmd_run(args):
    from .orchestrator import Orchestrator

    store = get_store()
    try:
        with store.debate_lock(args.id, command="run", force=args.force):
            specs = registry.load_agent_specs(args.config)
            ready = []
            for spec in specs:
                if not spec.enabled:
                    continue
                problem = registry.spec_problem(spec)
                if problem:
                    print(f"skipping agent '{spec.name}': {problem}", flush=True)
                    continue
                ready.append(spec)
            agents = registry.build_agents(ready)
            try:
                orch = Orchestrator(
                    store,
                    agents,
                    progress=lambda m: print(m, flush=True),
                )
            except ValueError as e:
                sys.exit(str(e))
            status = orch.run(
                args.id, max_rounds=args.max_rounds, quorum=args.quorum
            )
    except LockError as e:
        sys.exit(str(e))
    print(f"final status: {status}")
    if status in ("awaiting_human", "no_consensus"):
        print(
            f"review with `debate show {args.id}`, then "
            f"`debate approve {args.id}` or `debate reject {args.id} -m ...`"
        )
    if status == "error":
        sys.exit(3)
    if status != "awaiting_human":
        sys.exit(1)


def _status_line(state):
    return (
        f"{state['id']}: {state['status']} "
        f"(round {state['round']}/{state['max_rounds']})"
    )


def cmd_status(args):
    print(_status_line(get_store().read_state(args.id)))


def cmd_list(args):
    store = get_store()
    for did in store.list_ids():
        print(_status_line(store.read_state(did)))


def cmd_show(args):
    print(get_store().read_summary(args.id) or "(no summary yet)")


def cmd_result(args):
    store = get_store()
    state = store.read_state(args.id)
    result = build_result(
        store.read_events(args.id),
        id_fallback=args.id,
        title_fallback=state.get("title"),
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(render_final(result), end="")
    if result["status"] != "approved" or result["answer"] is None:
        sys.exit(1)


def _write_derived_views(store, debate_id, state):
    result = build_result(
        store.read_events(debate_id),
        id_fallback=debate_id,
        title_fallback=state.get("title"),
    )
    store.write_summary(debate_id, render_summary(state))
    store.write_result(debate_id, result)
    store.write_final(debate_id, render_final(result))


# Events that can describe a checkpoint, although each is appended before its
# state.json write completes.
_BOUNDARY_TYPES = frozenset({
    "phase_completed", "consensus", "no_consensus", "error", "human_decision",
})


def _brief(value, limit=70):
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return text if len(text) <= limit else text[:limit] + "..."


def cmd_fsck(args):
    """Check state.json against a replay of the transcript.

    state.json is the last checkpoint, not the latest truth. Boundary events
    are appended before their corresponding checkpoint write, so a hard crash
    can leave even a boundary event beyond state.json. Search backward for the
    newest boundary prefix that matches state.json and report later events as
    in flight.
    """
    store = get_store()
    events = store.read_events(args.id)
    state = store.read_state(args.id)
    try:
        replay_mod.replay(events[:1])
    except replay_mod.MissingGenesis as e:
        print(f"{args.id}: unverifiable — {e}")
        sys.exit(3)

    boundaries = [0] + [
        i for i, event in enumerate(events)
        if event.get("type") in _BOUNDARY_TYPES
    ]
    latest_expected = None
    matching_boundary = None
    for boundary in reversed(boundaries):
        expected = replay_mod.replay(events[: boundary + 1])
        if latest_expected is None:
            latest_expected = expected
        if expected == state:
            matching_boundary = boundary
            break

    if matching_boundary is not None:
        in_flight = len(events) - (matching_boundary + 1)
        note = ""
        if in_flight:
            plural = "s" if in_flight != 1 else ""
            note = (
                f" ({in_flight} event{plural} in flight after the last "
                "checkpoint)"
            )
        print(f"{args.id}: ok{note}")
        return

    expected = latest_expected
    print(f"{args.id}: diverged")
    missing = object()
    for key in sorted(set(expected) | set(state)):
        state_value = state.get(key, missing)
        replay_value = expected.get(key, missing)
        if state_value != replay_value:
            print(f"  {key}:")
            state_text = (
                "<missing>" if state_value is missing else _brief(state_value)
            )
            replay_text = (
                "<missing>" if replay_value is missing else _brief(replay_value)
            )
            print(f"    state.json: {state_text}")
            print(f"    replay    : {replay_text}")
    sys.exit(1)


# `decision` is state.json's vocabulary ("approved"); `command` is the verb the
# human typed ("approve"). The lock records the verb.
_DECISION_COMMANDS = {"approved": "approve", "rejected": "reject"}


def cmd_decide(args, decision):
    store = get_store()
    try:
        with store.debate_lock(
            args.id, command=_DECISION_COMMANDS[decision], force=args.force
        ):
            _decide_locked(store, args, decision)
    except LockError as e:
        # main() does not catch LockError (cli.py:410-417); cmd_run catches it
        # locally for the same reason.
        sys.exit(str(e))


def _decide_locked(store, args, decision):
    state = store.read_state(args.id)
    note = args.message or ""
    decision_events = [
        event
        for event in store.read_events(args.id)
        if event.get("type") == "human_decision"
    ]
    if decision_events:
        recorded = decision_events[0]
        recorded_decision = recorded.get("content")
        recorded_note = recorded.get("note", "")
        if any(
            event.get("content") != recorded_decision
            or event.get("note", "") != recorded_note
            for event in decision_events[1:]
        ):
            sys.exit("conflicting human decisions already exist in transcript")
        if (decision, note) != (recorded_decision, recorded_note):
            sys.exit(
                f"debate is already {recorded_decision}; requested decision conflicts"
            )
        state["human_decision"] = {
            "decision": recorded_decision,
            "note": recorded_note,
        }
        state["status"] = recorded_decision
        store.write_state(args.id, state)
        _write_derived_views(store, args.id, state)
        store.rebuild_index()
        print(f"{args.id}: {recorded_decision}")
        return
    if state["status"] not in ("awaiting_human", "no_consensus"):
        sys.exit(
            f"debate is '{state['status']}' — "
            "nothing is awaiting a human decision"
        )
    state["human_decision"] = {"decision": decision, "note": note}
    state["status"] = decision
    store.append_event(
        args.id,
        {
            "round": state["round"],
            "phase": "human",
            "agent": "human",
            "type": "human_decision",
            "content": decision,
            "note": note,
        },
    )
    store.write_state(args.id, state)
    _write_derived_views(store, args.id, state)
    store.rebuild_index()
    print(f"{args.id}: {decision}")


def cmd_agents(args):
    specs = registry.load_agent_specs(args.config)
    for spec in specs:
        if not spec.enabled:
            verdict = "disabled"
        else:
            problem = registry.spec_problem(spec)
            if problem:
                verdict = f"NOT READY — {problem}"
            elif spec.backend == "auto":
                verdict = f"ready ({registry.resolve_backend(spec)})"
            else:
                verdict = "ready"
        print(f"{spec.name:<12} {spec.backend:<4} {verdict}")
    if args.ping:
        ready = [
            s for s in specs if s.enabled and registry.spec_problem(s) is None
        ]
        for agent in registry.build_agents(ready):
            try:
                agent.ask("Reply with the single word: pong", task=models.FAST)
                print(f"{agent.name}: ping ok")
            except Exception as e:
                print(f"{agent.name}: ping FAILED — {e}")


def make_server(port: int, directory: str) -> http.server.ThreadingHTTPServer:
    viewer_html = (
        resources.files("debatelab").joinpath("viewer/index.html").read_text()
    )

    class Handler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            if self.path.split("?")[0] in ("/", "/index.html"):
                body = viewer_html.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                super().do_GET()

        def log_message(self, *args):
            pass

    handler = functools.partial(Handler, directory=directory)
    return http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)


def cmd_serve(args):
    root = get_store().root
    root.mkdir(parents=True, exist_ok=True)
    srv = make_server(args.port, str(root))
    print(
        f"viewer at http://127.0.0.1:{srv.server_address[1]}/ "
        "(Ctrl-C to stop)"
    )
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="debate", description="Multi-agent AI debate orchestrator"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("new", help="create a debate")
    sp.add_argument("problem")
    sp.add_argument("--context", nargs="*", help="context files to include")
    sp.add_argument(
        "--repo",
        help="ground the debate in this git repository (agents get a "
        "disposable checkout of its current HEAD)",
    )
    sp.set_defaults(fn=cmd_new)

    sp = sub.add_parser("run", help="run debate rounds until consensus or cap")
    sp.add_argument("id")
    sp.add_argument("--max-rounds", type=positive_int, default=None)
    sp.add_argument(
        "--quorum",
        type=quorum_fraction,
        default=None,
        help="fraction of the roster that must accept, e.g. 2/3 (default 2/3)",
    )
    sp.add_argument("--config", default="agents.yaml")
    sp.add_argument(
        "--force",
        action="store_true",
        help="break an existing debate lock (use only if that process is dead)",
    )
    sp.set_defaults(fn=cmd_run)

    sp = sub.add_parser("status", help="show a debate's status")
    sp.add_argument("id")
    sp.set_defaults(fn=cmd_status)

    sp = sub.add_parser("list", help="list all debates")
    sp.set_defaults(fn=cmd_list)

    sp = sub.add_parser("show", help="print a debate's summary")
    sp.add_argument("id")
    sp.set_defaults(fn=cmd_show)

    sp = sub.add_parser("result", help="print the final answer or no-answer result")
    sp.add_argument("id")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_result)

    sp = sub.add_parser(
        "fsck", help="check state.json against a replay of the transcript"
    )
    sp.add_argument("id")
    sp.set_defaults(fn=cmd_fsck)

    sp = sub.add_parser("approve", help="approve the consensus answer")
    sp.add_argument("id")
    sp.add_argument("-m", "--message", default="")
    sp.add_argument(
        "--force",
        action="store_true",
        help="break an existing debate lock (use only if that process is dead)",
    )
    sp.set_defaults(fn=lambda a: cmd_decide(a, "approved"))

    sp = sub.add_parser("reject", help="reject the consensus answer")
    sp.add_argument("id")
    sp.add_argument("-m", "--message", required=True)
    sp.add_argument(
        "--force",
        action="store_true",
        help="break an existing debate lock (use only if that process is dead)",
    )
    sp.set_defaults(fn=lambda a: cmd_decide(a, "rejected"))

    sp = sub.add_parser("agents", help="list configured agents and readiness")
    sp.add_argument("--config", default="agents.yaml")
    sp.add_argument(
        "--ping",
        action="store_true",
        help="send a live test prompt to each ready agent",
    )
    sp.set_defaults(fn=cmd_agents)

    sp = sub.add_parser("serve", help="serve the web viewer")
    sp.add_argument("--port", type=int, default=8080)
    sp.set_defaults(fn=cmd_serve)

    args = parser.parse_args(argv)
    try:
        args.fn(args)
    except registry.ConfigError as e:
        sys.exit(f"config error: {e}")
    except FileNotFoundError as e:
        sys.exit(str(e))
    except ValueError as e:
        sys.exit(str(e))
