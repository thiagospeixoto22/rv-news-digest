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
from zoneinfo import ZoneInfo

import pytz
import requests
import feedparser
from bs4 import BeautifulSoup
from dateutil import parser as dtparser

from sources import SOURCES, GOOGLE_NEWS_QUERIES, google_news_rss_url

ET = pytz.timezone("America/New_York")

# Scheduling - send whenever the schedule job runs
SEND_WEEKDAYS = {1, 4}  # Tue=1, Fri=4

def should_send_now() -> bool:
    """
    Scheduled runs: send on Tue/Fri even if GitHub runs late (fixes missed sends).
    Manual runs (workflow_dispatch): only send if FORCE_SEND=1.
    """
    event = os.environ.get("GITHUB_EVENT_NAME", "").strip()

    # If you manually click "Run workflow", require FORCE_SEND=1 to actually send.
    if event == "workflow_dispatch":
        return os.environ.get("FORCE_SEND", "").strip() == "1"

    now = datetime.now(ZoneInfo("America/New_York"))
    return now.weekday() in SEND_WEEKDAYS
    
def effective_days_back() -> int:
    """
    Tue digest covers since Friday (~4 days).
    Fri digest covers since Tuesday (~3 days).
    Override with DAYS_BACK_OVERRIDE if needed.
    """
    override = os.environ.get("DAYS_BACK_OVERRIDE", "").strip()
    if override.isdigit():
        return int(override)

    now = datetime.now(ZoneInfo("America/New_York"))
    if now.weekday() == 1:  # Tuesday
        return 4
    if now.weekday() == 4:  # Friday
        return 3
    return 7


# Tunables
MAX_ITEMS_PER_CATEGORY_IN_EMAIL = 40
ARTICLE_SUMMARY_SENTENCES = 2
CATEGORY_NOTABLE_COUNT = 2
FETCH_TIMEOUT = 35
FETCH_RETRIES = 2

# Dedupe tuning (higher = stricter duplicate match)
DEDUP_JACCARD_THRESHOLD = 0.92
DEDUP_MIN_SHARED_TOKENS = 5


@dataclass
class Item:
    source: str
    title: str
    url: str
    published: datetime
    summary: str = ""


# STRICT RV-PARK + US FILTER
MUST_HAVE_ANY = [
    "rv park", "rv parks",
    "rv resort", "rv resorts",
    "rv campground", "rv campgrounds",
    "campground", "campgrounds",
    "recreation vehicle park",
    "koa",
]

REJECT_IF_ANY = [
    # RV vehicle / travel content (not RV parks)
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

    # US-only requirement (unless it’s a known US operator mention)
    if not any(op in text for op in US_OPERATOR_OK):
        if not any(h in text for h in US_HINTS) and not has_state_abbr(text):
            return False

    return True


# Priority order for ties and display; Other forced last in display
CATEGORY_PRIORITY = [
    "Acquisitions / For Sale",
    "Financing / Markets",
    "Insurance / Risk",
    "Legal / Zoning",
    "Earnings / Public Companies",
    "Operations / Industry",
    "People / Notable",
    "Other",
]

CATEGORY_RULES = {
    "Acquisitions / For Sale": {
        "strong": ["for sale", "portfolio", "acquired", "acquisition", "sold", "sale-leaseback"],
        "keywords": ["broker", "listing", "listed", "transaction", "deal", "marketed", "buyer", "seller", "closed"],
        "exclude": ["earnings call", "10-q", "10-k", "8-k"],
    },
    "Financing / Markets": {
        "strong": ["refinance", "refinancing", "loan", "lender", "debt", "credit facility"],
        "keywords": ["interest rate", "cap rate", "financing", "bond", "cmbs", "maturity", "spread"],
        "exclude": [],
    },
    "Insurance / Risk": {
        "strong": ["insurance", "insurer", "premium", "underwriting", "policy"],
        "keywords": ["liability", "coverage", "claims", "risk", "wildfire", "flood", "hurricane"],
        "exclude": [],
    },
    "Legal / Zoning": {
        "strong": ["lawsuit", "litigation", "zoning", "ordinance", "permit"],
        "keywords": ["planning commission", "code enforcement", "injunction", "settlement", "sued", "appeal", "regulation"],
        "exclude": [],
    },
    "Earnings / Public Companies": {
        "strong": ["earnings", "guidance", "results", "conference call", "10-q", "10-k", "8-k"],
        "keywords": ["sec", "supplemental", "investor", "quarter", "revenue", "noi", "ffo"],
        "exclude": [],
    },
    "Operations / Industry": {
        "strong": ["occupancy", "reservations", "booking", "demand", "expansion", "development", "construction"],
        "keywords": ["upgrade", "renovation", "amenities", "opens", "opening", "groundbreaking", "survey", "report", "trend", "rvia"],
        "exclude": [],
    },
    "People / Notable": {
        "strong": ["passed away", "dies", "died", "death", "obituary"],
        "keywords": ["appointed", "named", "ceo", "cfo", "president", "founder", "resigns", "retire", "steps down"],
        "exclude": ["earnings", "10-q", "10-k", "8-k"],  # keep exec quotes in filings from stealing category
    },
}

def _score_category(text: str, title: str, cat: str) -> int:
    """
    Strict-ish scoring:
    - strong hits: title +6, body +3
    - keyword hits: title +3, body +1
    - exclude hits: -6 (penalty)
    """
    rule = CATEGORY_RULES.get(cat, {})
    strong = rule.get("strong", [])
    keywords = rule.get("keywords", [])
    exclude = rule.get("exclude", [])

    score = 0

    for w in strong:
        if w in title:
            score += 6
        elif w in text:
            score += 3

    for w in keywords:
        if w in title:
            score += 3
        elif w in text:
            score += 1

    for w in exclude:
        if w in title or w in text:
            score -= 6

    return score

def best_category(item: Item) -> str:
    """
    Assign each item to exactly ONE category (best fit).
    """
    title = re.sub(r"\s+", " ", (item.title or "")).strip().lower()
    text = re.sub(r"\s+", " ", f"{item.title} {item.summary}".lower()).strip()

    scores = {}
    for cat in CATEGORY_RULES.keys():
        scores[cat] = _score_category(text=text, title=title, cat=cat)

    # choose best
    best_cat = max(scores.keys(), key=lambda c: scores[c])
    best_score = scores[best_cat]

    # If nothing matched meaningfully, use Other.
    # Keep threshold low so relevant items don't get dumped.
    if best_score < 2:
        return "Other"

    # Tie-breaker: if multiple have same score, pick earlier priority
    tied = [c for c, s in scores.items() if s == best_score]
    if len(tied) > 1:
        tied.sort(key=lambda c: CATEGORY_PRIORITY.index(c) if c in CATEGORY_PRIORITY else 999)
        return tied[0]

    return best_cat


# FREE 1–2 SENTENCE ARTICLE SUMMARY
STOPWORDS = {
    "the","a","an","and","or","to","of","in","for","on","with","from","by","at","as",
    "is","are","was","were","be","been","it","its","this","that","these","those",
    "will","after","before","about","over","into","new","news","says","report","reports",
    "rv","park","parks","campground","campgrounds","resort","resorts"
}

def split_sentences(text: str) -> List[str]:
    text = re.sub(r"\s+", " ", (text or "").strip())
    if not text:
        return []
    sents = re.split(r"(?<=[.!?])\s+", text)
    return [s.strip() for s in sents if len(s.strip()) >= 25]

def free_article_summary(title: str, snippet: str, max_sentences: int = 2) -> str:
    snippet = BeautifulSoup(snippet or "", "lxml").get_text(" ", strip=True)
    sents = split_sentences(snippet)

    if not sents:
        clean_title = re.sub(r"\s+", " ", (title or "").strip())
        return f"Headline indicates: {clean_title}."

    words = re.findall(r"[a-z]{3,}", snippet.lower())
    freq = Counter(w for w in words if w not in STOPWORDS)

    scored = []
    for i, s in enumerate(sents):
        wds = re.findall(r"[a-z]{3,}", s.lower())
        score = sum(freq.get(w, 0) for w in wds)
        scored.append((score, i, s))

    top = sorted(scored, key=lambda x: x[0], reverse=True)[:max_sentences]
    top = sorted(top, key=lambda x: x[1])
    out = " ".join(t[2] for t in top).strip()
    return out[:420].rstrip()


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

def free_category_exec_summary(category: str, items: List[Item]) -> str:
    if not items:
        return "No notable updates surfaced in this category this window.", []

    text = " ".join((it.title + " " + it.summary) for it in items).lower()

    themes = []
    if category == "Acquisitions / For Sale":
        themes.append("deal/listing activity (acquisitions, sales, or broker listings)")
    elif category == "Financing / Markets":
        themes.append("financing/refi dynamics (loans, rates, debt, cap rates)")
    elif category == "Insurance / Risk":
        themes.append("insurance market / risk items (premiums, coverage, claims, weather risk)")
    elif category == "Legal / Zoning":
        themes.append("local legal/zoning actions affecting RV parks/campgrounds")
    elif category == "Earnings / Public Companies":
        themes.append("public-company commentary touching outdoor hospitality / RV operations")
    elif category == "Operations / Industry":
        themes.append("operations & industry signals (occupancy, demand, expansion, trends)")
    elif category == "People / Notable":
        themes.append("notable leadership / people updates relevant to the industry")
    else:
        themes.append("miscellaneous RV park/campground items that didn’t fit other buckets cleanly")

    state_mentions = []
    for it in items:
        t = " " + it.title.upper() + " "
        for ab in STATE_ABBR:
            if f", {ab} " in t or f"({ab})" in t:
                state_mentions.append(ab)
    top_states = [s for s, _ in Counter(state_mentions).most_common(3)]
    states_txt = f" Mentions clustered around {', '.join(top_states)}." if top_states else ""

    summary = (
        f"Headlines indicate {themes[0]}{states_txt} "
        f"Items below include the most relevant updates in this category."
    )

    ranked = sorted(items, key=lambda x: (importance_score(x), x.published), reverse=True)
    notable = [it.title for it in ranked[:CATEGORY_NOTABLE_COUNT]]
    return summary, notable


# Cross-source duplicate removal (same story, different links)
TITLE_STOPWORDS = {
    "the","a","an","and","or","to","of","in","for","on","with","from","by","at","as",
    "is","are","was","were","be","been","this","that","these","those","will",
}

def normalize_title(title: str) -> str:
    t = BeautifulSoup(title or "", "lxml").get_text(" ", strip=True)
    t = re.sub(r"\s+-\s+[^-]{2,}$", "", t).strip()  # drop trailing " - Publisher"
    t = t.lower()
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t

def title_tokens(norm_title: str) -> set:
    toks = [w for w in norm_title.split() if w and w not in TITLE_STOPWORDS]
    return set(toks)

def jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    u = a | b
    if not u:
        return 0.0
    return len(a & b) / len(u)

def source_priority(it: Item) -> int:
    s = (it.source or "").lower()
    pri = 0
    if "google news" in s:
        pri -= 5
    pri += min(len(it.summary or ""), 400) // 200
    return pri

def dedupe_cross_source(items: List[Item]) -> List[Item]:
    items_sorted = sorted(items, key=lambda it: (it.published, source_priority(it)), reverse=True)

    kept: List[Item] = []
    kept_norm: List[str] = []
    kept_tok: List[set] = []

    for it in items_sorted:
        nt = normalize_title(it.title)
        tt = title_tokens(nt)

        if nt in kept_norm:
            continue

        dup = False
        for existing_tokens in kept_tok:
            sim = jaccard(tt, existing_tokens)
            if sim >= DEDUP_JACCARD_THRESHOLD and len(tt & existing_tokens) >= DEDUP_MIN_SHARED_TOKENS:
                dup = True
                break

        if not dup:
            kept.append(it)
            kept_norm.append(nt)
            kept_tok.append(tt)

    return sorted(kept, key=lambda it: it.published, reverse=True)


# FETCH / PARSE HELPERS
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
        "User-Agent": "Mozilla/5.0 (compatible; AthenaRVNewsBot/1.0)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    last_err = None
    for _ in range(FETCH_RETRIES + 1):
        try:
            r = requests.get(url, headers=headers, timeout=FETCH_TIMEOUT)
            r.raise_for_status()
            return r.text
        except Exception as e:
            last_err = e
    raise last_err

def parse_rss(source_name: str, url: str) -> List[Item]:
    xml = fetch_url(url)
    feed = feedparser.parse(xml)
    items: List[Item] = []
    for e in feed.entries[:250]:
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

        items.append(Item(source=source_name, title=txt, url=href, published=published, summary=""))

    uniq: Dict[str, Item] = {}
    for it in items:
        uniq[it.url] = it
    return list(uniq.values())

def collect_all(days: int) -> List[Item]:
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


# EMAIL BUILD / SEND (MULTI-RECIPIENT) + text/plain alternative
def html_to_text(html: str) -> str:
    # Basic html -> text for deliverability
    soup = BeautifulSoup(html or "", "lxml")
    text = soup.get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text

def build_email_html(items_by_cat: Dict[str, List[Item]], days_back: int) -> str:
    now = datetime.now(tz=ET)
    start = (now - timedelta(days=days_back)).strftime("%b %d, %Y")
    end = now.strftime("%b %d, %Y")

    parts = [
        "<h2>Athena RV Park Digest (US-only, strict)</h2>",
        f"<p><b>Window:</b> {start} – {end}</p>",
    ]

    if not items_by_cat:
        parts.append("<p><b>No qualifying US RV-park items found in this window.</b></p>")
        return "\n".join(parts)

    # Sort categories by priority; force Other last even if it has many items
    ordered = sorted(items_by_cat.keys(), key=lambda c: (c == "Other", CATEGORY_PRIORITY.index(c) if c in CATEGORY_PRIORITY else 999))

    for cat in ordered:
        items = sorted(items_by_cat[cat], key=lambda x: x.published, reverse=True)
        parts.append(f"<h3>{cat} ({len(items)})</h3>")

        exec_sum, notable = free_category_exec_summary(cat, items)
        notable_html = ""
        if notable:
            notable_html = "<br><b>Notable:</b> " + " | ".join([BeautifulSoup(t, "lxml").get_text(" ", strip=True) for t in notable])
        parts.append(f"<p><b>Executive summary:</b> {exec_sum}{notable_html}</p>")

        parts.append("<ul>")
        for it in items[:MAX_ITEMS_PER_CATEGORY_IN_EMAIL]:
            d = it.published.astimezone(ET).strftime("%b %d, %Y")
            art_sum = free_article_summary(it.title, it.summary, max_sentences=ARTICLE_SUMMARY_SENTENCES)
            art_sum = BeautifulSoup(art_sum, "lxml").get_text(" ", strip=True)
            parts.append(
                f'<li><b>{d}</b> — <a href="{it.url}">{it.title}</a> '
                f'<i>({it.source})</i>'
                f'<br><span style="color:#444">{art_sum}</span></li>'
            )
        parts.append("</ul>")

    parts.append("<p style='color:#666;font-size:12px'>Automated via GitHub Actions.</p>")
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
    msg["Reply-To"] = from_email

    # Add text/plain first (deliverability), then HTML
    text_body = html_to_text(html_body)
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    context = ssl.create_default_context()
    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.starttls(context=context)
        server.login(username, password)
        server.sendmail(from_email, to_emails, msg.as_string())

    print(f"EMAIL_SENT_OK to {len(to_emails)} recipients")


def main():
    if not should_send_now():
        print("Not scheduled send time (Tue/Fri @ 9am ET). Set FORCE_SEND=1 to test.")
        return

    days_back = effective_days_back()

    items = collect_all(days=days_back)
    before = len(items)

    items = [it for it in items if is_strict_us_rvpark(it)]
    after_filter = len(items)

    items = dedupe_cross_source(items)
    after_dedupe = len(items)

    print(f"Collected {before} items; {after_filter} passed strict filter; {after_dedupe} after cross-source dedupe.")

    # Assign ONE best category per item
    buckets: Dict[str, List[Item]] = {}
    for it in items:
        cat = best_category(it)
        buckets.setdefault(cat, []).append(it)

    # If Other is empty, don’t show it (but it will still be last if present)
    subject = f"Athena RV Park Digest (US-only, strict) — {datetime.now(tz=ET).strftime('%b %d, %Y')}"
    html = build_email_html(buckets, days_back=days_back)
    send_email(subject, html)

    print(f"Sent digest with {after_dedupe} items across {len(buckets)} categories.")


if __name__ == "__main__":
    main()
