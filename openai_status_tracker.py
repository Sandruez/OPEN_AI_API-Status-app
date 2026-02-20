"""
OpenAI Status Page Tracker
===========================
Monitors the OpenAI status Atom feed for new incidents, outages, and degradations.

Design philosophy:
  - Uses the Atom feed (https://status.openai.com/history.atom) — a standard,
    push-friendly, cacheable format that avoids parsing raw HTML.
  - Respects HTTP caching headers (ETag / Last-Modified) so the server only
    sends a full payload when something has actually changed. This makes it
    safe and efficient even when monitoring 100+ status pages simultaneously.
  - Maintains an in-memory set of seen entry IDs so duplicate alerts are never
    printed within a single run.
  - Extracts the affected product/component from the entry title or body using
    a lightweight keyword match against known OpenAI API products.

Usage:
  python openai_status_tracker.py [--interval SECONDS]

Requirements:
  pip install feedparser requests
"""

import argparse
import signal
import sys
import time
from datetime import datetime, timezone

import feedparser
import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

FEED_URL = "https://status.openai.com/history.atom"

# Known OpenAI API products to extract from incident text.
# Extend this list as OpenAI adds new products.
KNOWN_PRODUCTS = [
    "Chat Completions",
    "Responses API",
    "Completions",
    "Embeddings",
    "Fine-tuning",
    "Images",
    "Audio",
    "Assistants",
    "Batch API",
    "Files API",
    "Moderation",
    "Realtime API",
    "Vector Stores",
    "Code Interpreter",
    "Function Calling",
    "Structured Outputs",
    "OpenAI API",
]

# Incident-related keywords; entries without these are skipped (e.g. maintenance notices).
INCIDENT_KEYWORDS = [
    "incident",
    "outage",
    "degraded",
    "degradation",
    "disruption",
    "partial",
    "investigating",
    "identified",
    "monitoring",
    "resolved",
    "update",
    "elevated",
    "error",
    "latency",
    "unavailable",
    "down",
    "failure",
]

DEFAULT_POLL_INTERVAL = 60  # seconds


# ---------------------------------------------------------------------------
# Feed fetching with HTTP conditional requests (ETag / Last-Modified)
# ---------------------------------------------------------------------------

class FeedTracker:
    def __init__(self, url: str, poll_interval: int):
        self.url = url
        self.poll_interval = poll_interval
        self._seen_ids: set[str] = set()
        self._etag: str | None = None
        self._last_modified: str | None = None
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "OpenAIStatusTracker/1.0 (+https://github.com/your-org/openai-status-tracker)"
        })

    def _fetch_feed(self) -> feedparser.FeedParserDict | None:
        """
        Fetch the Atom feed using conditional GET.
        Returns a parsed feed or None if the server signals 304 Not Modified.
        """
        headers = {}
        if self._etag:
            headers["If-None-Match"] = self._etag
        if self._last_modified:
            headers["If-Modified-Since"] = self._last_modified

        try:
            response = self._session.get(self.url, headers=headers, timeout=15)
        except requests.RequestException as exc:
            print(f"[{_now()}] ERROR: Could not reach {self.url} — {exc}", file=sys.stderr)
            return None

        if response.status_code == 304:
            # Server confirms nothing changed; no work to do.
            return None

        if response.status_code != 200:
            print(
                f"[{_now()}] ERROR: Unexpected HTTP {response.status_code} from {self.url}",
                file=sys.stderr,
            )
            return None

        # Cache validators for the next request
        self._etag = response.headers.get("ETag")
        self._last_modified = response.headers.get("Last-Modified")

        return feedparser.parse(response.text)

    def check(self):
        """Fetch and process new entries, printing alerts for new incidents."""
        feed = self._fetch_feed()
        if feed is None:
            return  # 304 Not Modified or transient error

        new_entries = [e for e in feed.entries if e.get("id") not in self._seen_ids]

        # Process oldest-first so the console log is chronological
        for entry in reversed(new_entries):
            entry_id = entry.get("id", "")
            self._seen_ids.add(entry_id)
            self._process_entry(entry)

    def _process_entry(self, entry: feedparser.util.FeedParserDict):
        """Decide if an entry is incident-related and, if so, print a formatted alert."""
        title: str = entry.get("title", "")
        # feedparser normalises content into entry.summary or entry.content[0].value
        body: str = entry.get("summary", "")
        if not body and entry.get("content"):
            body = entry["content"][0].get("value", "")

        combined_text = f"{title} {body}".lower()

        # Skip entries that look like routine maintenance or informational posts
        if not any(kw in combined_text for kw in INCIDENT_KEYWORDS):
            return

        product = _extract_product(title, body)
        timestamp = _entry_timestamp(entry)
        status_message = _clean_html(body) or title

        print(
            f"[{timestamp}] Product: {product}\n"
            f"Status: {status_message}\n"
            f"{'─' * 60}"
        )

    def run(self):
        """Blocking event loop. Ctrl-C to stop."""
        print(f"[{_now()}] Starting OpenAI Status Tracker (polling every {self.poll_interval}s)")
        print(f"[{_now()}] Feed: {self.url}")
        print("─" * 60)

        # Do the first check immediately
        self.check()

        while True:
            time.sleep(self.poll_interval)
            self.check()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _entry_timestamp(entry: feedparser.util.FeedParserDict) -> str:
    """Return a human-readable UTC timestamp from the feed entry."""
    ts = entry.get("updated_parsed") or entry.get("published_parsed")
    if ts:
        dt = datetime(*ts[:6], tzinfo=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    return _now()


def _extract_product(title: str, body: str) -> str:
    """
    Return the first matching known product name found in the title or body,
    falling back to a generic label.
    """
    combined = f"{title} {body}"
    for product in KNOWN_PRODUCTS:
        if product.lower() in combined.lower():
            return f"OpenAI API - {product}"
    return "OpenAI API"


def _clean_html(raw: str) -> str:
    """
    Very lightweight HTML tag stripper — avoids a BeautifulSoup dependency.
    Only used for console display; a more robust parser could be added.
    """
    import re
    text = re.sub(r"<[^>]+>", " ", raw)
    text = re.sub(r"\s+", " ", text).strip()
    # Truncate to keep console output readable
    return text[:300] + ("…" if len(text) > 300 else "")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _handle_sigint(_sig, _frame):
    print(f"\n[{_now()}] Tracker stopped.")
    sys.exit(0)


def main():
    parser = argparse.ArgumentParser(description="Track OpenAI status page incidents via Atom feed.")
    parser.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_POLL_INTERVAL,
        metavar="SECONDS",
        help=f"How often to poll the feed (default: {DEFAULT_POLL_INTERVAL}s)",
    )
    args = parser.parse_args()

    signal.signal(signal.SIGINT, _handle_sigint)

    tracker = FeedTracker(FEED_URL, args.interval)
    tracker.run()


if __name__ == "__main__":
    main()
