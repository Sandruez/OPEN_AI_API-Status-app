"""
OpenAI Status Tracker
======================
Monitors the OpenAI status Atom feed for new incidents, outages, and degradations.
Exposes a lightweight HTTP server so Railway can assign a public URL.

Visit /         → live dashboard (auto-refreshes every 30s)
Visit /logs     → raw JSON of all captured events
Visit /health   → health check
"""

import os
import re
import signal
import sys
import threading
import time
from datetime import datetime, timezone

import feedparser
import requests
from flask import Flask, jsonify

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

FEED_URL = "https://status.openai.com/history.atom"

KNOWN_PRODUCTS = [
    "Chat Completions", "Responses API", "Completions", "Embeddings",
    "Fine-tuning", "Images", "Audio", "Assistants", "Batch API",
    "Files API", "Moderation", "Realtime API", "Vector Stores",
    "Code Interpreter", "Function Calling", "Structured Outputs", "OpenAI API",
]

INCIDENT_KEYWORDS = [
    "incident", "outage", "degraded", "degradation", "disruption",
    "partial", "investigating", "identified", "monitoring", "resolved",
    "update", "elevated", "error", "latency", "unavailable", "down", "failure",
]

DEFAULT_POLL_INTERVAL = 60

# ---------------------------------------------------------------------------
# Shared in-memory log
# ---------------------------------------------------------------------------

_log_lock = threading.Lock()
_incident_log: list[dict] = []


def _append_log(entry: dict):
    with _log_lock:
        _incident_log.append(entry)
        if len(_incident_log) > 500:
            _incident_log.pop(0)


def _read_log() -> list[dict]:
    with _log_lock:
        return list(_incident_log)


# ---------------------------------------------------------------------------
# Feed tracker
# ---------------------------------------------------------------------------

class FeedTracker:
    def __init__(self, url: str, poll_interval: int):
        self.url = url
        self.poll_interval = poll_interval
        self._seen_ids: set[str] = set()
        self._etag: str | None = None
        self._last_modified: str | None = None
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "OpenAIStatusTracker/1.0"})

    def _fetch_feed(self):
        headers = {}
        if self._etag:
            headers["If-None-Match"] = self._etag
        if self._last_modified:
            headers["If-Modified-Since"] = self._last_modified
        try:
            r = self._session.get(self.url, headers=headers, timeout=15)
        except requests.RequestException as exc:
            print(f"[{_now()}] ERROR: {exc}", file=sys.stderr)
            return None
        if r.status_code == 304:
            return None
        if r.status_code != 200:
            print(f"[{_now()}] ERROR: HTTP {r.status_code}", file=sys.stderr)
            return None
        self._etag = r.headers.get("ETag")
        self._last_modified = r.headers.get("Last-Modified")
        return feedparser.parse(r.text)

    def check(self):
        feed = self._fetch_feed()
        if feed is None:
            return
        new_entries = [e for e in feed.entries if e.get("id") not in self._seen_ids]
        for entry in reversed(new_entries):
            self._seen_ids.add(entry.get("id", ""))
            self._process_entry(entry)

    def _process_entry(self, entry):
        title = entry.get("title", "")
        body = entry.get("summary", "")
        if not body and entry.get("content"):
            body = entry["content"][0].get("value", "")
        combined = f"{title} {body}".lower()
        if not any(kw in combined for kw in INCIDENT_KEYWORDS):
            return
        product = _extract_product(title, body)
        timestamp = _entry_timestamp(entry)
        status_message = _clean_html(body) or title
        record = {"timestamp": timestamp, "product": product, "status": status_message}
        _append_log(record)
        print(f"[{timestamp}] Product: {product}\nStatus: {status_message}\n{'─'*60}")

    def run(self):
        print(f"[{_now()}] Tracker started — polling every {self.poll_interval}s")
        self.check()
        while True:
            time.sleep(self.poll_interval)
            self.check()


# ---------------------------------------------------------------------------
# Flask web server
# ---------------------------------------------------------------------------

app = Flask(__name__)


@app.route("/health")
def health():
    return jsonify({"status": "ok", "feed": FEED_URL}), 200


@app.route("/logs")
def logs_json():
    return jsonify(_read_log()), 200


@app.route("/")
def dashboard():
    entries = _read_log()
    rows_html = ""
    if entries:
        for e in reversed(entries):
            rows_html += f"""
            <div class="card">
              <div class="meta">
                <span class="product">{e['product']}</span>
                <span class="ts">{e['timestamp']} UTC</span>
              </div>
              <div class="status">{e['status']}</div>
            </div>"""
    else:
        rows_html = '<p class="empty">No incidents detected yet. Checking every 60s…</p>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="30">
  <title>OpenAI Status Tracker</title>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0d0d0d;color:#e8e8e8;padding:2rem}}
    h1{{font-size:1.4rem;font-weight:600;margin-bottom:.25rem}}
    .subtitle{{font-size:.85rem;color:#666;margin-bottom:2rem}}
    .card{{background:#1a1a1a;border:1px solid #2a2a2a;border-radius:8px;padding:1rem 1.25rem;margin-bottom:1rem}}
    .meta{{display:flex;justify-content:space-between;margin-bottom:.5rem}}
    .product{{font-weight:600;color:#10a37f;font-size:.9rem}}
    .ts{{font-size:.8rem;color:#555}}
    .status{{font-size:.9rem;color:#ccc;line-height:1.5}}
    .empty{{color:#555;font-size:.9rem}}
    .badge{{display:inline-block;background:#10a37f22;color:#10a37f;border-radius:4px;padding:2px 8px;font-size:.75rem;margin-left:.5rem;vertical-align:middle}}
  </style>
</head>
<body>
  <h1>OpenAI Status Tracker <span class="badge">LIVE</span></h1>
  <p class="subtitle">Auto-refreshes every 30s &nbsp;·&nbsp; {len(entries)} event(s) captured</p>
  {rows_html}
</body>
</html>"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _entry_timestamp(entry) -> str:
    ts = entry.get("updated_parsed") or entry.get("published_parsed")
    if ts:
        return datetime(*ts[:6], tzinfo=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return _now()


def _extract_product(title: str, body: str) -> str:
    combined = f"{title} {body}"
    for product in KNOWN_PRODUCTS:
        if product.lower() in combined.lower():
            return f"OpenAI API - {product}"
    return "OpenAI API"


def _clean_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:400] + ("…" if len(text) > 400 else "")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))

    interval = int(os.environ.get("POLL_INTERVAL", DEFAULT_POLL_INTERVAL))
    tracker = FeedTracker(FEED_URL, interval)
    t = threading.Thread(target=tracker.run, daemon=True)
    t.start()

    port = int(os.environ.get("PORT", 8080))
    print(f"[{_now()}] Web server starting on port {port}")
    app.run(host="0.0.0.0", port=port)
