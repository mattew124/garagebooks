"""What the owner is told, and when (roadmap item 8). The `notifications` job calls `run()` every 15
minutes; it does nothing, and says "off", until an address is set on the Settings page.

Each thing is told once: what has been sent is a row in garagebooks.notification, keyed by the event.
The key holds what the event was about, so a reminder whose date or odometer is edited is a new event
and is told again. A reminder that is resolved (or deleted) forgets what was told about it, so reopening
it tells again. A notice is recorded only after it was sent, so a failure is tried again next pass.

Told so far:
  * a reminder coming due ("due soon"), and again when it becomes overdue. The odometer is only what
    `sources` knows (TeslaMate's, or a reading by hand): a reminder whose odometer isn't known is told on
    its date alone, never on a zero. A registration the provider renews is not pushed.
  * the OneDrive client secret 30 days and 7 days before its expiry date, and once it has passed
  * the OneDrive sign-in no longer working
  * TeslaMate newer than this release was tested with (opt-in on Settings), once for each new migration
  * emails waiting to be imported from a car's mailbox folder (what the `mail` job last found), once for
    each newest email, and the mailbox sign-in no longer working
Still to come, with the roadmap items that make them: new invoices waiting for review (item 13) and a
lease budget heading for a shortfall (L3).
"""

import re
from dataclasses import dataclass
from datetime import timedelta

from . import db, jobs, reminders

MAX_PER_PASS = 10               # the rest next time, so a first run on a long list isn't a flood
KEEP = "1 year"                 # how long a sent notice is remembered
SIGNIN_CHECK_EVERY = timedelta(days=1)
SECRET_LEAD_DAYS = (30, 7)      # before the client secret's expiry date
TITLE_LENGTH = 300              # the column's limit

SOON, OVERDUE = "soon", "overdue"


@dataclass(frozen=True)
class Notice:
    key: str
    title: str
    message: str
    tags: tuple = ()


def _day(d):
    return f"{d.day} {d:%b %Y}"


def _plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


def _date_words(days_left):
    if days_left == 0:
        return "today"
    return f"in {_plural(days_left, 'day')}" if days_left > 0 else f"{_plural(-days_left, 'day')} overdue"


def _km_words(km_left):
    if km_left == 0:
        return "due now"
    return f"{abs(km_left):,.0f} km over" if km_left < 0 else f"{max(km_left, 1):,.0f} km to go"


def reminder_notices(conn, teslamate, now):
    """-> (the notices wanted now, the ids of the open stored reminders)."""
    wanted, open_ids = [], set()
    for item in reminders.due_items(conn, teslamate, now.date(), now):
        if item["reminder_id"] is not None:
            open_ids.add(item["reminder_id"])
        status = item["status"]
        if status.code not in (reminders.DUE_SOON, reminders.OVERDUE):
            continue      # upcoming, odometer unknown, or one the provider takes care of
        due_date, due_km = item["due_date"], item["due_km"]
        when = SOON if status.code == reminders.DUE_SOON else OVERDUE
        if item["reminder_id"] is not None:     # what it is due on is in the key: edit either and it is a new event
            key = f"reminder:{item['reminder_id']}:{due_date or '-'}:{'-' if due_km is None else f'{float(due_km):g}'}:{when}"
        else:                                   # from the car's lease: registration or lease end
            key = f"{item['kind']}:{item['car_id']}:{due_date}:{when}"
        parts = []
        if due_date is not None:
            parts.append(f"due {_day(due_date)} ({_date_words(status.days_left)})")
        if due_km is not None and status.km_left is not None:      # an odometer not known says nothing
            parts.append(f"due at {float(due_km):,.0f} km ({_km_words(status.km_left)})")
        message = f"{item['car_name']}: " + "; ".join(parts) + "."
        label = "overdue" if when == OVERDUE else "due soon"
        wanted.append(Notice(key, f"{item['title']}: {label} ({item['car_name']})", message,
                             ("warning",) if when == OVERDUE else ("calendar",)))
    return wanted, open_ids


def secret_notices(account, today):
    """The client secret's expiry date, if one was entered: 30 and 7 days before, and when it has passed."""
    expires = account and account["secret_expires"]
    if not expires:
        return []
    left = (expires - today).days
    where = "Enter a new one on the Invoice storage page before invoices stop uploading."
    if left < 0:
        return [Notice(f"onedrive_secret:{expires}:expired", "OneDrive client secret has expired",
                       f"It expired on {_day(expires)}. Enter a new one on the Invoice storage page, then connect OneDrive again.",
                       ("key",))]
    # The nearest lead reached: one notice, not one for each lead already passed.
    reached = [lead for lead in SECRET_LEAD_DAYS if left <= lead]
    if not reached:
        return []
    lead = min(reached)
    return [Notice(f"onedrive_secret:{expires}:{lead}d", f"OneDrive client secret expires in {_plural(left, 'day')}",
                   f"It expires on {_day(expires)}. {where}", ("key",))]


def signin_notices(account, tz_now):
    lost = account and account["signin_lost_at"]
    if not lost:
        return []
    return [Notice(f"onedrive_signin:{lost.astimezone(tz_now.tzinfo).date()}", "OneDrive sign-in lost",
                   "OneDrive no longer accepts the saved sign-in, so invoices can't be uploaded. "
                   "Connect again on the Invoice storage page.", ("warning",))]


def mail_signin_notices(account, tz_now):
    lost = account and account["mail_signin_lost_at"]
    if not lost:
        return []
    return [Notice(f"mail_signin:{lost.astimezone(tz_now.tzinfo).date()}", "Mailbox sign-in lost",
                   "Outlook no longer accepts the saved mailbox sign-in, so emailed invoices aren't being looked for. "
                   "Connect the mailbox again on the Invoice storage page.", ("warning",))]


def mail_notices(conn):
    """Emails in a car's mailbox folder not yet imported, as the mail job last counted them. Keyed by the
    newest one, so a new email is told again and the same ones are not."""
    found = conn.execute(
        "SELECT f.car_id, f.waiting, f.newest_at, c.name FROM garagebooks.car_mail_folder f "
        "JOIN garagebooks.car c ON c.id = f.car_id WHERE f.waiting > 0 AND f.newest_at IS NOT NULL "
        "ORDER BY f.car_id").fetchall()
    return [Notice(f"mail:{r['car_id']}:{r['newest_at']:%Y%m%dT%H%M%S}",
                   f"{_plural(r['waiting'], 'email')} waiting to import ({r['name'] or 'Car ' + str(r['car_id'])})",
                   "Check and import them from the car's mailbox folder on the Invoice storage page.", ("envelope",))
            for r in found]


def teslamate_notices(teslamate):
    """TeslaMate has moved past what this release was tested with, so its figures are held back."""
    if teslamate.state != "untested" or teslamate.migration is None:
        return []
    return [Notice(f"teslamate:{teslamate.migration}:untested", "TeslaMate is newer than GarageBooks has been tested with",
                   "TeslaMate's odometer and charging figures are held back until GarageBooks is upgraded to a release "
                   "that lists this TeslaMate. Everything you entered is unaffected.", ("warning",))]


def forget_resolved(conn, open_ids):
    """Forget what was told about reminders that are no longer open (resolved or deleted), so one that is
    reopened is told again."""
    for row in conn.execute("SELECT key FROM garagebooks.notification WHERE key LIKE 'reminder:%'").fetchall():
        match = re.match(r"reminder:(\d+):", row["key"])
        if match and int(match.group(1)) not in open_ids:
            conn.execute("DELETE FROM garagebooks.notification WHERE key = %s", (row["key"],))


def run(conn, send, teslamate, now, onedrive=False, signin_check=None):
    """One pass. `send(url, fmt, title, message, tags=...)` is notify.send (or a stand-in), `now` is aware,
    `onedrive` says whether OneDrive is set up in this install, and `signin_check()` is how to find out
    whether the saved sign-in still works (it records a loss itself). -> a few words for the Settings page;
    raises jobs.JobError when the very first send of the pass failed."""
    url, fmt = db.get_notify(conn)
    if not url:
        return "off"
    wanted = []
    account = None
    if onedrive:
        account = db.onedrive_account(conn)
        checked = account and account["signin_checked_at"]
        if (signin_check is not None and account and account["refresh_token"] and not account["signin_lost_at"]
                and (checked is None or checked < now - SIGNIN_CHECK_EVERY)):
            signin_check()      # at most once a day: asking Microsoft is the only way to know
            account = db.onedrive_account(conn)
        wanted += signin_notices(account, now)
        wanted += secret_notices(account, now.date())
        wanted += mail_signin_notices(account, now)
        wanted += mail_notices(conn)
    if db.get_notify_teslamate(conn):
        wanted += teslamate_notices(teslamate)
    found, open_ids = reminder_notices(conn, teslamate, now)
    wanted += found
    forget_resolved(conn, open_ids)
    conn.execute(f"DELETE FROM garagebooks.notification WHERE sent_at < now() - interval '{KEEP}'")
    told = {r["key"] for r in conn.execute("SELECT key FROM garagebooks.notification").fetchall()}
    done = 0
    for notice in [n for n in wanted if n.key not in told][:MAX_PER_PASS]:
        try:
            send(url, fmt, notice.title, notice.message, tags=notice.tags)
        except jobs.JobError as exc:
            if not done:
                raise
            return f"{done} sent, then it stopped: {exc}"      # what was sent stays recorded: the job returns
        conn.execute("INSERT INTO garagebooks.notification (key, title) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING",
                     (notice.key, notice.title[:TITLE_LENGTH]))
        done += 1
    return f"{done} sent" if done else "nothing new"
