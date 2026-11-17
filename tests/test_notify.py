"""Sending a notice (notify.py): one POST to the address the owner chose, in the shape ntfy or a JSON
webhook (Apprise API, Home Assistant) expects. The address is a secret, so it must never come back out
in an error, and a stand-in for the outside world is used throughout: the only real sockets here are to a
listener on this machine."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from garagebooks import jobs, notify

URL = "https://ntfy.example.invalid/your-secret-topic"


class Recorder:
    def __init__(self, status=200, error=None):
        self.calls, self.status, self.error = [], status, error

    def __call__(self, url, headers, body):
        self.calls.append((url, dict(headers), body))
        if self.error:
            raise self.error
        return self.status


# ---- the two shapes -----------------------------------------------------------------

def test_ntfy_gets_the_message_as_text_with_the_title_and_tags_as_headers():
    rec = Recorder()
    notify.send(URL, "ntfy", "Registration due", "Test car: due in 10 days", tags=("calendar", "warning"), fetch=rec)
    (url, headers, body), = rec.calls
    assert url == URL and body == b"Test car: due in 10 days"
    assert headers["Title"] == "Registration due" and headers["Tags"] == "calendar,warning"
    assert headers["Content-Type"].startswith("text/plain")


def test_a_title_ntfy_cannot_carry_in_a_header_is_encoded_and_never_breaks_the_header():
    rec = Recorder()
    notify.send(URL, "ntfy", "Tyres: Zoë's car\r\nX-Evil: 1", "body", fetch=rec)
    title = rec.calls[0][1]["Title"]
    assert "\r" not in title and "\n" not in title
    assert title.isascii() and title.startswith("=?UTF-8?")        # RFC 2047, which ntfy documents
    assert "X-Evil" not in rec.calls[0][1]


def test_json_carries_title_message_and_body_for_apprise_and_home_assistant():
    rec = Recorder()
    notify.send(URL, "json", "A title", "A message", tags=("car",), fetch=rec)
    (_, headers, body), = rec.calls
    assert headers["Content-Type"] == "application/json"
    assert json.loads(body) == {"title": "A title", "message": "A message", "body": "A message"}


def test_a_login_in_the_address_becomes_basic_auth_and_is_not_sent_in_the_url():
    rec = Recorder()
    notify.send("https://me:pa55@ntfy.example.invalid/topic", "ntfy", "t", "m", fetch=rec)
    url, headers, _ = rec.calls[0]
    assert url == "https://ntfy.example.invalid/topic"
    assert headers["Authorization"] == "Basic bWU6cGE1NQ=="


@pytest.mark.parametrize("url", ["ftp://x.example.invalid/t", "file:///etc/passwd", "javascript:alert(1)", "", "//x/y"])
def test_only_http_and_https_are_ever_posted_to(url):
    rec = Recorder()
    with pytest.raises(jobs.JobError):
        notify.send(url, "ntfy", "t", "m", fetch=rec)
    assert rec.calls == []


# ---- failing without telling the secret ------------------------------------------------

def test_a_refusal_names_the_status_and_not_the_address():
    with pytest.raises(jobs.JobError) as err:
        notify.send(URL, "ntfy", "t", "m", fetch=Recorder(status=403))
    assert "403" in str(err.value) and "your-secret-topic" not in str(err.value) and "ntfy.example" not in str(err.value)


@pytest.mark.parametrize("error", [OSError(f"cannot connect to {URL}"), TimeoutError(URL), ConnectionRefusedError(URL)])
def test_an_unreachable_service_says_so_and_not_where(error):
    with pytest.raises(jobs.JobError) as err:
        notify.send(URL, "ntfy", "t", "m", fetch=Recorder(error=error))
    assert "could not be reached" in str(err.value) and "your-secret-topic" not in str(err.value)
    assert err.value.__cause__ is None and err.value.__suppress_context__


def test_nothing_logs_the_address_only_its_host(caplog):
    caplog.set_level("DEBUG")
    notify.send(URL, "ntfy", "t", "m", fetch=Recorder())
    with pytest.raises(jobs.JobError):
        notify.send(URL, "ntfy", "t", "m", fetch=Recorder(status=500))
    assert "your-secret-topic" not in caplog.text and "ntfy.example.invalid" in caplog.text


def test_the_masked_address_shows_the_host_and_nothing_secret():
    assert notify.host_of("https://me:pa55@ntfy.example.invalid:8443/your-secret-topic?x=1") == "ntfy.example.invalid:8443"
    assert notify.host_of("not a url") == "an address"


# ---- the real POST, to a listener on this machine ------------------------------------------

@pytest.fixture()
def listener():
    seen = []

    class Handler(BaseHTTPRequestHandler):
        reply = (200, {})

        def do_POST(self):
            seen.append((self.path, self.headers, self.rfile.read(int(self.headers.get("Content-Length", 0)))))
            status, headers = type(self).reply
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", Handler, seen
    server.shutdown()
    server.server_close()


def test_a_real_post_arrives_as_ntfy_expects(listener):
    base, _, seen = listener
    notify.send(f"{base}/topic-one", "ntfy", "Hello", "World", tags=("car",))
    (path, headers, body), = seen
    assert path == "/topic-one" and body == b"World" and headers["Title"] == "Hello" and headers["Tags"] == "car"


def test_a_redirect_is_not_followed_and_is_reported_by_its_status(listener):
    base, handler, seen = listener
    handler.reply = (302, {"Location": f"{base}/elsewhere"})
    with pytest.raises(jobs.JobError) as err:
        notify.send(f"{base}/topic", "ntfy", "t", "m")
    assert "302" in str(err.value)
    assert [p for p, _, _ in seen] == ["/topic"]


def test_an_error_page_is_never_shown(listener):
    base, handler, _ = listener
    handler.reply = (500, {})
    with pytest.raises(jobs.JobError) as err:
        notify.send(f"{base}/topic", "ntfy", "t", "m")
    assert "500" in str(err.value)


def test_a_closed_port_is_unreachable_not_a_crash():
    with pytest.raises(jobs.JobError) as err:
        notify.send("http://127.0.0.1:9/topic", "ntfy", "t", "m")
    assert "could not be reached" in str(err.value)
