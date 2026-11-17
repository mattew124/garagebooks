"""Words for the scheduled jobs, for the Settings page."""

from datetime import timedelta


def every_in_words(every):
    """A job's interval as a phrase: 'every hour', 'every 6 hours', 'every 90 minutes'."""
    minutes = int(every / timedelta(minutes=1))
    for size, unit in ((1440, "day"), (60, "hour"), (1, "minute")):
        if minutes % size == 0:
            count = minutes // size
            return f"every {unit}" if count == 1 else f"every {count} {unit}s"
    return f"every {minutes} minutes"      # not reached: the loop ends at 1, which divides everything


def state(job):
    """How the last run went, as one word for the page: never, running, ok or failed."""
    if job["last_started_at"] is None:
        return "never"
    if job["last_finished_at"] is None:
        return "running"
    return "ok" if job["last_ok"] else "failed"
