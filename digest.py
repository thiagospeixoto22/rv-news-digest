# digest.py
import os
import re
import smtplib
import ssl
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import List, Optional, Dict
from urllib.parse import urljoin

import pytz
import requests
import feedparser
from bs4 import BeautifulSoup
from dateutil import parser as dtparser

from sources import SOURCES, GOOGLE_NEWS_QUERIES, google_news_rss_url

ET = pytz.timezone("America/New_York")

@dataclass
class Item:
    source: str
    title: str
    url: str
    published: datetime
    summary: str = ""

KEYWORDS = {
    "Acquisitions / For Sale": [
        "acquisition", "acquired", "merger", "portfolio", "for sale", "listed", "broker", "transaction", "closed", "deal"
    ],
    "Insurance / Risk": [
        "insurance", "insurer", "premium", "wildfire", "flood", "hurricane", "liability", "risk", "claim"
    ],
    "Legal / Zoning": [
        "zoning", "ordinance", "lawsuit", "litigation", "permit", "planning", "commission", "approval", "denied"
    ],
    "Financing / Markets": [
        "financing", "refinancing", "loan", "lender", "interest rate", "debt", "bond", "cap rate", "treasury"
    ],
    "Earnings / Public Companies": [
        "earnings", "guidance", "supplemental", "dividend", "conference call", "results", "sec filing", "10-q", "10-k", "8-k"
    ],
    "Operations / Industry": [
        "occupancy", "rate", "revpar", "dynamic pricing", "reservations", "demand", "supply", "development", "expansion"
    ],
    "People / Notable": [
        "ceo", "founder", "appointed", "resigns", "retired", "death", "dies", "passed away", "obituary"
    ],
}

def safe_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return dtparser.parse(s)
    except Exception:
        return None

def within_days(dt: datetime, days: int) -> bool:
    now = datetime.now(tz=ET)
    if dt.tzinfo is None:
        dt = ET.localize(dt)
    return dt >= (now - timedelta(days=days))

def fetch_url(url: str) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; AthenaRVNewsBot/1.0; +https://github.com/)"
    }
    r = requests.get(url, headers=headers, timeout=25)
    r.raise_for_status()
    return r.text

def parse_rss(source_name: str, url: str) -> List[Item]:
    # feedparser can accept URLs directly, but we fetch ourselves so we can set UA, handle odd servers
    xml = fetch_url(url)
    feed = feedparser.parse(xml)
    items: List[Item] = []
    for e in feed.entries[:80]:
        title = (e.get("title") or "").strip()
        link = (e.get("link") or "").strip()
        published = safe_dt(e.get("published") or e.get("updated"))
        if not (title and link and published):
            continue
        summary = BeautifulSoup(e.get("summary", "") or "", "lxml").get_text(" ", strip=True)
        items.append(Item(source=source_name, title=title, url=link, published=published, summary=summary))
    return items

def parse_html_rvbusiness(source_name: str, url: str) -> List[Item]:
    html = fetch_url(url)
    soup = BeautifulSoup(html, "lxml")
    items: List[Item] = []

    # RVBusiness pages typically have article blocks with links and date text.
    # We'll grab the main content area links that look like posts.
    for a in soup.select("a"):
        href = a.get("href") or ""
        text = a.get_text(" ", strip=True)
        if not href or not text:
            continue
        if href.startswith("/"):
            href = urljoin(url, href)
        if "rvbusiness.com" not in href:
            continue
        # Heuristic: skip nav links
        if len(text) < 12:
            continue
        # Try to find a nearby date
        parent_text = a.parent.get_text(" ", strip=True) if a.parent else ""
        m = re.search(r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+\d{4}", parent_text)
        if not m:
            continue
        published = safe_dt(m.group(0))
        if not published:
            continue
        items.append(Item(source=source_name, title=text, url=href, published=published))
    # Deduplicate by URL
    uniq = {}
    for it in items:
        uniq[it.url] = it
    return list(uniq.values())

def parse_html_koa_press(source_name: str, url: str) -> List[Item]:
    html = fetch_url(url)
    soup = BeautifulSoup(html, "lxml")
    items: List[Item] = []
    # KOA pressroom lists press releases as links; dates typically visible on the page.
    for a in soup.select("a"):
        href = a.get("href") or ""
        txt = a.get_text(" ", strip=True)
        if not href or not txt:
            continue
        if href.startswith("/"):
            href = urljoin(url, href)
        if "koapressroom.com" not in href:
            continue
        # Find date nearby
        block = a.parent.get_text(" ", strip=True) if a.parent else ""
        dt = safe_dt(block)
        if not dt:
            # fallback: try to parse from link slug? skip if no date
            continue
        items.append(Item(source=source_name, title=txt, url=href, published=dt))
    uniq = {}
    for it in items:
        uniq[it.url] = it
    return list(uniq.values())

def parse_html_simple_list(source_name: str, url: str) -> List[Item]:
    # Works for many IR “news” pages: pull links and try to parse a date in the surrounding text.
    html = fetch_url(url)
    soup = BeautifulSoup(html, "lxml")
    items: List[Item] = []
    date_regex = re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b|\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},\s+\d{4}\b")

    for a in soup.select("a"):
        txt = a.get_text(" ", strip=True)
        href = a.get("href") or ""
        if not txt or not href:
            continue
        if href.startswith("/"):
            href = urljoin(url, href)

        # Focus on same-site links
        if url.split("/")[2] not in href:
            continue

        # Nearby date
        context = (a.parent.get_text(" ", strip=True) if a.parent else "")[:300]
        m = date_regex.search(context)
        if not m:
            continue
        published = safe_dt(m.group(0))
        if not published:
            continue
        items.append(Item(source=source_name, title=txt, url=href, published=published))

    uniq = {}
    for it in items:
        uniq[it.url] = it
    return list(uniq.values())

def categorize(item: Item) -> List[str]:
    hay = (item.title + " " + item.summary).lower()
    tags = []
    for cat, words in KEYWORDS.items():
        if any(w in hay for w in words):
            tags.append(cat)
    return tags or ["Other"]

def build_email_html(items_by_cat: Dict[str, List[Item]]) -> str:
    now = datetime.now(tz=ET)
    start = (now - timedelta(days=7)).strftime("%b %d, %Y")
    end = now.strftime("%b %d, %Y")

    parts = [f"<h2>Athena RV Park Weekly Digest</h2><p><b>Window:</b> {start} – {end}</p>"]
    for cat, items in items_by_cat.items():
        parts.append(f"<h3>{cat} ({len(items)})</h3><ul>")
        for it in sorted(items, key=lambda x: x.published, reverse=True)[:15]:
            d = it.published.astimezone(ET).strftime("%b %d, %Y")
            parts.append(f'<li><b>{d}</b> — <a href="{it.url}">{it.title}</a> <i>({it.source})</i></li>')
        parts.append("</ul>")
    parts.append("<p style='color:#666;font-size:12px'>Automated weekly digest via GitHub Actions.</p>")
    return "\n".join(parts)

def send_email(subject: str, html_body: str):
    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))

    username = os.environ["SMTP_USERNAME"]
    password = os.environ["SMTP_PASSWORD"]

    to_email = os.environ["TO_EMAIL"]
    from_email = os.environ.get("FROM_EMAIL", username)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_email
    msg["To"] = to_email
    msg.attach(MIMEText(html_body, "html"))

    context = ssl.create_default_context()
    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.starttls(context=context)
        server.login(username, password)
        server.sendmail(from_email, [to_email], msg.as_string())
    print("EMAIL_SENT_OK")

def collect_all(days: int = 7) -> List[Item]:
    out: List[Item] = []

    # Pull from your listed sources
    for s in SOURCES:
        try:
            if s["type"] == "rss_guess":
                out.extend(parse_rss(s["name"], s["url"]))
            elif s["type"] == "html_rvbusiness":
                out.extend(parse_html_rvbusiness(s["name"], s["url"]))
            elif s["type"] == "html_koa_press":
                out.extend(parse_html_koa_press(s["name"], s["url"]))
            elif s["type"] in ("html_sun_news", "html_els_news"):
                out.extend(parse_html_simple_list(s["name"], s["url"]))
        except Exception as e:
            # Don't kill the whole run because one site is flaky.
            print(f"[WARN] Failed source {s['name']}: {e}")

    # Google News RSS queries
    for q in GOOGLE_NEWS_QUERIES:
        try:
            url = google_news_rss_url(q["q"])
            out.extend(parse_rss(f"Google News: {q['name']}", url))
        except Exception as e:
            print(f"[WARN] Failed Google News query {q['name']}: {e}")

    # Filter to window + dedupe by URL
    filtered = []
    seen = set()
    for it in out:
        dt = it.published
        if dt.tzinfo is None:
            dt = ET.localize(dt)
        it.published = dt
        if not within_days(it.published, days):
            continue
        if it.url in seen:
            continue
        seen.add(it.url)
        filtered.append(it)

    return filtered

def main():
    items = collect_all(days=7)

    buckets: Dict[str, List[Item]] = {}
    for it in items:
        cats = categorize(it)
        for c in cats:
            buckets.setdefault(c, []).append(it)

    subject = f"Athena RV Park Weekly Digest — {datetime.now(tz=ET).strftime('%b %d, %Y')}"
    html = build_email_html(buckets)
    send_email(subject, html)
    print(f"Sent digest with {len(items)} items across {len(buckets)} categories.")

if __name__ == "__main__":
    main()
