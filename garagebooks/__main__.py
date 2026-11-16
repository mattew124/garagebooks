"""`python -m garagebooks worker`: run the scheduled jobs in a process of their own.

For a second container from the same image, with SCHEDULER=worker set on both so the web process
leaves the jobs to this one. It starts the app the way the web process does (same settings, same
database, same schema check) and then only runs the jobs: it listens on no port.
"""

import signal
import sys
import threading

from . import create_app

USAGE = "usage: python -m garagebooks worker    (run the scheduled jobs; see the Scheduled jobs page of the docs)"


def main(argv):
    if argv != ["worker"]:
        print(USAGE, file=sys.stderr)
        return 2
    app = create_app(background_jobs=False)
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    app.extensions["scheduler"].run_forever(stop)
    return 0


if __name__ == "__main__":     # pragma: no cover - the two lines that hand over to main()
    sys.exit(main(sys.argv[1:]))
