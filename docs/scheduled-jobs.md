# Scheduled jobs

Some work has to happen on a timetable, whether or not anyone has the app open. GarageBooks has a
small scheduler for it. It needs nothing to set up: by default it runs inside the app, and you can
move it to a container of its own.

## The jobs

| Job | Runs | What it does |
| --- | --- | --- |
| `teslamate` | every hour | Looks at TeslaMate again: adds its new cars, and holds its figures back or lets them through (see [TeslaMate version](reference.md#teslamate-version)). With no TeslaMate it records that, and nothing else. |
| `dashboard` | every 6 hours | Rewrites the Grafana dashboard file, so its time range and panels follow the data. |
| `ato_rates` | every 7 days | Reads the income tax brackets and the home charging rate for an electric car from the ATO's pages (see [Live rates from the ATO](lease-calculations.md#live-rates-from-the-ato)). If a page can't be read (an install with no internet) or has changed shape, it fails and keeps the rates it has. |
| `onedrive_folders` | every hour | Brings the app's listing of your [OneDrive](onedrive.md) folders and PDFs up to date; the first one lists the whole drive and takes a few minutes if it is large. Says *no OneDrive connected* until OneDrive is. |
| `mail` | every 30 minutes | Looks in each car's [mailbox folder](mailbox.md) and counts the emails not yet imported, for the Invoice storage page and a notice. It only reads. Says *no mailbox connected* until the mailbox is. |
| `notifications` | every 15 minutes | Sends the [notices](notifications.md) you asked for: a reminder coming due, the Microsoft client secret nearing its expiry, the OneDrive or mailbox sign-in lost. Does nothing, and says *off*, until an address is set on the Settings page. |

A job that was missed (the app was stopped for a day) runs once when something is running again, not
once for every time it was missed. A new job runs the first time the scheduler looks, within a
minute of the app starting.

## Where they run

Set `SCHEDULER` in `.env` and restart:

| Value | Meaning |
| --- | --- |
| `app` | The default. A background thread in the app container runs the jobs. |
| `worker` | The app runs none. A second container from the same image does (below), so the web process stays light. |
| `off` | Nobody runs them. The dashboard is then only rewritten when the app starts, and nothing looks at TeslaMate again between restarts. |

Anything else is reported in the log and treated as `app`.

## Running the worker container

The worker is in `docker-compose.yml` under a compose profile, so it does not start unless you ask.
Set `SCHEDULER=worker` in `.env` (so the app leaves the jobs to it), then start both:

```bash
docker compose --profile worker up -d
```

The worker is the same image started with `python3 -m garagebooks worker`. It listens on no port,
has the same read-only filesystem and dropped capabilities as the app, reads the same `.env`, and
writes the dashboard into the same `dashboards` volume. If you use
[`docker-compose.invoices.yml`](local-folder.md), it gets the invoice folder too. It stops promptly
when told to (`docker stop`, `docker compose down`) and the restart policy starts it again if it
dies.

If you set `SCHEDULER=worker` but never start the worker, nothing runs the jobs: the Settings page
says so (each job stays at *Not run yet*, or its last run gets older).

To go back, remove `SCHEDULER` from `.env` (or set it to `app`), then `docker compose up -d`. A
worker container left over is stopped with `docker compose --profile worker down`.

## How processes share the work

Each job has one row in the `garagebooks.job` table: when it last started and finished, and how it
went. Before running a job, a process claims it with a single `UPDATE` that only succeeds if the job is
due, so when several processes look at once (the app's two web workers, or the app and a worker
container) exactly one of them runs it. The others skip it. You can therefore run more than one
process without the jobs running twice.

A job's own changes are saved only if it finishes. If it fails, they are undone.

## Where to see them

**Settings**, in the **Scheduled jobs** section, lists each job: what it does, how often, when it last
ran (in your timezone), how it went, and when it is next due. The section also says where the jobs
run. It is read-only.

<!-- screenshot: settings-scheduled-jobs - The Scheduled jobs section of the Settings page, showing both jobs after a run -->

| Shown | Meaning |
| --- | --- |
| Not run yet | The scheduler has not got to it. |
| OK | The last run finished. A few words say what it did. |
| Failed | The last run failed, with the reason. |
| Running, or interrupted | It started and never reported finishing: it is running now, or the container stopped part-way. It is run again at its next turn. |

## When a job fails

The failure is recorded and shown on Settings, the log has the details (`docker compose logs
garagebooks`, or the worker's), and the other jobs carry on. A failed job is not retried at once: it
waits for its next turn, so a fault does not turn into a loop.

## Restricted database role

If you use the least-privilege role from `scripts/provision-db.sh` with `SKIP_MIGRATE=1`, run the script
again after upgrading: the scheduler needs the grant on the `garagebooks.job` table that this
version adds.
