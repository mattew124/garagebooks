# Gunicorn reads this from the working directory (/srv in the image).
import os

_level = os.environ.get("LOG_LEVEL", "info").strip().lower()
# An unknown value is reported by the app itself on start; don't also stop gunicorn here.
loglevel = _level if _level in ("debug", "info", "warning", "error") else "info"

# A claims connector drives a browser and can take a minute (garagebooks/claims.py waits up to 150 s). A
# request that outlasts this is killed, so it must be longer than that. It is here and not on the
# command line, which would override it.
timeout = 180
