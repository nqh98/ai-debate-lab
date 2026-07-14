"""The `debate` command-line interface."""
import argparse
import functools
import http.server
import sys
from importlib import resources
from pathlib import Path

from .agents import models, registry
from .store import DebateStore, render_summary


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def get_store() -> DebateStore:
    return DebateStore(Path.cwd() / "debates")


def cmd_new(args):
    store = get_store()
    contexts = []
    for f in args.context or []:
        p = Path(f)
        contexts.append((p.name, p.read_text()))
    title = args.problem.strip().splitlines()[0][:60]
    print(store.create(title, args.problem, contexts))


def cmd_run(args):
    from .orchestrator import Orchestrator

    store = get_store()
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
    status = orch.run(args.id, max_rounds=args.max_rounds)
    print(f"final status: {status}")
    if status in ("awaiting_human", "no_consensus"):
        print(
            f"review with `debate show {args.id}`, then "
            f"`debate approve {args.id}` or `debate reject {args.id} -m ...`"
        )


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


def cmd_decide(args, decision):
    store = get_store()
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
        store.write_summary(args.id, render_summary(state))
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
    store.write_summary(args.id, render_summary(state))
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
    srv = make_server(args.port, str(Path.cwd()))
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
    sp.set_defaults(fn=cmd_new)

    sp = sub.add_parser("run", help="run debate rounds until consensus or cap")
    sp.add_argument("id")
    sp.add_argument("--max-rounds", type=positive_int, default=None)
    sp.add_argument("--config", default="agents.yaml")
    sp.set_defaults(fn=cmd_run)

    sp = sub.add_parser("status", help="show a debate's status")
    sp.add_argument("id")
    sp.set_defaults(fn=cmd_status)

    sp = sub.add_parser("list", help="list all debates")
    sp.set_defaults(fn=cmd_list)

    sp = sub.add_parser("show", help="print a debate's summary")
    sp.add_argument("id")
    sp.set_defaults(fn=cmd_show)

    sp = sub.add_parser("approve", help="approve the consensus answer")
    sp.add_argument("id")
    sp.add_argument("-m", "--message", default="")
    sp.set_defaults(fn=lambda a: cmd_decide(a, "approved"))

    sp = sub.add_parser("reject", help="reject the consensus answer")
    sp.add_argument("id")
    sp.add_argument("-m", "--message", required=True)
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
