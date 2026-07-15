"""Live run status: heartbeat lines, stall alerts, and live.json.

With no proactive cancellation (spec §3), visibility does the job
cancellation used to: a background thread ticks every HEARTBEAT_INTERVAL
seconds, prints one line per in-flight agent call, rings the terminal
bell once when a call crosses its stall threshold, and atomically
rewrites debates/<id>/live.json for the viewer. Nothing here cancels
anything, ever (spec §6)."""
import threading
import time
from datetime import datetime, timezone

HEARTBEAT_INTERVAL = 60


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _minutes(seconds: float) -> str:
    return f"{int(seconds // 60)}m"


class LiveStatus:
    def __init__(self, store, debate_id, progress,
                 interval=HEARTBEAT_INTERVAL, clock=time.monotonic):
        self.store = store
        self.debate_id = debate_id
        self.progress = progress
        self.interval = interval
        self.clock = clock
        self._lock = threading.Lock()
        self._calls = {}
        self._round = None
        self._phase = None
        self._stop = threading.Event()
        self._thread = None

    def set_phase(self, round_, phase):
        with self._lock:
            self._round, self._phase = round_, phase

    def call_started(self, agent, task, stall_after):
        with self._lock:
            self._calls[agent] = {
                "task": task,
                "started_iso": _now_iso(),
                "started": self.clock(),
                "stall_after": stall_after,
                "alerted": False,
            }

    def call_finished(self, agent):
        with self._lock:
            self._calls.pop(agent, None)

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None
        self.store.delete_live(self.debate_id)

    def _loop(self):
        while not self._stop.wait(self.interval):
            self.tick()

    def tick(self):
        with self._lock:
            now = self.clock()
            calls, lines = [], []
            for agent in sorted(self._calls):
                c = self._calls[agent]
                elapsed = now - c["started"]
                threshold = c["stall_after"]
                stalled = threshold is not None and elapsed >= threshold
                calls.append({
                    "agent": agent,
                    "task": c["task"],
                    "started": c["started_iso"],
                    "elapsed_s": int(elapsed),
                    "stalled": stalled,
                })
                if stalled and not c["alerted"]:
                    c["alerted"] = True
                    lines.append(
                        f"\a⚠ {agent} · {self._phase} · {_minutes(elapsed)}"
                        f" — exceeded stall threshold "
                        f"({_minutes(threshold)}); still waiting.\n"
                        "  Ctrl-C interrupts; `debate run` resumes from "
                        "the last completed phase."
                    )
                elif stalled:
                    lines.append(
                        f"⚠ {agent} · {self._phase} · {_minutes(elapsed)}"
                        f" — still waiting"
                    )
                else:
                    lines.append(
                        f"⏳ {agent} · {self._phase} · {_minutes(elapsed)}"
                    )
            payload = {
                "updated": _now_iso(),
                "round": self._round,
                "phase": self._phase,
                "calls": calls,
            }
        self.store.write_live(self.debate_id, payload)
        for line in lines:
            self.progress(line)
