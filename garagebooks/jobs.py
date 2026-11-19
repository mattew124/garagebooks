"""Scheduled jobs: periodic work, run by the web process or by a worker from the same image.

A job is a name, how often it runs, and a function. Every so often the scheduler looks for the
jobs that are due and runs them, one after another. Nothing here knows what a job does.

    scheduler.add("example", timedelta(hours=1), run, "What it does, for the Settings page")

    def run(conn):          # an open connection: committed if this returns, rolled back if it raises
        ...
        return "3 sent"     # optional: a few words for the page

One row per job in garagebooks.job says when it last started and finished and how it went. That
row is also how several processes share the work: a job that is due is claimed with a single
UPDATE, so two web workers (or the app and a worker container) never both run it. A job that was
missed while nothing was running runs once when something is, not once for each time missed. One
that fails waits for its next turn.

Where it runs is the installer's choice (SCHEDULER):
  app      a background thread in each web worker (the default: nothing to set up)
  worker   the web process runs nothing; a second container from the same image does
           (`python -m garagebooks worker`), so the web process stays light
  off      nobody runs them
"""

import re
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import psycopg

MODES = ("app", "worker", "off")
NAME = re.compile(r"[a-z][a-z0-9_]{0,39}")      # the same rule as the table's CHECK
SHORTEST = timedelta(minutes=1)
MESSAGE_LENGTH = 500                            # the same limit as the table's CHECK
TICK_SECONDS = 60                               # how often the loop looks for work
START_DELAY_SECONDS = 20                        # let the app finish starting before the first look


class JobError(Exception):
    """A job failed, and says why in words meant for the owner: the message is shown on a page.
    Anything else a job raises is logged in full and shown only by its kind, because the text of an
    unexpected error may hold an address or a token."""


@dataclass(frozen=True)
class Job:
    name: str
    every: timedelta
    run: object               # callable(conn) -> str | None
    description: str = ""


def mode(env):
    """Where the jobs run, from SCHEDULER. -> one of MODES. Raises ValueError for anything else."""
    value = (env.get("SCHEDULER") or "app").strip().lower()
    if value not in MODES:
        raise ValueError(f"SCHEDULER must be one of {', '.join(MODES)} (it is {value!r}).")
    return value


class Scheduler:
    def __init__(self, connect, log, clock=None):
        self.connect, self.log = connect, log
        self.clock = clock or (lambda: datetime.now(UTC))
        self.jobs = {}

    def add(self, name, every, run, description=""):
        if not NAME.fullmatch(name or ""):
            raise ValueError(f"A job's name is lower-case letters, digits and underscores (not {name!r}).")
        if name in self.jobs:
            raise ValueError(f"There is already a job called {name}.")
        if every < SHORTEST:
            raise ValueError(f"The job {name} would run more often than once a minute.")
        self.jobs[name] = Job(name, every, run, description)

    def _claim(self, conn, job, now):
        """Take this job if it is due: one UPDATE, so of several processes asking at once only one
        gets it. -> whether it is ours to run."""
        with conn.transaction():
            conn.execute("INSERT INTO garagebooks.job (name) VALUES (%s) ON CONFLICT (name) DO NOTHING", (job.name,))
            return conn.execute(
                "UPDATE garagebooks.job SET last_started_at = %(now)s, last_finished_at = NULL, last_ok = NULL,"
                " last_message = NULL WHERE name = %(name)s"
                " AND (last_started_at IS NULL OR last_started_at <= %(now)s - %(every)s) RETURNING name",
                {"name": job.name, "now": now, "every": job.every}).fetchone() is not None

    def _run(self, conn, job):
        """-> (ok, message). The job's own writes are committed only when it returns."""
        try:
            with conn.transaction():
                message = job.run(conn)
            return True, message if isinstance(message, str) else None
        except JobError as exc:
            self.log.warning("job %s: %s", job.name, exc)
            return False, str(exc)
        except Exception as exc:     # noqa: BLE001 - a job may fail in any way; the loop and the other jobs carry on
            self.log.exception("job %s failed", job.name)
            return False, f"{type(exc).__name__}: see the log"

    def run_due(self):
        """Run every job that is due, in the order they were added. -> the names that ran."""
        ran = []
        try:
            conn = self.connect()
        except psycopg.Error as exc:
            self.log.warning("scheduled jobs: no database (%s); trying again later", str(exc).strip())
            return ran
        with conn:
            conn.autocommit = True      # each step below is its own transaction
            for job in list(self.jobs.values()):
                now = self.clock()
                if not self._claim(conn, job, now):
                    continue
                self.log.debug("job %s: starting", job.name)
                ok, message = self._run(conn, job)
                conn.execute(
                    "UPDATE garagebooks.job SET last_finished_at = %s, last_ok = %s, last_message = %s WHERE name = %s",
                    (self.clock(), ok, message[:MESSAGE_LENGTH] if message else None, job.name))
                if ok:
                    self.log.info("job %s: done%s", job.name, f" ({message})" if message else "")
                ran.append(job.name)
        return ran

    def status(self, conn):
        """Each job with its last run, for a page: name, description, every, last_started_at,
        last_finished_at, last_ok, last_message and next_due_at (None until it has run once)."""
        saved = {r["name"]: r for r in conn.execute("SELECT * FROM garagebooks.job").fetchall()}
        listed = []
        for job in sorted(self.jobs.values(), key=lambda j: j.name):
            last = saved.get(job.name) or {}
            started = last.get("last_started_at")
            listed.append({
                "name": job.name, "description": job.description, "every": job.every,
                "last_started_at": started, "last_finished_at": last.get("last_finished_at"),
                "last_ok": last.get("last_ok"), "last_message": last.get("last_message"),
                "next_due_at": started + job.every if started else None,
            })
        return listed

    def run_forever(self, stop, tick=TICK_SECONDS, delay=START_DELAY_SECONDS):
        """Look for work every `tick` seconds until `stop` (a threading.Event) is set."""
        if stop.wait(delay):
            return
        while True:
            try:
                self.run_due()
            except Exception:       # noqa: BLE001 - whatever went wrong, the next look may go better
                self.log.exception("scheduled jobs: a pass failed; trying again later")
            if stop.wait(tick):
                return

    def start_thread(self):
        """Run in the background of this process. -> the Event that stops it."""
        stop = threading.Event()
        threading.Thread(target=self.run_forever, args=(stop,), name="scheduled-jobs", daemon=True).start()
        return stop
