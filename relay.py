#!/usr/bin/env python3
"""Fetch the RSS feeds the production server cannot reach and write them as compact JSON.

Runs on a GitHub Actions runner (outside the firewall) and publishes the JSON to the `data` branch of
a public relay repository; the dashboard server reads it from raw.githubusercontent.com. Only titles,
links, publication times and a short summary are kept — the feeds' own pages stay the source of truth.

stdlib only, so the runner needs no pip install.
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from xml.etree import ElementTree as ET

FEEDS = {
    "scmp.json": {"source": "南华早报 SCMP", "url": "https://www.scmp.com/rss/2/feed"},
    "nikkei.json": {"source": "Nikkei Asia", "url": "https://asia.nikkei.com/rss/feed/nar"},
}
MAX_ITEMS = 30
SUMMARY_CHARS = 160
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _text(el) -> str:
    return unescape("".join(el.itertext())).strip() if el is not None else ""


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", unescape(s or ""))).strip()


def _iso(raw: str) -> str | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    except (TypeError, ValueError):
        pass
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return (dt.astimezone(timezone.utc) if dt.tzinfo else dt).strftime("%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None


def parse_feed(xml_bytes: bytes) -> list[dict]:
    """Items of an RSS 2.0 (<channel><item>) or RSS 1.0 / RDF (<item> next to <channel>) document."""
    root = ET.fromstring(xml_bytes)
    items, seen = [], set()
    for el in root.iter():
        if _local(el.tag) != "item":
            continue
        fields = {_local(c.tag): c for c in el}
        title = _clean(_text(fields.get("title")))
        link = _text(fields.get("link")) or el.attrib.get("{http://www.w3.org/1999/02/22-rdf-syntax-ns#}about", "")
        if not title or not link or link in seen:
            continue
        seen.add(link)
        summary = _clean(_text(fields.get("description")) or _text(fields.get("encoded")))[:SUMMARY_CHARS]
        published = _iso(_text(fields.get("pubDate")) or _text(fields.get("date")))
        items.append({"title": title, "link": link, "summary": summary, "published_at": published})
        if len(items) >= MAX_ITEMS:
            break
    return items


def fetch(url: str, attempts: int = 3) -> bytes:
    last = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/rss+xml, application/xml, text/xml, */*"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read()
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"{url}: {last}")


def main(out_dir: str = "out") -> int:
    import os

    os.makedirs(out_dir, exist_ok=True)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    failed = 0
    for name, cfg in FEEDS.items():
        try:
            items = parse_feed(fetch(cfg["url"]))
            if not items:
                raise RuntimeError("feed parsed but had no items")
        except Exception as exc:  # noqa: BLE001
            print(f"FAILED {name}: {exc}", file=sys.stderr)
            failed += 1
            continue
        with open(os.path.join(out_dir, name), "w", encoding="utf-8") as f:
            json.dump({"source": cfg["source"], "fetched_at": now, "items": items}, f, ensure_ascii=False, indent=1)
        print(f"ok {name}: {len(items)} items")
    return 1 if failed == len(FEEDS) else 0       # one feed down is tolerable; both down is a failed run


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:2]))
