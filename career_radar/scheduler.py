"""Built-in scheduler: replaces external cron jobs.

Jobs run in a daemon thread inside the web process at local times in the configured timezone
(collection several times a day, discovery nightly, the optional LLM scout nightly). One run per
job at a time; a missed time (app was down) does not replay a backlog, it simply runs at the
next scheduled time. ``run_now`` starts a job immediately in the background.
"""
from __future__ import annotations

import logging
import re
import threading
import time as _time
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

log = logging.getLogger("career-radar.scheduler")
_TIME = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def parse_times(values):
    """Valid unique 'HH:MM' strings, sorted; invalid entries raise ValueError."""
    out = set()
    for value in values or []:
        value = str(value).strip()
        if not _TIME.match(value):
            raise ValueError(f"Invalid time {value!r}; use 24-hour HH:MM")
        out.add(value)
    return sorted(out)


def next_run(times, now, tz):
    """Next datetime (aware) strictly after ``now`` for local 'HH:MM' ``times``."""
    local = now.astimezone(tz)
    candidates = []
    for day in (0, 1, 2):
        date = local.date() + timedelta(days=day)
        for value in times:
            hour, minute = map(int, value.split(":"))
            moment = datetime.combine(date, time(hour, minute), tz)
            if moment > now:
                candidates.append(moment)
    return min(candidates) if candidates else None


class Scheduler:
    """``jobs`` maps name -> callable(); ``plan`` returns {name: [HH:MM, ...]} (empty = off)."""

    def __init__(self, jobs, plan, *, tz="America/Toronto", on_result=None, clock=None, poll_seconds=20):
        self.jobs, self.plan, self.on_result = jobs, plan, on_result
        self.tz = ZoneInfo(tz) if isinstance(tz, str) else tz
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.poll_seconds = poll_seconds
        self._lock = threading.Lock()
        self._running = {}
        self._last_tick = None
        self._last = {}
        self._stop = threading.Event()
        self._thread = None

    # --- state ---------------------------------------------------------------------------
    def status(self):
        now = self.clock()
        plan = self._plan()
        with self._lock:
            jobs = {}
            for name in self.jobs:
                last = self._last.get(name) or {}
                upcoming = next_run(plan.get(name) or [], now, self.tz)
                jobs[name] = {"times": plan.get(name) or [], "next_run": upcoming.isoformat() if upcoming else None,
                              "running": name in self._running, "last_run": last.get("finished_at"),
                              "last_ok": last.get("ok"), "last_summary": last.get("summary")}
            return {"running": bool(self._thread and self._thread.is_alive()), "timezone": str(self.tz), "jobs": jobs}

    def remember(self, name, result):
        with self._lock:
            self._last[name] = result

    def is_running(self, name):
        with self._lock:
            return name in self._running

    def _plan(self):
        try:
            plan = self.plan() or {}
        except Exception:  # A bad settings file must not stop the scheduler thread.
            log.exception("Could not read the schedule")
            return {}
        clean = {}
        for name in self.jobs:
            try:
                clean[name] = parse_times(plan.get(name) or [])
            except ValueError:
                log.warning("Ignoring invalid schedule for %s", name)
                clean[name] = []
        return clean

    # --- running ---------------------------------------------------------------------------
    def run_now(self, name, *, wait=False):
        """Start ``name`` in a background thread. Returns False when it is already running."""
        if name not in self.jobs:
            raise KeyError(name)
        with self._lock:
            if name in self._running:
                return False
            self._running[name] = self.clock()
        thread = threading.Thread(target=self._execute, args=(name,), daemon=True, name=f"career-{name}")
        thread.start()
        if wait:
            thread.join()
        return True

    def _execute(self, name):
        started = self.clock()
        result = {"started_at": started.isoformat(), "ok": False, "summary": None}
        try:
            outcome = self.jobs[name]()
            ok, summary = (outcome if isinstance(outcome, tuple) else (True, outcome))
            result.update(ok=bool(ok), summary=str(summary)[:500] if summary is not None else None)
        except Exception as exc:  # Every failure is recorded, never raised into the thread.
            log.exception("Scheduled job %s failed", name)
            result.update(ok=False, summary=f"{type(exc).__name__}: {exc}"[:500])
        finally:
            result["finished_at"] = self.clock().isoformat()
            with self._lock:
                self._running.pop(name, None)
                self._last[name] = result
            if self.on_result:
                try:
                    self.on_result(name, result)
                except Exception:
                    log.exception("Could not record the result of %s", name)

    def tick(self):
        """Start every job with a scheduled time in (previous tick, now]. Called by the loop.

        The first tick only records the time, so a restart never replays missed runs; a long
        gap (suspend) runs each job at most once. Schedule edits apply on the next tick."""
        now = self.clock()
        previous, self._last_tick = self._last_tick, now
        if previous is None:
            return
        plan = self._plan()
        for name in self.jobs:
            due = next_run(plan.get(name) or [], previous, self.tz)
            if due is not None and due <= now:
                self.run_now(name)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="career-scheduler")
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("Scheduler tick failed")
            self._stop.wait(self.poll_seconds)
