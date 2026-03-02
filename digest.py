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
from collections import Counter

import pytz
import requests
import feedparser
from bs4 import BeautifulSoup
from dateutil import parser as dtparser

from pydantic import BaseModel, Field
from openai import OpenAI  # openai>=1.0.0

from sources import SOURCES, GOOGLE_NEWS_QUERIES, google_news_rss_url

ET = pytz.timezone("America/New_York")

# ============ TUNABLES ============
MAX_ITEMS_PER_CATEGORY_IN_EMAIL = 35   # keep email readable
MAX_ITEMS_FOR_AI_EXEC_SUMMARY = 10     # top N used to infer category themes
AI_ARTICLE_BATCH_SIZE = 12             # summaries per API call (keeps token use sane)
AI_SNIPPET_CHARS = 220                 # snippet length sent to AI per item
AI_ARTICLE_SUMMARY_MAX_WORDS = 35      # keep summaries short


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

NON_US_HINTS = [
    "australia", "western australia", "queensland", "new south wales", "victoria",
    "canada", "ontario", "british columbia", "alberta",
    "united kingdom", "uk", "england", "scotland", "wales",
    "ireland", "new zealand",
    "europe", "germany", "france", "spain", "italy",
    "south africa", "india",
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

    if any(nu in text for nu in NON_US_HINTS):
        return False
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
        "demand", "development", "expansion", "booking", "reservation"
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
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    return r.text


def parse_rss(source_name: str, url: str) -> List[Item]:
    xml = fetch_url(url)
    feed = feedparser.parse(xml)
    items: List[Item] = []
    for e in feed.entries[:220]:
        title = (e.get("title") or "").strip()
        link = (e.get("link") or "").strip()
        published = safe_dt(e.get("published") or e.get("updated"))
        if not (title and link and published):
            continue
        summary_html = e.get("summary", "") or ""
        summary = BeautifulSoup(summary_html, "lxml").get_text(" ", strip=True)
        items.append(Item(source=source_name, title=title, url=link, published=published, summary=summary))
    return items


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

        context = (a.parent.get_text(" ", strip=True) if a.parent else "")[:700]
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
            elif s["type"] == "html_simple_dates":
                out.extend(parse_html_simple_dates(s["name"], s["url"]))
            else:
                out.extend(parse_rss(s["name"], s["url"]))
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
# AI: EXEC SUMMARY + PER-ARTICLE SUMMARIES
# ----------------------------
IMPORTANT_TERMS = [
    "acquire", "acquisition", "acquired", "portfolio", "transaction", "for sale", "listed",
    "lawsuit", "litigation", "zoning", "ordinance", "permit", "injunction",
    "insurance", "premium", "underwriting", "liability", "claims",
    "financing", "refinance", "loan", "lender", "debt", "cap rate", "interest rate",
    "earnings", "guidance", "results", "10-q", "10-k", "8-k",
    "bankruptcy", "foreclosure", "default",
]


def importance_score(it: Item) -> int:
    t = (it.title + " " + (it.summary or "")).lower()
    score = 0
    for w in IMPORTANT_TERMS:
        if w in t:
            score += 2
    if any(x in t for x in ["sun communities", "sun outdoors", "equity lifestyle", "els", "koa"]):
        score += 2
    return score


def get_openai_client() -> Optional[OpenAI]:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        return None
    return OpenAI(api_key=api_key)


def ai_exec_summary(category: str, items: List[Item], client: OpenAI, model: str) -> str:
    ranked = sorted(items, key=lambda x: (importance_score(x), x.published), reverse=True)[:MAX_ITEMS_FOR_AI_EXEC_SUMMARY]
    lines = []
    for it in ranked:
        d = it.published.astimezone(ET).strftime("%b %d")
        snippet = re.sub(r"\s+", " ", (it.summary or "").strip())[:180]
        lines.append(f"- {d} | {it.title} | {snippet}")

    prompt = (
        "Write an executive summary for Athena Real Estate.\n"
        "Scope: STRICTLY US RV parks / RV resorts / campgrounds.\n"
        f"Category: {category}\n\n"
        "Output format:\n"
        "Summary: <2 sentences max>\n"
        "Notable: <1–2 titles only>\n\n"
        "Rules: No links. No sources. No fluff. No speculation.\n\n"
        "Headlines:\n" + "\n".join(lines)
    )

    try:
        resp = client.responses.create(
            model=model,
            input=prompt,
            max_output_tokens=160,
        )
        text = (resp.output_text or "").strip()
        return BeautifulSoup(text, "lxml").get_text(" ", strip=True)
    except Exception as e:
        print(f"[WARN] AI exec summary failed for '{category}': {e}")
        return ""


# Structured output models for per-article summaries
class ArticleSummaryOut(BaseModel):
    url: str = Field(..., description="Exact URL from input")
    summary: str = Field(..., description="1–2 sentence summary (<=35 words), no links")


class ArticleSummaryBatchOut(BaseModel):
    summaries: List[ArticleSummaryOut]


def ai_article_summaries(items: List[Item], client: OpenAI, model: str) -> Dict[str, str]:
    """
    Returns {url: summary} for the given items.
    Uses structured outputs via responses.parse for reliable JSON-schema-constrained output. :contentReference[oaicite:1]{index=1}
    """
    if not items:
        return {}

    # Build compact input
    blocks = []
    for it in items:
        snippet = re.sub(r"\s+", " ", (it.summary or "").strip())[:AI_SNIPPET_CHARS]
        blocks.append(
            f"URL: {it.url}\n"
            f"TITLE: {it.title}\n"
            f"SNIPPET: {snippet}\n"
        )

    system = (
        "You summarize US RV park / campground news for executives.\n"
        "Return a 1–2 sentence summary for each item.\n"
        "Rules:\n"
        f"- Max {AI_ARTICLE_SUMMARY_MAX_WORDS} words per summary.\n"
        "- No links.\n"
        "- Do not invent facts not supported by TITLE/SNIPPET.\n"
        "- If the snippet is thin, write a cautious summary like 'Headline indicates ...'.\n"
    )
    user = "Summarize each item:\n\n" + "\n---\n".join(blocks)

    try:
        # responses.parse enforces the schema (structured outputs)
        resp = client.responses.parse(
            model=model,
            input=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            text_format=ArticleSummaryBatchOut,
        )
        parsed: ArticleSummaryBatchOut = resp.output_parsed
        out = {}
        for s in parsed.summaries:
            out[s.url] = BeautifulSoup((s.summary or "").strip(), "lxml").get_text(" ", strip=True)
        return out
    except Exception as e:
        print(f"[WARN] AI article summaries failed: {e}")
        return {}


def chunked(lst: List[Item], n: int) -> List[List[Item]]:
    return [lst[i:i + n] for i in range(0, len(lst), n)]


# ----------------------------
# EMAIL BUILD / SEND (MULTI-RECIPIENT)
# ----------------------------
def build_email_html(
    items_by_cat: Dict[str, List[Item]],
    exec_summaries: Dict[str, str],
    article_summaries: Dict[str, str],
) -> str:
    now = datetime.now(tz=ET)
    start = (now - timedelta(days=7)).strftime("%b %d, %Y")
    end = now.strftime("%b %d, %Y")

    parts = [
        "<h2>Athena RV Park Weekly Digest (US-only, strict)</h2>",
        f"<p><b>Window:</b> {start} – {end}</p>",
    ]

    if not items_by_cat:
        parts.append("<p><b>No qualifying US RV-park items found this week.</b></p>")
        return "\n".join(parts)

    for cat in sorted(items_by_cat.keys(), key=lambda c: len(items_by_cat[c]), reverse=True):
        items = sorted(items_by_cat[cat], key=lambda x: x.published, reverse=True)[:MAX_ITEMS_PER_CATEGORY_IN_EMAIL]
        parts.append(f"<h3>{cat} ({len(items_by_cat[cat])})</h3>")

        # Executive summary (category-level)
        ex = exec_summaries.get(cat, "").strip()
        if ex:
            ex = ex.replace("Summary:", "<b>Executive summary:</b>").replace("Notable:", "<br><b>Notable:</b>")
            parts.append(f"<p>{ex}</p>")

        parts.append("<ul>")
        for it in items:
            d = it.published.astimezone(ET).strftime("%b %d, %Y")
            s = article_summaries.get(it.url, "").strip()
            s_html = f"<br><span style='color:#444'>{s}</span>" if s else ""
            parts.append(
                f"<li><b>{d}</b> — <a href=\"{it.url}\">{it.title}</a> "
                f"<i>({it.source})</i>{s_html}</li>"
            )
        parts.append("</ul>")

    parts.append("<p style='color:#666;font-size:12px'>Automated weekly digest via GitHub Actions.</p>")
    return "\n".join(parts)


def send_email(subject: str, html_body: str):
    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))

    username = os.environ["SMTP_USERNAME"].strip()
    password = os.environ["SMTP_PASSWORD"].strip().replace("\u00A0", "").replace(" ", "")

    from_email = os.environ.get("FROM_EMAIL", username).strip()

    to_emails_raw = os.environ["TO_EMAIL"].strip()
    to_emails = [e.strip() for e in to_emails_raw.split(",") if e.strip()]
    if not to_emails:
        raise RuntimeError("TO_EMAIL is empty. Provide one or more emails, comma-separated.")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_email
    msg["To"] = ", ".join(to_emails)
    msg.attach(MIMEText(html_body, "html"))

    context = ssl.create_default_context()
    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.starttls(context=context)
        server.login(username, password)
        server.sendmail(from_email, to_emails, msg.as_string())

    print(f"EMAIL_SENT_OK to {len(to_emails)} recipients")


def main():
    items = collect_all(days=7)
    before = len(items)
    items = [it for it in items if is_strict_us_rvpark(it)]
    after = len(items)
    print(f"Collected {before} items; {after} passed strict US RV-park filter.")

    # Categorize
    buckets: Dict[str, List[Item]] = {}
    for it in items:
        for c in categorize(it):
            buckets.setdefault(c, []).append(it)

    # AI client
    client = get_openai_client()
    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini").strip()

    exec_summaries: Dict[str, str] = {}
    article_summaries: Dict[str, str] = {}

    if client is None:
        print("[WARN] OPENAI_API_KEY missing; AI summaries disabled.")
    else:
        # Category exec summaries (one call per category)
        for cat, its in buckets.items():
            s = ai_exec_summary(cat, its, client, model)
            if s:
                exec_summaries[cat] = s

        # Per-article summaries: summarize each unique URL once, in batches
        unique_items = list({it.url: it for it in items}.values())
        # Prefer important/recent items first (helps if you later cap)
        unique_items = sorted(unique_items, key=lambda x: (importance_score(x), x.published), reverse=True)

        for batch in chunked(unique_items, AI_ARTICLE_BATCH_SIZE):
            article_summaries.update(ai_article_summaries(batch, client, model))

        print(f"AI summarized {len(article_summaries)}/{len(unique_items)} unique articles.")

    subject = f"Athena RV Park Weekly Digest (US-only, strict) — {datetime.now(tz=ET).strftime('%b %d, %Y')}"
    html = build_email_html(buckets, exec_summaries, article_summaries)
    send_email(subject, html)
    print(f"Sent digest with {after} filtered items across {len(buckets)} categories.")


if __name__ == "__main__":
    main()
