#!/usr/bin/env python3
"""Builds conservative RSS feeds from configured public news pages."""
from datetime import datetime, timezone
from email.utils import format_datetime
from html import unescape
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse
import json, re, sys, xml.etree.ElementTree as ET

import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "generated-feeds"
UA = "meuFeed/1.0 (+https://willervasques.github.io/meuFeed/)"

def canonical_url(raw, base, allowed):
    url = urljoin(base, raw.strip())
    p = urlparse(url)
    if p.scheme not in ("http", "https") or p.hostname not in allowed:
        return None
    path = re.sub(r"/+", "/", p.path or "/")
    if path.endswith("/") and path != "/":
        path = path[:-1]
    return urlunparse((p.scheme, p.netloc.lower(), path, "", "", ""))

def parse_date(soup, node):
    candidates = [
        node.get("datetime") if node else None,
        node.get("content") if node else None,
        soup.find("meta", attrs={"property":"article:published_time"}),
        soup.find("meta", attrs={"name":"date"}),
        soup.find("meta", attrs={"name":"pubdate"}),
        soup.find("meta", attrs={"itemprop":"datePublished"}),
    ]
    for c in candidates:
        if hasattr(c, "get"):
            c = c.get("content")
        if not c:
            continue
        try:
            d = datetime.fromisoformat(str(c).replace("Z", "+00:00"))
            if d.tzinfo is None: d = d.replace(tzinfo=timezone.utc)
            return d.astimezone(timezone.utc)
        except (ValueError, TypeError):
            pass
    return None

def build(source):
    base = source["base_url"]
    allowed = set(source.get("allowed_hosts", [urlparse(base).hostname]))
    fetch_ok = True
    try:
        response = requests.get(base, timeout=25, headers={"User-Agent": UA})
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
    except requests.RequestException as exc:
        print(f"[WARN] {source['name']}: fetch failed: {exc}; emitting an empty valid RSS", file=sys.stderr)
        soup = BeautifulSoup("", "html.parser")
        fetch_ok = False
    entries, seen = [], set()
    for a in soup.select(source.get("article_selector", "a[href]")):
        title = " ".join(a.get_text(" ", strip=True).split())
        href = a.get("href")
        if not title or len(title) < 18 or not href:
            continue
        link = canonical_url(href, base, allowed)
        if not link or link in seen:
            continue
        path = urlparse(link).path.lower()
        if path in ("", "/", "/ noticias") or any(x in path for x in ("/tag/", "/categoria/", "/author/", "/search")):
            continue
        # Skip obvious navigation and utility links; retain legitimate article paths.
        if len(path.strip("/").split("/")) < 2 and not re.search(r"\d", path):
            continue
        seen.add(link)
        parent = a
        for _ in range(4):
            if parent.parent: parent = parent.parent
        date_node = a.find("time") or parent.find("time")
        published = parse_date(soup, date_node)
        desc = " ".join(parent.get_text(" ", strip=True).split())
        if desc == title: desc = ""
        entries.append((published, title, link, desc[:1200]))
    entries.sort(key=lambda e: (e[0] is not None, e[0] or datetime.min.replace(tzinfo=timezone.utc)), reverse=True)
    entries = entries[:60]
    if not entries:
        print(f"[WARN] {source['name']}: no article links found", file=sys.stderr)
    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = source["name"]
    ET.SubElement(channel, "link").text = base
    ET.SubElement(channel, "description").text = f"Feed gerado pelo meuFeed para {source['name']}"
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(datetime.now(timezone.utc))
    for published, title, link, desc in entries:
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = title
        ET.SubElement(item, "link").text = link
        ET.SubElement(item, "guid", isPermaLink="true").text = link
        if desc: ET.SubElement(item, "description").text = desc
        if published: ET.SubElement(item, "pubDate").text = format_datetime(published)
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / source["output"]
    ET.ElementTree(rss).write(path, encoding="utf-8", xml_declaration=True)
    print(f"[OK] {source['name']}: {len(entries)} items -> {path}")
    return fetch_ok

def main():
    cfg = yaml.safe_load((ROOT / "sources.yaml").read_text(encoding="utf-8")) or {}
    failed = 0
    for source in cfg.get("sources", []):
        if not build(source): failed += 1
    if failed:
        print(f"[WARN] {failed} source(s) failed to fetch; valid RSS files were still emitted.")

if __name__ == "__main__":
    main()
