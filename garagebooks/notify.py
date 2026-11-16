"""Sending a notice: one POST to an address the owner chose (an ntfy topic, an Apprise API, a Home
Assistant webhook). Standard library only.

The address is a secret for most of these services (a topic name, a webhook id), so it never leaves
this module except in the request itself: it is not logged (only its host is), not put in an error
message, not shown on a page. A failure says what happened in words ("answered HTTP 403", "could not
be reached") and nothing else. What the other end answers is never read beyond a small cap and never
shown. Redirects are not followed: a notice goes where the owner said, and nowhere else.

The owner chooses this address and the app has no login of its own, so a host on the local network (a
self-hosted ntfy) is allowed; only http and https are.
"""

import base64
import json
import logging
import re
import urllib.error
import urllib.request
from urllib.parse import urlsplit, urlunsplit

from .jobs import JobError

log = logging.getLogger("garagebooks.notify")

FORMATS = {"ntfy": "ntfy", "json": "JSON (Apprise API, Home Assistant)"}
DEFAULT_FORMAT = "ntfy"
TIMEOUT_SECONDS = 10
READ_CAP = 1024              # of the answer: enough to finish the exchange, never kept or shown


def host_of(url):
    """The host (and port) of an address, to name it in a log or on a page without giving the rest away."""
    try:
        parts = urlsplit(url or "")
        return (parts.netloc.rpartition("@")[2] or "an address") if parts.hostname else "an address"
    except ValueError:
        return "an address"


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _post(url, headers, body):
    """The real POST. -> the HTTP status. Raises OSError (or a subclass) when nothing answered."""
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")  # nosec B310 - scheme checked in send()
    opener = urllib.request.build_opener(_NoRedirects)
    try:
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:  # nosec B310 - http(s) only, checked in send()
            response.read(READ_CAP)
            return response.status
    except urllib.error.HTTPError as exc:       # a status other than 2xx, a redirect included
        exc.close()
        return exc.code


def _header_text(text):
    """A value fit for an HTTP header: one line, and ASCII (RFC 2047 encoded words when it is not, which
    is what ntfy documents for a non-ASCII title)."""
    one_line = re.sub(r"[\x00-\x1f\x7f]+", " ", str(text)).strip()
    if one_line.isascii():
        return one_line
    return "=?UTF-8?B?" + base64.b64encode(one_line.encode()).decode() + "?="


def _tag(tag):
    return re.sub(r"[^a-z0-9_+-]", "", str(tag).lower())


def send(url, fmt, title, message, tags=(), fetch=None):
    """Post one notice. `fetch(url, headers, body) -> status` stands in for the network in tests.
    Raises jobs.JobError, in words for the owner, when it didn't arrive."""
    fetch = fetch or _post
    try:
        parts = urlsplit(url or "")
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("scheme")
        headers = {}
        if parts.username is not None:      # https://user:password@host/topic: sent as Basic auth, not in the address
            login = f"{parts.username}:{parts.password or ''}".encode()
            headers["Authorization"] = "Basic " + base64.b64encode(login).decode()
            url = urlunsplit(parts._replace(netloc=parts.netloc.rpartition("@")[2]))
    except ValueError:
        raise JobError("The notification address is not a full http:// or https:// address.") from None
    title, message = str(title), str(message)
    if fmt == "json":
        headers["Content-Type"] = "application/json"
        body = json.dumps({"title": title, "message": message, "body": message}).encode()
    else:
        headers["Content-Type"] = "text/plain; charset=utf-8"
        headers["Title"] = _header_text(title)
        clean = [t for t in (_tag(t) for t in tags) if t]
        if clean:
            headers["Tags"] = ",".join(clean)
        body = message.encode()
    host = host_of(url)
    try:
        status = fetch(url, headers, body)
    except (OSError, ValueError):       # an error's text can hold the address: say only that it failed
        log.warning("notice to %s: could not be reached", host)
        raise JobError("The notification service could not be reached.") from None
    if not 200 <= status < 300:
        log.warning("notice to %s: answered HTTP %s", host, status)
        raise JobError(f"The notification service answered HTTP {status}"
                       + (" (a redirect: use the address it redirects to)" if 300 <= status < 400 else "") + ".")
    log.info("notice sent to %s", host)
