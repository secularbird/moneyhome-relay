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
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from xml.etree import ElementTree as ET

# Verified 2026-10-16: these public feeds return 403 on both production and
# GitHub-hosted runners. Keep documented, but do not advertise them as live.
DISABLED_FEEDS = {
    "bls_cpi.json": {"source": "BLS 美国劳工统计局 CPI", "url": "https://www.bls.gov/feed/cpi.rss", "max_age_days": 45},
    "bls_ppi.json": {"source": "BLS 美国劳工统计局 PPI", "url": "https://www.bls.gov/feed/ppi.rss", "max_age_days": 45},
    "bls_jobs.json": {"source": "BLS 美国劳工统计局就业", "url": "https://www.bls.gov/feed/empsit.rss", "max_age_days": 45},
}

FEEDS = {
    "euronews_business.json": {"source": "Euronews 商业", "url": "https://www.euronews.com/rss?format=mrss&level=vertical&name=business", "max_age_days": 7},
    "scmp.json": {"source": "南华早报 SCMP", "url": "https://www.scmp.com/rss/2/feed"},
    "nikkei.json": {"source": "Nikkei Asia", "url": "https://asia.nikkei.com/rss/feed/nar"},
    "bbc_business.json": {"source": "BBC 商业", "url": "https://feeds.bbci.co.uk/news/business/rss.xml"},
    "bbc_world.json": {"source": "BBC 国际", "url": "https://feeds.bbci.co.uk/news/world/rss.xml"},
    "cnn_money.json": {"source": "CNN 财经", "url": "http://rss.cnn.com/rss/money_news_international.rss"},
    "cnn_world.json": {"source": "CNN 国际", "url": "http://rss.cnn.com/rss/edition_world.rss"},
    "google_business.json": {"source": "Google 新闻商业", "url": "https://news.google.com/rss/headlines/section/topic/BUSINESS?hl=en-US&gl=US&ceid=US:en"},
    "google_business_cn.json": {"source": "Google 新闻商业(中文)", "url": "https://news.google.com/rss/headlines/section/topic/BUSINESS?hl=zh-CN&gl=CN&ceid=CN:zh-Hans"},
    "guardian_business.json": {"source": "卫报商业", "url": "https://www.theguardian.com/business/rss"},
    "aljazeera.json": {"source": "半岛电视台", "url": "https://www.aljazeera.com/xml/rss/all.xml"},
    "economist.json": {"source": "经济学人财经", "url": "https://www.economist.com/finance-and-economics/rss.xml"},
    "cna_business.json": {"source": "CNA 商业", "url": "https://www.channelnewsasia.com/api/v1/rss-outbound-feed?_format=xml&category=6936"},
    "dw_business.json": {"source": "DW 商业", "url": "https://rss.dw.com/xml/rss-en-bus"},
    "investing.json": {"source": "Investing.com", "url": "https://www.investing.com/rss/news.rss"},
    "ft_markets.json": {"source": "FT 金融时报", "url": "https://www.ft.com/rss/markets"},
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


def fetch(url: str, attempts: int = 2) -> bytes:
    last = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/rss+xml, application/xml, text/xml, */*"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.read()
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"{url}: {last}")


def _one(name: str, cfg: dict):
    try:
        items = parse_feed(fetch(cfg["url"]))
        if cfg.get("max_age_days"):
            now = datetime.now(timezone.utc)
            cutoff = now - timedelta(days=cfg["max_age_days"])
            fresh = []
            for item in items:
                stamp = item.get("published_at")
                if not stamp:
                    continue
                dt = datetime.fromisoformat(stamp).replace(tzinfo=timezone.utc)
                if cutoff <= dt <= now + timedelta(minutes=15):
                    fresh.append({**item, "published_at": dt.isoformat(timespec="seconds")})
            items = fresh
        if not items:
            raise RuntimeError("feed parsed but had no eligible items")
        return name, items, None
    except Exception as exc:  # noqa: BLE001
        return name, None, exc


def main(out_dir: str = "out") -> int:
    import os
    from concurrent.futures import ThreadPoolExecutor

    os.makedirs(out_dir, exist_ok=True)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ok = 0
    statuses = {}
    with ThreadPoolExecutor(max_workers=8) as ex:                  # blocked feeds wait out their timeout; do not queue them
        for name, items, exc in ex.map(lambda kv: _one(*kv), FEEDS.items()):
            if exc is not None:
                print(f"FAILED {name}: {exc}", file=sys.stderr)
                statuses[name] = {"status": "failed", "error": str(exc)[:300]}
                # Publish an explicit failure, not a missing file (404) or a
                # stale success from a previous run. Consumers keep their DB cache.
                with open(os.path.join(out_dir, name), "w", encoding="utf-8") as f:
                    json.dump({"source": FEEDS[name]["source"], "fetched_at": now,
                               "status": "failed", "items": []}, f, ensure_ascii=False)
                continue
            with open(os.path.join(out_dir, name), "w", encoding="utf-8") as f:
                json.dump({"source": FEEDS[name]["source"], "fetched_at": now, "items": items}, f, ensure_ascii=False, indent=1)
            print(f"ok {name}: {len(items)} items")
            ok += 1
            statuses[name] = {"status": "ok", "item_count": len(items)}
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"fetched_at": now, "status": "ok" if ok == len(FEEDS) else "partial",
                   "feeds": statuses}, f, ensure_ascii=False, indent=1)
    return 0 if ok == len(FEEDS) else 1


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:2]))
