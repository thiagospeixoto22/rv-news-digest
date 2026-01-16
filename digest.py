# digest.py
import os
import re
import ssl
import smtplib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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

# OpenAI SDK (Responses API)
try:
    from openai import OpenAI
except Exception:
    OpenAI = None

ET = pytz.timezone("America/New_York")


@dataclass
class Item:
    source: str
    title: str
    url: str
    published: datetime
    summary: str = ""


# ----------------------------
# STRICT RV-PARK + US FILTER
# ----------------------------

MUST_HAVE_ANY = [
    "rv park", "rv parks",
    "rv resort", "rv resorts",
    "rv campground", "rv campgrounds",
    "campground", "campgrounds",
    "recreation vehicle park",
    "koa",
]

REJECT_IF_ANY = [
    "travel trailer", "fifth wheel", "motorhome", "pickup truck", "tow vehicle",
    "airstream", "campervan", "van life", "vanlife",
    "rv review", "rv show", "rv expo", "dealership", "dealer",
    "msrp", "new model", "recall",
    "best rv", "top rv", "rv tips", "rv maintenance",
]

US_OPERATOR_OK = [
    "koa",
    "sun communities", "sun outdoors", "sui",
    "equity lifestyle", "equity lifestyle properties", "els",
    "rhp properties",
]

US_HINTS = [
    "united states", "u.s.", "usa", "american",
    "county", "city of", "state of",
] + [
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana",
    "maine", "maryland", "massachusetts", "michigan", "minnesota",
    "mississippi", "missouri", "montana", "nebraska", "nevada",
    "new hampshire", "new jersey", "new mexico", "new york",
    "north carolina", "north dakota", "ohio", "oklahoma", "oregon",
    "pennsylvania", "rhode island", "south carolina", "south dakota",
    "tennessee", "texas", "utah", "vermont", "virginia", "washington",
    "west virginia", "wisconsin", "wyoming",
]

STATE_ABBR = [
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN","IA","KS","KY","LA",
    "ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ","NM","NY","NC","ND","OH","OK",
    "OR","PA","RI","SC","SD","TN","TX","UT","VT","VA","WA","WV","WI","WY"
]


def has_state_abbr(text: str) -> bool:
    t = " " + text.upper() + " "
    for ab in STATE_ABBR:
        if f" {ab} " in t or f", {ab} " in t or f"({ab})" in t:
            return True
    return False


def is_strict_us_rvpark(item: Item) -> bool:
    text = (f"{item.title} {item.summary}").lower()
    text = re.sub(r"\s+", " ", text).strip()

    if not any(k in text for k in MUST_HAVE_ANY):
        return False

    if any(bad in text for bad in REJECT_IF_ANY):
        return False

    if not any(op in text for op in US_OPERATOR_OK):
        if not any(h in text for h in US_HINTS) and not has_state_abbr(text):
            return False

    return True


# ----------------------------
# CATEGORY TAGGING
# ----------------------------
KEYWORDS = {
    "Acquisitions / For Sale": [
        "acquisition", "acquired", "merger", "portfolio", "for sale", "listed",
        "broker", "transaction", "deal", "sale-leaseback"
    ],
    "Insurance / Risk": [
        "insurance", "insurer", "premium", "underwriting", "liability", "risk",
        "claim", "wildfire", "flood", "hurricane"
    ],
    "Legal / Zoning": [
        "zoning", "ordinance", "lawsuit", "litigation", "permit",
        "planning commission", "code enforcement", "injunction"
    ],
    "Financing / Markets": [
        "financing", "refinancing", "loan", "lender", "debt", "cap rate",
        "interest rate", "bond"
    ],
    "Earnings / Public Companies": [
        "earnings", "guidance", "conference call", "results",
        "10-q", "10-k", "8-k", "sec filing"
    ],
    "Operations / Industry": [
        "occupancy", "rates", "revenue", "revpar", "reservations",
        "demand", "development", "expansion"
    ],
    "People / Notable": [
        "ceo", "founder", "appointed", "resigns", "retired",
        "death", "dies", "passed away", "obituary"
    ],
}


def categorize(item: Item) -> List[str]:
    hay = (item.title + " " + item.summary).lower()
    tags = []
    for cat, words in KEYWORDS.items():
        if any(w in hay for w in words):
            tags.append(cat)
    return tags or ["Other"]


# ----------------------------
# FETCH / PARSE HELPERS
# ----------------------------

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
    headers = {"User-Agent": "Mozilla/5.0 (compatible; AthenaRVNewsBot/1.0)"}
    r = requests.get(url, headers=headers, timeout=25)
    r.raise_for_status()
    return r.text


def parse_rss(source_name: str, url: str) -> List[Item]:
    xml = fetch_url(url)
    feed = feedparser.parse(xml)
    items: List[Item] = []

    for e in feed.entries[:160]:
        title = (e.get("title") or "").strip()
        link = (e.get("link") or "").strip()
        published = safe_dt(e.get("published") or e.get("updated"))
        if not (title and link and published):
            continue

        summary_html = e.get("summary", "") or ""
        summary = BeautifulSoup(summary_html, "lxml").get_text(" ", strip=True)
        items.append(Item(source=source_name, title=title, url=link, published=published, summary=summary))

    return items


def parse_html_rvbusiness(source_name: str, url: str) -> List[Item]:
    html = fetch_url(url)
    soup = BeautifulSoup(html, "lxml")
    items: List[Item] = []

    date_re = re.compile(
        r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+\d{4}"
    )

    for a in soup.select("a"):
        href = a.get("href") or ""
        text = a.get_text(" ", strip=True)
        if not href or not text:
            continue
        if href.startswith("/"):
            href = urljoin(url, href)
        if "rvbusiness.com" not in href:
            continue
        if len(text) < 12:
            continue

        parent_text = a.parent.get_text(" ", strip=True) if a.parent else ""
        m = date_re.search(parent_text)
        if not m:
            continue
        published = safe_dt(m.group(0))
        if not published:
            continue

        items.append(Item(source=source_name, title=text, url=href, published=published))

    uniq: Dict[str, Item] = {}
    for it in items:
        uniq[it.url] = it
    return list(uniq.values())


def parse_html_simple_dates(source_name: str, url: str) -> List[Item]:
    html = fetch_url(url)
    soup = BeautifulSoup(html, "lxml")
    items: List[Item] = []

    date_regex = re.compile(
        r"\b\d{1,2}/\d{1,2}/\d{2,4}\b|\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},\s+\d{4}\b"
    )

    base_host = url.split("/")[2]

    for a in soup.select("a"):
        txt = a.get_text(" ", strip=True)
        href = a.get("href") or ""
        if not txt or not href:
            continue
        if href.startswith("/"):
            href = urljoin(url, href)

        if base_host not in href:
            continue

        context = (a.parent.get_text(" ", strip=True) if a.parent else "")[:400]
        m = date_regex.search(context)
        if not m:
            continue

        published = safe_dt(m.group(0))
        if not published:
            continue

        items.append(Item(source=source_name, title=txt, url=href, published=published))

    uniq: Dict[str, Item] = {}
    for it in items:
        uniq[it.url] = it
    return list(uniq.values())


def collect_all(days: int = 7) -> List[Item]:
    out: List[Item] = []

    for s in SOURCES:
        try:
            if s["type"] == "rss":
                out.extend(parse_rss(s["name"], s["url"]))
            elif s["type"] == "html_rvbusiness":
                out.extend(parse_html_rvbusiness(s["name"], s["url"]))
            elif s["type"] == "html_simple_dates":
                out.extend(parse_html_simple_dates(s["name"], s["url"]))
        except Exception as e:
            print(f"[WARN] Failed source {s['name']}: {e}")

    for q in GOOGLE_NEWS_QUERIES:
        try:
            url = google_news_rss_url(q["q"])
            out.extend(parse_rss(f"Google News: {q['name']}", url))
        except Exception as e:
            print(f"[WARN] Failed Google News query {q['name']}: {e}")

    filtered: List[Item] = []
    seen = set()

    for it in out:
        dt = it.published
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        it.published = dt

        if not within_days(it.published.astimezone(ET), days):
            continue
        if it.url in seen:
            continue
        seen.add(it.url)
        filtered.append(it)

    return filtered


# ----------------------------
# AI: ONE SUMMARY PER CATEGORY
# ----------------------------

def ai_category_summary(category: str, items: List[Item]) -> str:
    """
    Returns a VERY short category summary.
    - ONE API call per category
    - Uses only title/source/date lines (no per-article summaries)
    - Falls back to "" on any error
    """
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key or OpenAI is None:
        return ""

    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini").strip()

    # Limit what we send to keep cost low and keep it "category level"
    items_sorted = sorted(items, key=lambda x: x.published, reverse=True)[:12]
    lines = []
    for it in items_sorted:
        d = it.published.astimezone(ET).strftime("%b %d, %Y")
        lines.append(f"- {d} | {it.source} | {it.title}")

    prompt = (
        f"Category: {category}\n"
        f"Audience: Athena Real Estate (US RV park / campground industry).\n"
        f"Task: Write a VERY SHORT category-level summary (NOT per-article summaries).\n"
        f"Rules:\n"
        f"- Output 1–2 sentences OR 2 bullets max.\n"
        f"- Mention only what is supported by the headlines.\n"
        f"- Keep it strictly US RV parks/campgrounds.\n"
        f"- Do not include links.\n\n"
        f"Headlines:\n" + "\n".join(lines)
    )

    try:
        client = OpenAI(api_key=api_key)
        resp = client.responses.create(
            model=model,
            input=prompt,
            max_output_tokens=120,
        )
        text = getattr(resp, "output_text", "") or ""
        return text.strip()
    except Exception as e:
        print(f"[WARN] AI summary failed for {category}: {e}")
        return ""


# ----------------------------
# EMAIL BUILD / SEND
# ----------------------------

def build_email_html(items_by_cat: Dict[str, List[Item]]) -> str:
    now = datetime.now(tz=ET)
    start = (now - timedelta(days=7)).strftime("%b %d, %Y")
    end = now.strftime("%b %d, %Y")

    parts = [
        f"<h2>Athena RV Park Weekly Digest (US-only, strict)</h2>",
        f"<p><b>Window:</b> {start} – {end}</p>",
    ]

    if not items_by_cat:
        parts.append("<p><b>No qualifying US RV-park items found this week.</b></p>")
        return "\n".join(parts)

    # Categories sorted by item count desc
    for cat in sorted(items_by_cat.keys(), key=lambda c: len(items_by_cat[c]), reverse=True):
        items = items_by_cat[cat]
        parts.append(f"<h3>{cat} ({len(items)})</h3>")

        # AI summary (short)
        summary = ai_category_summary(cat, items)
        if summary:
            parts.append(f"<p><b>Quick summary:</b> {BeautifulSoup(summary, 'lxml').get_text(' ', strip=True)}</p>")

        parts.append("<ul>")
        for it in sorted(items, key=lambda x: x.published, reverse=True)[:20]:
            d = it.published.astimezone(ET).strftime("%b %d, %Y")
            parts.append(
                f'<li><b>{d}</b> — <a href="{it.url}">{it.title}</a> '
                f'<i>({it.source})</i></li>'
            )
        parts.append("</ul>")

    parts.append("<p style='color:#666;font-size:12px'>Automated weekly digest via GitHub Actions.</p>")
    return "\n".join(parts)


def send_email(subject: str, html_body: str):
    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))

    username = os.environ["SMTP_USERNAME"].strip()
    password = os.environ["SMTP_PASSWORD"].strip().replace("\u00A0", "").replace(" ", "")

    to_email = os.environ["TO_EMAIL"].strip()
    from_email = os.environ.get("FROM_EMAIL", username).strip()

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


def main():
    items = collect_all(days=7)
    before = len(items)
    items = [it for it in items if is_strict_us_rvpark(it)]
    after = len(items)

    print(f"Collected {before} items; {after} passed strict US RV-park filter.")

    buckets: Dict[str, List[Item]] = {}
    for it in items:
        for c in categorize(it):
            buckets.setdefault(c, []).append(it)

    subject = f"Athena RV Park Weekly Digest (US-only, strict) — {datetime.now(tz=ET).strftime('%b %d, %Y')}"
    html = build_email_html(buckets)
    send_email(subject, html)

    print(f"Sent digest with {after} filtered items across {len(buckets)} categories.")


if __name__ == "__main__":
    main()
