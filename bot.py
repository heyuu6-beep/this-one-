#!/usr/bin/env python3
"""
Telegram News Bot
-----------------
Collects new items from RSS feeds and ordinary web pages (via CSS selectors)
and posts them to a Telegram channel. Already-posted links are remembered in
SQLite so nothing is sent twice.

Usage:
    python bot.py --seed    # first run: mark current items as seen, post nothing
    python bot.py           # run forever, checking every CHECK_INTERVAL_MINUTES
    python bot.py --once    # single pass, then exit (good for cron)
    python bot.py --limit 5 # post only the 5 newest unseen items, then exit
    python bot.py --test    # send one test message to the channel
    python bot.py --dry-run # list new items per site, post nothing
"""
import argparse
import html
import json
import logging
import os
import re
import sqlite3
import time
from urllib import robotparser
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import feedparser
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "")  # one or more, comma separated: @one,@two or -100123...
CHANNELS = [c.strip() for c in CHANNEL_ID.split(",") if c.strip()]
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL_MINUTES", "15")) * 60
SOURCES_FILE = os.getenv("SOURCES_FILE", "sources.json")
DB_FILE = os.getenv("DB_FILE", "seen.db")
USER_AGENT = os.getenv("USER_AGENT", "TelegramNewsBot/1.0 (+contact: you@example.com)")
POST_DELAY = float(os.getenv("POST_DELAY_SECONDS", "3"))  # Telegram limits ~20 msgs/min per channel
RESPECT_ROBOTS = os.getenv("RESPECT_ROBOTS_TXT", "true").lower() == "true"
# Optional: only post items whose title/summary contains one of these words (comma separated)
KEYWORDS = [k.strip().lower() for k in os.getenv("KEYWORDS", "").split(",") if k.strip()]

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
HEADERS = {"User-Agent": USER_AGENT}
TIMEOUT = 20

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("newsbot")


# ----------------------------------------------------------------- storage
def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    conn.execute("CREATE TABLE IF NOT EXISTS seen (url TEXT PRIMARY KEY, ts INTEGER)")
    conn.commit()
    return conn


def is_seen(conn, url: str) -> bool:
    return conn.execute("SELECT 1 FROM seen WHERE url=?", (url,)).fetchone() is not None


def mark_seen(conn, url: str) -> None:
    conn.execute("INSERT OR IGNORE INTO seen VALUES (?, ?)", (url, int(time.time())))
    conn.commit()


# ----------------------------------------------------------------- helpers
_robots_cache: dict[str, robotparser.RobotFileParser] = {}


def allowed_by_robots(url: str) -> bool:
    if not RESPECT_ROBOTS:
        return True
    parts = urlparse(url)
    base = f"{parts.scheme}://{parts.netloc}"
    rp = _robots_cache.get(base)
    if rp is None:
        rp = robotparser.RobotFileParser()
        try:
            resp = requests.get(base + "/robots.txt", headers=HEADERS, timeout=TIMEOUT)
            rp.parse(resp.text.splitlines() if resp.status_code == 200 else [])
        except requests.RequestException:
            rp.parse([])
        _robots_cache[base] = rp
    return rp.can_fetch(USER_AGENT, url)


def clean_url(url: str) -> str:
    """Remove tracking parameters (utm_*, fbclid, gclid) from a link."""
    if not url:
        return url
    p = urlparse(url)
    keep = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
            if not k.lower().startswith("utm_") and k.lower() not in ("fbclid", "gclid")]
    return urlunparse(p._replace(query=urlencode(keep)))


def clean_text(raw: str, limit: int = 400) -> str:
    text = BeautifulSoup(raw or "", "html.parser").get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def fetch(url: str) -> requests.Response | None:
    if not allowed_by_robots(url):
        log.warning("robots.txt disallows %s - skipping", url)
        return None
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        return resp
    except requests.RequestException as exc:
        log.error("Fetch failed for %s: %s", url, exc)
        return None


# ----------------------------------------------------------------- scrapers
def parse_feed(content: bytes, max_items: int) -> list[dict]:
    feed = feedparser.parse(content)
    items = []
    for e in feed.entries[:max_items]:
        image = None
        if e.get("media_content"):
            image = e.media_content[0].get("url")
        elif e.get("media_thumbnail"):
            image = e.media_thumbnail[0].get("url")
        elif e.get("enclosures"):
            enc = e.enclosures[0]
            if enc.get("type", "").startswith("image"):
                image = enc.get("href")
        items.append({
            "title": clean_text(e.get("title", ""), 200),
            "url": clean_url(e.get("link", "")),
            "summary": clean_text(e.get("summary", "")),
            "image": image,
        })
    return items


def scrape_rss(src: dict) -> list[dict]:
    resp = fetch(src["url"])
    if not resp:
        return []
    return parse_feed(resp.content, src.get("max_items", 10))


def same_site(a: str, b: str) -> bool:
    strip = lambda u: urlparse(u).netloc.lower().replace("www.", "")
    return strip(a) == strip(b)


def scrape_auto(src: dict) -> list[dict]:
    """1) use the site's own RSS/Atom feed if it advertises one,
       2) otherwise read article headlines straight from the page."""
    max_items = src.get("max_items", 10)

    # a feed address given by hand wins
    if src.get("feed_url"):
        resp = fetch(src["feed_url"])
        items = parse_feed(resp.content, max_items) if resp else []
        if items:
            return items  # otherwise fall through to auto-discovery below

    resp = fetch(src["url"])
    if not resp:
        return []
    soup = BeautifulSoup(resp.text, "html.parser")

    # 1) feed auto-discovery: <link rel="alternate" type="application/rss+xml" href="...">
    for link in soup.find_all("link", attrs={"rel": "alternate"}):
        if "rss" in (link.get("type") or "") or "atom" in (link.get("type") or ""):
            feed_url = urljoin(src["url"], link.get("href", ""))
            fresp = fetch(feed_url)
            if fresp:
                items = parse_feed(fresp.content, max_items)
                if items:
                    return items

    # 2) headline fallback: links inside headings or <article> blocks
    items, seen_urls = [], set()
    for a in soup.select("article a[href], h1 a[href], h2 a[href], h3 a[href]"):
        title = clean_text(a.get_text(), 200)
        url = clean_url(urljoin(src["url"], a["href"]))
        path = urlparse(url).path.strip("/")
        if len(title) < 25 or not path or url in seen_urls or not same_site(url, src["url"]):
            continue
        seen_urls.add(url)
        image = None
        box = a.find_parent(["article", "li", "div"])
        img = box.select_one("img") if box else None
        if img is not None:
            image = img.get("src") or img.get("data-src")
            if image:
                image = urljoin(src["url"], image)
        items.append({"title": title, "url": url, "summary": "", "image": image})
        if len(items) >= max_items:
            break
    return items


def scrape_html(src: dict) -> list[dict]:
    resp = fetch(src["url"])
    if not resp:
        return []
    soup = BeautifulSoup(resp.text, "html.parser")
    sel = src["selectors"]
    items = []
    for node in soup.select(sel["item"])[: src.get("max_items", 10)]:
        title_el = node.select_one(sel["title"]) if sel.get("title") else node
        link_el = node.select_one(sel["link"]) if sel.get("link") else node
        if not title_el or not link_el or not link_el.get("href"):
            continue
        summary_el = node.select_one(sel["summary"]) if sel.get("summary") else None
        image_el = node.select_one(sel["image"]) if sel.get("image") else None
        image = None
        if image_el is not None:
            image = image_el.get("src") or image_el.get("data-src")
            if image:
                image = urljoin(src["url"], image)
        items.append({
            "title": clean_text(title_el.get_text(), 200),
            "url": clean_url(urljoin(src["url"], link_el["href"])),
            "summary": clean_text(summary_el.get_text()) if summary_el else "",
            "image": image,
        })
    return items


SCRAPERS = {"rss": scrape_rss, "html": scrape_html, "auto": scrape_auto}


# ----------------------------------------------------------------- telegram
def tg_call(method: str, payload: dict) -> bool:
    for _ in range(3):
        try:
            r = requests.post(f"{TG_API}/{method}", json=payload, timeout=TIMEOUT)
        except requests.RequestException as exc:
            log.error("Telegram request error: %s", exc)
            time.sleep(5)
            continue
        if r.status_code == 429:  # flood control
            wait = r.json().get("parameters", {}).get("retry_after", 10)
            log.warning("Rate limited, sleeping %ss", wait)
            time.sleep(wait + 1)
            continue
        if r.ok:
            return True
        log.error("Telegram error %s: %s", r.status_code, r.text)
        return False
    return False


def format_message(item: dict, source_name: str, limit: int) -> str:
    """Title, short excerpt, and a clear citation of where the news came from."""
    domain = urlparse(item["url"]).netloc.replace("www.", "")
    parts = [f"<b>{html.escape(item['title'])}</b>"]
    if item["summary"]:
        parts.append(html.escape(item["summary"]))
    parts.append(
        f"\U0001F4F0 Source: <b>{html.escape(source_name)}</b> ({html.escape(domain)})\n"
        f'\U0001F517 <a href="{html.escape(item["url"], quote=True)}">Read the full story</a>'
    )
    text = "\n\n".join(parts)
    return text[:limit]


def post_item(item: dict, source_name: str) -> bool:
    """Post to every configured channel. True if at least one succeeded."""
    any_ok = False
    for channel in CHANNELS:
        ok = False
        if item.get("image"):
            ok = tg_call("sendPhoto", {
                "chat_id": channel,
                "photo": item["image"],
                "caption": format_message(item, source_name, 1024),
                "parse_mode": "HTML",
            })
        if not ok:  # no image, or Telegram rejected it: send text only
            ok = tg_call("sendMessage", {
                "chat_id": channel,
                "text": format_message(item, source_name, 4096),
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            })
        any_ok = any_ok or ok
    return any_ok


# ----------------------------------------------------------------- main loop
def load_sources() -> list[dict]:
    with open(SOURCES_FILE, encoding="utf-8") as f:
        return [s for s in json.load(f) if s.get("enabled", True)]


def matches_keywords(item: dict) -> bool:
    if not KEYWORDS:
        return True
    blob = f"{item['title']} {item['summary']}".lower()
    return any(k in blob for k in KEYWORDS)


def run_cycle(conn, seed: bool = False, dry_run: bool = False) -> None:
    for src in load_sources():
        scraper = SCRAPERS.get(src["type"])
        if not scraper:
            log.error("Unknown source type %r for %s", src["type"], src["name"])
            continue
        try:
            items = scraper(src)
        except Exception:
            log.exception("Scraper crashed for %s", src["name"])
            continue

        new_items = [i for i in items if i["url"] and i["title"] and not is_seen(conn, i["url"])]
        log.info("%s: %d items found, %d new", src["name"], len(items), len(new_items))

        if dry_run:  # show what would happen, send nothing, save nothing
            for item in new_items:
                flag = "" if matches_keywords(item) else "  (filtered out by KEYWORDS)"
                print(f"  - {item['title']}\n    {item['url']}{flag}")
            continue

        for item in reversed(new_items):  # oldest first
            if seed or not matches_keywords(item):
                mark_seen(conn, item["url"])
                continue
            if post_item(item, src["name"]):
                mark_seen(conn, item["url"])
                time.sleep(POST_DELAY)


def post_latest(conn, limit: int) -> None:
    """Post up to `limit` of the newest unseen items, spread across all sources."""
    per_source = []
    for src in load_sources():
        scraper = SCRAPERS.get(src["type"])
        if not scraper:
            continue
        try:
            items = scraper(src)
        except Exception:
            log.exception("Scraper crashed for %s", src["name"])
            continue
        fresh = [(src["name"], i) for i in items
                 if i["url"] and i["title"] and not is_seen(conn, i["url"]) and matches_keywords(i)]
        log.info("%s: %d items found, %d new", src["name"], len(items), len(fresh))
        per_source.append(fresh)

    # take items round-robin (newest first from each source) until the limit is reached
    chosen = []
    while len(chosen) < limit and any(per_source):
        for bucket in per_source:
            if bucket and len(chosen) < limit:
                chosen.append(bucket.pop(0))

    for name, item in reversed(chosen):  # oldest first
        if post_item(item, name):
            mark_seen(conn, item["url"])
            time.sleep(POST_DELAY)
    log.info("Posted %d item(s).", len(chosen))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", action="store_true", help="mark current items as seen without posting")
    parser.add_argument("--once", action="store_true", help="run one cycle and exit")
    parser.add_argument("--limit", type=int, metavar="N", help="post only the N newest unseen items (all sites combined), then exit")
    parser.add_argument("--dry-run", action="store_true", help="show new items found, post nothing")
    parser.add_argument("--test", action="store_true", help="send one test message to the channel")
    args = parser.parse_args()

    if not BOT_TOKEN or not CHANNELS:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHANNEL_ID in .env")

    if args.test:
        for ch in CHANNELS:
            ok = tg_call("sendMessage", {"chat_id": ch, "text": "Bot connected. Test message."})
            print(f"{ch}: " + ("test message sent." if ok else "FAILED - is the bot an admin there?"))
        return

    conn = init_db()
    if args.limit:
        post_latest(conn, args.limit)
        return
    if args.dry_run:
        run_cycle(conn, dry_run=True)
        return
    if args.seed:
        run_cycle(conn, seed=True)
        log.info("Seeding complete.")
        return
    if args.once:
        run_cycle(conn)
        return
    while True:
        run_cycle(conn)
        log.info("Sleeping %d minutes", CHECK_INTERVAL // 60)
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
