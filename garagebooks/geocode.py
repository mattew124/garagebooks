"""Where is a fuel receipt's station? Its address, looked up on OpenStreetMap (Nominatim).

Only what invoices.station_queries read off a fuel receipt is sent: the station's street, suburb, state and
postcode. Nothing else from the receipt or the installation goes with it (OpenStreetMap sees the server's address
and a User-Agent naming this app and its version). ADDRESS_LOOKUP=0 switches it off. Nominatim's usage policy
asks for a named User-Agent, at most a request a second and no repeats: answers are kept for the process's life.

An answer is only believed when it is on the street that was asked for. A failed or refused lookup finds nothing,
and the form offers the current location instead.
"""

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

SEARCH_URL = "https://nominatim.openstreetmap.org/search"
TIMEOUT_SECONDS = 5
PAUSE_SECONDS = 1.0
MAX_QUERIES = 4
MAX_BYTES = 256 * 1024
_AU_STATE = re.compile(r", (?:QLD|NSW|VIC|SA|WA|TAS|NT|ACT)(?: \d{4})?$")

log = logging.getLogger("garagebooks.geocode")


def _get(url, headers):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:  # nosec B310 - fixed https address
        return resp.read(MAX_BYTES)


class Lookup:
    def __init__(self, version, enabled=True, fetch=None, pause=PAUSE_SECONDS):
        self.enabled = enabled
        self._agent = f"garagebooks/{version or 'dev'}"
        self._fetch, self._pause = fetch, pause
        self._lock = threading.Lock()
        self._last = float("-inf")
        self._answers = {}

    def find(self, queries):
        """The first of `queries` ("12 Example Road, Suburb, QLD") OpenStreetMap knows.
        -> {"location": "lat, lon", "query": the one that matched, "exact": the building, not just the street},
        or None."""
        if not self.enabled:
            return None
        for query in queries[:MAX_QUERIES]:
            try:
                place = self._ask(query)
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                log.info("address lookup failed: %s", exc)
                return None                  # unreachable or unreadable: the others would fare no better
            found = _believe(place, query)
            if found:
                return found
        return None

    def _ask(self, query):
        with self._lock:
            if query in self._answers:
                return self._answers[query]
            time.sleep(max(0.0, self._last + self._pause - time.monotonic()))
            params = {"q": query, "format": "jsonv2", "limit": "1", "addressdetails": "1"}
            if _AU_STATE.search(query):
                params["countrycodes"] = "au"
            try:
                body = (self._fetch or _get)(f"{SEARCH_URL}?{urllib.parse.urlencode(params)}",
                                             {"User-Agent": self._agent})
            finally:
                self._last = time.monotonic()
            places = json.loads(body)
            if not isinstance(places, list):
                raise ValueError("unexpected reply")
            self._answers[query] = places[0] if places else None
            return self._answers[query]


def _words(text):
    """A street's name without its number and its kind ("12 Sample Creek Rd" -> ["sample", "creek"])."""
    return re.findall(r"[a-z']+", text.lower())[:-1]


def _believe(place, query):
    try:
        address = place["address"]
        lat, lon = float(place["lat"]), float(place["lon"])
        road = address["road"].lower()
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    name = _words(query.split(",")[0])
    if not (abs(lat) <= 90 and abs(lon) <= 180 and name and all(re.search(rf"\b{re.escape(w)}\b", road) for w in name)):
        return None
    return {"location": f"{lat:.6f}, {lon:.6f}", "query": query, "exact": bool(address.get("house_number"))}
