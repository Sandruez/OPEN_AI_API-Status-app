# OpenAI Status Tracker

Automatically detects and logs new incidents, outages, and degradations from the [OpenAI Status Page](https://status.openai.com).

## Quick Start

```bash
pip install -r requirements.txt
python openai_status_tracker.py
# Optional: custom poll interval (default 60s)
python openai_status_tracker.py --interval 30
```

## Example Output

```
[2025-11-03 14:32:00] Starting OpenAI Status Tracker (polling every 60s)
[2025-11-03 14:32:00] Feed: https://status.openai.com/history.atom
────────────────────────────────────────────────────────────
[2025-11-03 14:32:00] Product: OpenAI API - Chat Completions
Status: We are investigating reports of elevated error rates affecting Chat Completions…
────────────────────────────────────────────────────────────
```

## Design & Scalability

### Why the Atom Feed?

OpenAI's status page (like most Statuspage-powered sites) publishes a standard **Atom feed** at `/history.atom`. This is the correct abstraction layer to consume:

- **No HTML scraping** — the feed is a structured, machine-readable format that won't break when the page redesigns.
- **Standard format** — the same approach works for any Statuspage, PagerDuty, or BetterStack-powered status page, so scaling to 100+ providers is trivial.

### HTTP Conditional Requests (ETag / Last-Modified)

On every poll, the script sends the `If-None-Match` (ETag) and `If-Modified-Since` headers from the previous response. If nothing has changed, the server returns **HTTP 304 Not Modified** with an empty body — zero parsing work, minimal bandwidth.

This is the key design choice that makes the solution efficient at scale:

| Approach | Bandwidth per poll | CPU per poll |
|---|---|---|
| Raw HTML scraping | High (full page) | High (DOM parse) |
| JSON API polling (no caching) | Medium | Low |
| **Atom + conditional GET (this script)** | **~0 when unchanged** | **~0 when unchanged** |

### Deduplication

A per-run in-memory `set` of entry IDs ensures each incident update is printed exactly once, even if the polling interval is short.

### Extending to 100+ Status Pages

To track many providers concurrently, run each `FeedTracker` in its own thread or `asyncio` task:

```python
import threading

feeds = [
    "https://status.openai.com/history.atom",
    "https://www.githubstatus.com/history.atom",
    "https://status.anthropic.com/history.atom",
    # ... add more
]

threads = [
    threading.Thread(target=FeedTracker(url, interval=60).run, daemon=True)
    for url in feeds
]
for t in threads:
    t.start()
for t in threads:
    t.join()
```

No external queue or message broker required for 100 feeds; each thread sleeps independently and only does real work when its feed actually changes.
