# sources.py
import urllib.parse

# Keep your direct sources here (RSS when possible, HTML scrape when needed).
# NOTE: Some sites may block scraping or change markup; Google News RSS will still cover most items.
SOURCES = [
    # RVBusiness (HTML scrape)
    {"name": "RVBusiness - Today's News", "type": "html_rvbusiness", "url": "https://rvbusiness.com/todays-news/"},
    {"name": "RVBusiness - Today's Industry News", "type": "html_rvbusiness", "url": "https://rvbusiness.com/category/todays-industry-news/"},

    # Woodall’s (RSS)
    {"name": "Woodall's Campground Magazine", "type": "rss", "url": "https://woodallscm.com/feed/"},

    # Modern Campground (RSS)
    {"name": "Modern Campground", "type": "rss", "url": "https://moderncampground.com/feed/"},

    # KOA press releases (HTML scrape)
    {"name": "KOA Press Releases", "type": "html_simple_dates", "url": "https://www.koapressroom.com/press-releases/"},

    # Public company IR news pages (HTML scrape)
    {"name": "Sun Communities News Releases", "type": "html_simple_dates", "url": "https://www.suninc.com/news-releases"},
    {"name": "ELS IR News", "type": "html_simple_dates", "url": "https://equitylifestyle.gcs-web.com/news"},
]

# Google News RSS searches for "hard-to-source" categories.
# We keep the edition as US English via hl/gl/ceid in the URL builder.
# Tightened queries + explicit RV-park/campground terms + exclusions for vehicle/travel noise.
GOOGLE_NEWS_QUERIES = [
    {
        "name": "US RV Parks - Acquisitions / For Sale",
        "q": (
            '("rv park" OR "rv resort" OR campground OR "rv campground" OR campgrounds) '
            '(acquisition OR acquired OR "for sale" OR listing OR broker OR transaction OR portfolio OR "sale-leaseback") '
            '(United States OR U.S. OR USA OR county OR city OR state) '
            '-motorhome -"travel trailer" -"fifth wheel" -airstream -campervan -vanlife -pickup -tow -dealership '
            'when:7d'
        ),
    },
    {
        "name": "US RV Parks - Insurance / Risk",
        "q": (
            '("rv park" OR "rv resort" OR campground OR "rv campground" OR campgrounds) '
            '(insurance OR insurer OR premium OR underwriting OR liability OR wildfire OR flood OR hurricane OR claims) '
            '(United States OR U.S. OR USA OR state) '
            '-motorhome -"travel trailer" -"fifth wheel" -airstream -campervan -vanlife -pickup -tow '
            'when:7d'
        ),
    },
    {
        "name": "US RV Parks - Legal / Lawsuits / Zoning",
        "q": (
            '("rv park" OR "rv resort" OR campground OR "rv campground" OR campgrounds) '
            '(zoning OR ordinance OR lawsuit OR litigation OR permit OR "planning commission" OR "code enforcement") '
            '(United States OR U.S. OR USA OR state OR county OR city) '
            '-motorhome -"travel trailer" -"fifth wheel" -airstream -campervan -vanlife '
            'when:7d'
        ),
    },
    {
        "name": "US RV Parks - Financing / Rates / Debt",
        "q": (
            '("rv park" OR "rv resort" OR campground OR "rv campground" OR campgrounds) '
            '(financing OR refinance OR refinancing OR loan OR lender OR debt OR cap rate OR "interest rate") '
            '(United States OR U.S. OR USA) '
            '-motorhome -"travel trailer" -"fifth wheel" -airstream -campervan -vanlife '
            'when:7d'
        ),
    },
    {
        "name": "Sun / ELS - Outdoor Hospitality Mentions",
        "q": (
            '("Sun Communities" OR "Sun Outdoors" OR SUI OR "Equity LifeStyle" OR ELS) '
            '("rv resort" OR "rv park" OR campground OR "outdoor hospitality") '
            '(earnings OR guidance OR "press release" OR acquisition OR results OR "conference call") '
            'when:14d'
        ),
    },
    {
        "name": "Campendium / The Dyrt - RV Park Related",
        "q": (
            '(Campendium OR "The Dyrt") '
            '("rv park" OR "rv resort" OR campground OR "rv campground" OR campgrounds) '
            '(United States OR U.S. OR USA) '
            'when:14d'
        ),
    },
]

def google_news_rss_url(query: str) -> str:
    # US edition bias
    base = "https://news.google.com/rss/search?q="
    return base + urllib.parse.quote(query) + "&hl=en-US&gl=US&ceid=US:en"
