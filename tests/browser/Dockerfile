# The image ships matching browsers but not the Python package, which is in requirements.txt beside this file.
# Dependabot bumps the two together (the playwright group in .github/dependabot.yml); a test checks they match.
FROM mcr.microsoft.com/playwright/python:v1.65.2-noble
COPY requirements.txt /tmp/requirements.txt
RUN pip install --break-system-packages --no-cache-dir -r /tmp/requirements.txt
