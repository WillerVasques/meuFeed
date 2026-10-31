#!/usr/bin/env python3
"""Builds RSS feeds from configured HTML pages or native RSS/Atom feeds."""
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
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
    if not raw:
        return None
    url = urljoin(base, str(raw).strip())
    p = urlparse(url)
    if p.scheme not in ("http", "https") or (p.hostname or "").lower() not in allowed:
        return None
    path = re.sub(r"/+", "/", p.path or "/")
    if path.endswith("/") and path != "/":
        path = path[:-1]
    return urlunparse((p.scheme, p.netloc.lower(), path, "", "", ""))

def local_name(tag):
    return tag.rsplit("}", 1)[-1].lower()

def child_text(node, names):
    for child in list(node):
        if local_name(child.tag) in names:
            value = "".join(child.itertext()).strip()
            if value:
                return value
    return ""

def feed_link(node, atom=False):
    if atom:
        links = [e for e in list(node) if local_name(e.tag) == "link"]
        for link in links:
            if link.attrib.get("rel", "alternate") == "alternate":
                href = link.attrib.get("href")
                if href:
                    return href
        return links[0].attrib.get("href", "") if links else ""
    return child_text(node, {"link"})

def parse_feed_date(value):
    if not value:
        return None
    try:
        result = parsedate_to_datetime(value)
        if result.tzinfo is None:
            result = result.replace(tzinfo=timezone.utc)
        return result.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if result.tzinfo is None:
                result = result.replace(tzinfo=timezone.utc)
            return result.astimezone(timezone.utc)
        except (AttributeError, TypeError, ValueError):
            return None

def image_from_item(node, feed_url):
    """Obtém imagem por enclosure, Media RSS ou imagem embutida na descrição."""
    for element in node.iter():
        name = local_name(element.tag)
        raw = element.attrib.get("url") or element.attrib.get("href")
        if raw and name in {"content", "thumbnail", "enclosure"}:
            url = urljoin(feed_url, raw.strip())
            if urlparse(url).scheme in {"http", "https"}:
                return url
    for element in node.iter():
        if local_name(element.tag) in {"description", "summary", "encoded", "content"} and element.text:
            soup = BeautifulSoup(unescape(element.text), "html.parser")
            img = soup.find("img", src=True)
            if img:
                url = urljoin(feed_url, img["src"].strip())
                if urlparse(url).scheme in {"http", "https"}:
                    return url
    return ""


def article_metadata(session, url):
    """Prioriza descrição e imagem declaradas pela própria página da notícia."""
    try:
        response = session.get(url, timeout=(8, 18))
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        def meta(*selectors):
            for selector in selectors:
                tag = soup.select_one(selector)
                if tag:
                    value = tag.get("content") or tag.get("href")
                    if value and value.strip():
                        return value.strip()
            return ""

        description = meta(
            'meta[property="og:description"]',
            'meta[name="description"]',
            'meta[name="twitter:description"]',
        )
        image = meta(
            'meta[property="og:image"]',
            'meta[name="twitter:image"]',
            'meta[property="og:image:secure_url"]',
        )
        if not description:
            lead = soup.select_one("article p, .article-content p, .post-content p, main p")
            if lead:
                description = lead.get_text(" ", strip=True)
        if image:
            image = urljoin(response.url, image)
            if urlparse(image).scheme not in {"http", "https"}:
                image = ""
        description = " ".join(BeautifulSoup(description, "html.parser").get_text(" ", strip=True).split())
        return description[:1200], image
    except requests.RequestException as exc:
        print(f"[WARN] Não foi possível enriquecer notícia {url}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return "", ""


def parse_native_feed(content, source, feed_url, allowed, session=None):
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise ValueError(f"resposta não é XML/RSS/Atom válido: {exc}") from exc

    root_name = local_name(root.tag)
    atom = root_name == "feed"
    nodes = [node for node in root.iter() if local_name(node.tag) in ({"entry"} if atom else {"item"})]
    entries, seen = [], set()
    for node in nodes:
        title = " ".join(child_text(node, {"title"}).split())
        raw_link = feed_link(node, atom=atom)
        link = canonical_url(raw_link, feed_url, allowed)
        if not title or not link or link in seen:
            continue
        seen.add(link)
        description = child_text(node, {"description", "summary", "encoded", "content"})
        description = " ".join(BeautifulSoup(unescape(description), "html.parser").get_text(" ", strip=True).split())
        image = image_from_item(node, feed_url)
        if source.get("enrich_articles") and session:
            page_description, page_image = article_metadata(session, link)
            if page_description:
                description = page_description
            if page_image:
                image = page_image
        date_value = child_text(node, {"pubdate", "published", "updated", "date"})
        published = parse_feed_date(date_value)
        entries.append((published, title, link, description[:1200], image))

    entries.sort(
        key=lambda e: (e[0] is not None, e[0] or datetime.min.replace(tzinfo=timezone.utc)),
        reverse=True,
    )
    entries = entries[:60]
    if not entries:
        raise ValueError("feed XML foi lido, mas nenhum item com título e link permitido foi encontrado")
    return entries

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
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            return d.astimezone(timezone.utc)
        except (ValueError, TypeError):
            pass
    return None

def parse_html_entries(soup, source, base, allowed):
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
        if len(path.strip("/").split("/")) < 2 and not re.search(r"\\d", path):
            continue
        seen.add(link)
        parent = a
        for _ in range(4):
            if parent.parent:
                parent = parent.parent
        date_node = a.find("time") or parent.find("time")
        published = parse_date(soup, date_node)
        desc = " ".join(parent.get_text(" ", strip=True).split())
        if desc == title:
            desc = ""
        entries.append((published, title, link, desc[:1200], ""))
    entries.sort(
        key=lambda e: (e[0] is not None, e[0] or datetime.min.replace(tzinfo=timezone.utc)),
        reverse=True,
    )
    return entries[:60]

def write_output(source, entries):
    site_url = source.get("site_url") or source["base_url"]
    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = source["name"]
    ET.SubElement(channel, "link").text = site_url
    ET.SubElement(channel, "description").text = f"Feed gerado pelo meuFeed para {source['name']}"
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(datetime.now(timezone.utc))
    for published, title, link, desc, image in entries:
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = title
        ET.SubElement(item, "link").text = link
        ET.SubElement(item, "guid", isPermaLink="true").text = link
        if desc:
            ET.SubElement(item, "description").text = desc
        if image:
            ET.SubElement(item, "{http://search.yahoo.com/mrss/}content", {"url": image, "medium": "image"})
            ET.SubElement(item, "enclosure", {"url": image, "type": "image/jpeg"})
        if published:
            ET.SubElement(item, "pubDate").text = format_datetime(published)
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / source["output"]
    ET.ElementTree(rss).write(path, encoding="utf-8", xml_declaration=True)
    print(f"[OK] {source['name']}: {len(entries)} items -> {path}")

def build(source):
    base = source["base_url"]
    allowed = {str(host).lower() for host in source.get("allowed_hosts", [urlparse(base).hostname])}
    fetch_ok = True
    session = requests.Session()
    session.headers.update({
        "User-Agent": UA,
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, text/html;q=0.9, */*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    })
    try:
        response = session.get(base, timeout=(10, 30))
        print(
            f"[INFO] {source['name']}: HTTP {response.status_code}; "
            f"content-type={response.headers.get('Content-Type', '(missing)')}; "
            f"server={response.headers.get('Server', '(missing)')}",
            file=sys.stderr,
        )
        response.raise_for_status()
        if source.get("mode", "html").lower() in {"rss", "atom", "xml"}:
            entries = parse_native_feed(response.content, source, response.url, allowed, session=session)
        else:
            soup = BeautifulSoup(response.text, "html.parser")
            entries = parse_html_entries(soup, source, base, allowed)
        if not entries:
            print(f"[WARN] {source['name']}: no article links found", file=sys.stderr)
    except requests.exceptions.SSLError as exc:
        print(f"[ERROR] {source['name']}: SSL/TLS failure: {type(exc).__name__}: {exc}", file=sys.stderr)
        entries, fetch_ok = [], False
    except requests.exceptions.HTTPError as exc:
        response = getattr(exc, "response", None)
        status = response.status_code if response is not None else "(unknown)"
        headers = response.headers if response is not None else {}
        body = response.text if response is not None else ""
        excerpt = " ".join(BeautifulSoup(body, "html.parser").get_text(" ", strip=True).split())[:800]
        print(
            f"[ERROR] {source['name']}: HTTP {status}; "
            f"content-type={headers.get('Content-Type', '(missing)')}; "
            f"server={headers.get('Server', '(missing)')}; response excerpt={excerpt!r}",
            file=sys.stderr,
        )
        entries, fetch_ok = [], False
    except (requests.RequestException, ValueError) as exc:
        print(f"[ERROR] {source['name']}: {type(exc).__name__}: {exc}", file=sys.stderr)
        entries, fetch_ok = [], False
    finally:
        session.close()
    write_output(source, entries)
    return fetch_ok

def main():
    cfg = yaml.safe_load((ROOT / "sources.yaml").read_text(encoding="utf-8")) or {}
    failed = 0
    for source in cfg.get("sources", []):
        if not build(source):
            failed += 1
    if failed:
        print(f"[WARN] {failed} source(s) failed to fetch; valid RSS files were still emitted.")

if __name__ == "__main__":
    main()
