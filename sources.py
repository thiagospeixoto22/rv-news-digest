# sources.py
SOURCES = [
    # --- Industry news sources ---
    # RVBusiness pages exist (we'll scrape HTML). :contentReference[oaicite:2]{index=2}
    {"name": "RVBusiness - Today's News", "type": "html_rvbusiness", "url": "https://rvbusiness.com/todays-news/"},
    {"name": "RVBusiness - Today's Industry News", "type": "html_rvbusiness", "url": "https://rvbusiness.com/category/todays-industry-news/"},

    # Woodall's supports RSS (their article URLs include rss params). :contentReference[oaicite:3]{index=3}
    {"name": "Woodall's Campground Magazine", "type": "rss_guess", "url": "https://woodallscm.com/feed/"},

    # Modern Campground is WordPress-like and supports category feeds; /feed is commonly available.
    {"name": "Modern Campground", "type": "rss_guess", "url": "https://moderncampground.com/feed/"},

    # KOA press releases page (scrape) :contentReference[oaicite:4]{index=4}
    {"name": "KOA Press Releases", "type": "html_koa_press", "url": "https://www.koapressroom.com/press-releases/"},

    # Public company IR pages (scrape)
    # Sun Communities news releases :contentReference[oaicite:5]{index=5}
    {"name": "Sun Communities News Releases", "type": "html_sun_news", "url": "https://www.suninc.com/news-releases"},
    # ELS investor relations news :contentReference[oaicite:6]{index=6}
    {"name": "ELS IR News", "type": "html_els_news", "url": "https://equitylifestyle.gcs-web.com/news"},
]

# ---- Google News RSS searches (for “hard to source” topics) ----
# Undocumented by Google, but widely used; Feedly shows example syntax incl. when: :contentReference[oaicite:7]{index=7}
GOOGLE_NEWS_QUERIES = [
    {
        "name": "RV Parks - Acquisitions & For Sale",
        "q": '(rv park OR rv resort OR campground OR "outdoor hospitality") (acquisition OR acquired OR sale OR "for sale" OR broker OR transaction OR portfolio) when:7d'
    },
    {
        "name": "RV Parks - Insurance / Risk",
        "q": '(rv park OR campground OR rv resort) (insurance OR insurer OR premium OR wildfire OR flood OR liability OR claims) when:7d'
    },
    {
        "name": "RV Parks - Legal / Zoning / Regulation",
        "q": '(rv park OR campground OR rv resort) (zoning OR ordinance OR lawsuit OR litigation OR permit OR planning commission OR "code enforcement") when:7d'
    },
    {
        "name": "RV Parks - Financing / Rates / Debt",
        "q": '(rv park OR campground OR rv resort) (financing OR loan OR lender OR cap rate OR refinancing OR debt) when:7d'
    },
    {
        "name": "SUI / Sun Outdoors",
        "q": '(Sun Communities OR Sun Outdoors OR SUI) (earnings OR guidance OR dividend OR acquisition OR resort OR campground) when:14d'
    },
    {
        "name": "ELS",
        "q": '(Equity LifeStyle OR ELS) (earnings OR guidance OR dividend OR acquisition OR RV) when:14d'
    },
    {
        "name": "Campendium / The Dyrt / Campspot",
        "q": '(Campendium OR "The Dyrt" OR Campspot) (rv park OR campground OR outdoor hospitality) when:14d'
    },
]

def google_news_rss_url(query: str) -> str:
    # US English edition
    import urllib.parse
    return "https://news.google.com/rss/search?q=" + urllib.parse.quote(query) + "&hl=en-US&gl=US&ceid=US:en"
