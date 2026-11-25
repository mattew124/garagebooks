"""GET every page the home page links to, from inside the app's container (past the proxy's login).

Run from the checkout whose stack it is: see SKILL.md. Exits 1 if any page fails.
"""

import html
import os
import re
import sys
import urllib.request

ORIGIN = "http://127.0.0.1:8080"
base = os.environ.get("BASE_PATH", "").rstrip("/") + "/"


def get(path):
    with urllib.request.urlopen(ORIGIN + path, timeout=60) as response:  # nosec B310 - fixed http origin
        return response.read()


home = get(base).decode()
links = {html.unescape(link) for link in re.findall(r'href="([^"#]+)"', home)}
# This app's own pages: not Grafana's (another container), not static files.
pages = [base] + sorted(
    link
    for link in links - {base}
    if link.startswith(base)
    and not link.startswith((base + "grafana", base + "static"))
)

failed = 0
for page in pages:
    try:
        get(page)
    except OSError as error:  # HTTPError (4xx, 5xx) and timeouts are both OSErrors
        failed += 1
        print(f"FAIL {page}: {error}")
print(f"{len(pages) - failed} of {len(pages)} pages OK")
sys.exit(1 if failed else 0)
