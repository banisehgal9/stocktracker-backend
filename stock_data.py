"""
Stock data fetching logic.
Ported from Smarth's desktop stock_tracker.py into standalone functions
that return plain dictionaries (no UI code).
"""

import json
import time
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime
import yfinance as yf


# ══════════════════════════════════════════════════════════════════════════════
# Currency conversion
# ══════════════════════════════════════════════════════════════════════════════

CURRENCIES = {
    "USD": {"symbol": "$",  "flag": "\U0001f1fa\U0001f1f8", "pair": None},
    "CAD": {"symbol": "C$", "flag": "\U0001f1e8\U0001f1e6", "pair": "CAD=X"},
    "EUR": {"symbol": "\u20ac",  "flag": "\U0001f1ea\U0001f1fa", "pair": "EUR=X"},
    "GBP": {"symbol": "\u00a3",  "flag": "\U0001f1ec\U0001f1e7", "pair": "GBP=X"},
    "AUD": {"symbol": "A$", "flag": "\U0001f1e6\U0001f1fa", "pair": "AUD=X"},
    "INR": {"symbol": "\u20b9",  "flag": "\U0001f1ee\U0001f1f3", "pair": "INR=X"},
    "JPY": {"symbol": "\u00a5",  "flag": "\U0001f1ef\U0001f1f5", "pair": "JPY=X"},
    "CHF": {"symbol": "Fr", "flag": "\U0001f1e8\U0001f1ed", "pair": "CHF=X"},
}

_fx_cache: dict = {"USD": 1.0}


def fetch_fx_rates():
    """Refresh all FX rates from Yahoo Finance."""
    for code, info in CURRENCIES.items():
        if not info["pair"]:
            _fx_cache[code] = 1.0
            continue
        try:
            rate = yf.Ticker(info["pair"]).fast_info.last_price
            if rate and rate > 0:
                _fx_cache[code] = float(rate)
        except Exception:
            pass
        time.sleep(0.5)  # small delay to avoid rate limiting


def usd_to(usd_amount: float, currency: str) -> float:
    """Convert a USD amount to the target currency."""
    if currency == "USD":
        return usd_amount
    rate = _fx_cache.get(currency, 1.0)
    return usd_amount * rate


def get_fx_cache() -> dict:
    """Return the current FX rate cache (USD base)."""
    return dict(_fx_cache)


def get_currencies() -> list:
    """Return list of supported currencies for the frontend dropdown."""
    return [
        {"code": code, "symbol": info["symbol"], "flag": info["flag"]}
        for code, info in CURRENCIES.items()
    ]


# ══════════════════════════════════════════════════════════════════════════════
# Ticker search — multiple fallback endpoints
# ══════════════════════════════════════════════════════════════════════════════

def _yf_search_v1(query: str) -> list:
    q = urllib.parse.quote(query)
    url = (f"https://query2.finance.yahoo.com/v1/finance/search"
           f"?q={q}&quotesCount=10&newsCount=0&enableFuzzyQuery=true"
           f"&quotesQueryId=tss_match_phrase_query")
    req = urllib.request.Request(url, headers={
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Safari/537.36"),
        "Accept": "application/json",
        "Referer": "https://finance.yahoo.com/",
    })
    with urllib.request.urlopen(req, timeout=6) as r:
        return json.loads(r.read()).get("quotes", [])


def _yf_search_v2(query: str) -> list:
    q = urllib.parse.quote(query)
    url = (f"https://query1.finance.yahoo.com/v2/finance/autocomplete"
           f"?query={q}&lang=en")
    req = urllib.request.Request(url, headers={
        "User-Agent": "python-requests/2.28.0",
    })
    with urllib.request.urlopen(req, timeout=6) as r:
        data = json.loads(r.read())
    return data.get("ResultSet", {}).get("Result", [])


def _parse_v1(items: list) -> list:
    out = []
    for i in items:
        sym = i.get("symbol", "")
        name = i.get("longname") or i.get("shortname") or ""
        typ = i.get("quoteType", "EQUITY")
        exch = i.get("exchDisp", "")
        if sym:
            out.append({"symbol": sym, "name": name[:50],
                        "type": typ, "exchange": exch})
    return out


def _parse_v2(items: list) -> list:
    out = []
    for i in items:
        sym = i.get("symbol", "")
        name = i.get("name") or i.get("longname") or ""
        exch = i.get("exchDisp") or i.get("exchange") or ""
        typ = i.get("typeDisp") or "EQUITY"
        if sym:
            out.append({"symbol": sym, "name": name[:50],
                        "type": typ, "exchange": exch})
    return out


def search_tickers(query: str) -> list:
    """Search for tickers. Tries Yahoo v1, v2, then yfinance built-in."""
    if not query:
        return []

    try:
        raw = _yf_search_v1(query)
        if raw:
            return _parse_v1(raw)
    except Exception:
        pass

    try:
        raw = _yf_search_v2(query)
        if raw:
            return _parse_v2(raw)
    except Exception:
        pass

    try:
        results = yf.Search(query, max_results=8)
        quotes = getattr(results, "quotes", []) or []
        if quotes:
            return _parse_v1(quotes)
    except Exception:
        pass

    return []


# ══════════════════════════════════════════════════════════════════════════════
# News — yfinance + Google News RSS fallback
# ══════════════════════════════════════════════════════════════════════════════

def _fetch_yf_news(symbol: str) -> list:
    items = []
    seen = set()

    # Method 1: ticker.news (older yfinance)
    try:
        raw = yf.Ticker(symbol).news or []
        for item in raw[:6]:
            if "content" in item:
                item = item["content"]
            title = item.get("title", "")
            url = (item.get("canonicalUrl", {}).get("url", "")
                   or item.get("link", "")
                   or item.get("clickThroughUrl", {}).get("url", ""))
            src = (item.get("provider", {}).get("displayName", "")
                   or item.get("publisher", ""))
            ts = item.get("pubDate", "") or item.get("providerPublishTime", 0)
            if isinstance(ts, str):
                try:
                    ts = int(datetime.fromisoformat(
                        ts.replace("Z", "+00:00")).timestamp())
                except Exception:
                    ts = 0
            if title and title not in seen:
                seen.add(title)
                items.append({"title": title, "url": url,
                              "source": src, "time": ts})
    except Exception:
        pass

    # Method 2: yf.Search news (newer yfinance)
    if len(items) < 2:
        try:
            s = yf.Search(symbol, news_count=6, max_results=1)
            raw = getattr(s, "news", []) or []
            for item in raw:
                if "content" in item:
                    item = item["content"]
                title = item.get("title", "")
                url = (item.get("canonicalUrl", {}).get("url", "")
                       or item.get("link", ""))
                src = (item.get("provider", {}).get("displayName", "")
                       or item.get("publisher", ""))
                ts = item.get("pubDate", 0) or item.get("providerPublishTime", 0)
                if isinstance(ts, str):
                    try:
                        ts = int(datetime.fromisoformat(
                            ts.replace("Z", "+00:00")).timestamp())
                    except Exception:
                        ts = 0
                if title and title not in seen:
                    seen.add(title)
                    items.append({"title": title, "url": url,
                                  "source": src, "time": ts})
        except Exception:
            pass

    return items


def _fetch_google_rss(symbol: str) -> list:
    items = []
    try:
        q = urllib.parse.quote(f"{symbol} stock")
        url = f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 StockTracker/1.0"
        })
        with urllib.request.urlopen(req, timeout=7) as r:
            root = ET.fromstring(r.read())
        for entry in root.findall(".//item")[:6]:
            title = entry.findtext("title") or ""
            link = entry.findtext("link") or ""
            src = entry.find("source")
            src_t = src.text if src is not None else "Google News"
            pub = entry.findtext("pubDate") or ""
            if title:
                items.append({"title": title, "url": link,
                              "source": src_t, "time": 0})
    except Exception:
        pass
    return items


def fetch_latest_news(symbol: str) -> list:
    news = _fetch_yf_news(symbol)
    seen = {n["title"] for n in news}

    if len(news) < 5:
        for item in _fetch_google_rss(symbol):
            if item["title"] not in seen:
                seen.add(item["title"])
                news.append(item)
            if len(news) >= 5:
                break

    return news[:5]


# ══════════════════════════════════════════════════════════════════════════════
# Reddit buzz
# ══════════════════════════════════════════════════════════════════════════════

def fetch_reddit_buzz(symbol: str) -> str:
    buzz = []
    sym = symbol.upper()
    for sub in ["stocks", "investing", "wallstreetbets"]:
        url = (f"https://www.reddit.com/r/{sub}/search.json"
               f"?q={sym}&sort=new&limit=5&restrict_sr=1")
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "StockTracker/1.0"
            })
            with urllib.request.urlopen(req, timeout=5) as r:
                data = json.loads(r.read())
            for p in data.get("data", {}).get("children", []):
                pd = p.get("data", {})
                title = pd.get("title", "")
                ups = pd.get("ups", 0)
                if title and ups > 5:
                    buzz.append({"upvotes": ups, "title": title,
                                 "subreddit": sub})
        except Exception:
            pass

    if not buzz:
        return f"No notable Reddit discussion found for ${sym} recently."

    buzz.sort(key=lambda x: x["upvotes"], reverse=True)
    parts = []
    for b in buzz[:2]:
        short = b["title"][:90] + "\u2026" if len(b["title"]) > 90 else b["title"]
        parts.append(f'r/{b["subreddit"]}: "{short}" ({b["upvotes"]} upvotes)')
    return "  \u2022  ".join(parts)


# ══════════════════════════════════════════════════════════════════════════════
# Full stock data fetch
# ══════════════════════════════════════════════════════════════════════════════

def fetch_stock_data(symbol: str) -> dict:
    """Fetch all data for a single stock. Returns a flat dictionary."""
    result = {
        "symbol": symbol.upper(),
        "price": None,
        "change_day": None,
        "change_day_pct": None,
        "change_week": None,
        "change_week_pct": None,
        "news": [],
        "consensus": "N/A",
        "reddit": "",
        "history_30d": [],
        "error": None,
        "last_updated": datetime.now().isoformat(),
    }

    try:
        ticker = yf.Ticker(symbol)

        # Price + day change
        try:
            fi = ticker.fast_info
            price = float(fi.last_price)
            prev = float(fi.previous_close)
            result["price"] = round(price, 2)
            result["change_day"] = round(price - prev, 2)
            result["change_day_pct"] = round((price - prev) / prev * 100, 2)
        except Exception:
            hist = ticker.history(period="2d")
            if len(hist) >= 2:
                p = float(hist["Close"].iloc[-1])
                v = float(hist["Close"].iloc[-2])
                result["price"] = round(p, 2)
                result["change_day"] = round(p - v, 2)
                result["change_day_pct"] = round((p - v) / v * 100, 2)

        # Week change
        try:
            hw = ticker.history(period="8d")
            if len(hw) >= 5:
                w0 = float(hw["Close"].iloc[-6])
                w1 = float(hw["Close"].iloc[-1])
                result["change_week"] = round(w1 - w0, 2)
                result["change_week_pct"] = round((w1 - w0) / w0 * 100, 2)
        except Exception:
            pass

        # 30-day price history (for sparkline charts on Discover page)
        try:
            h30 = ticker.history(period="1mo")
            result["history_30d"] = [
                {"date": d.strftime("%Y-%m-%d"), "close": round(float(c), 2)}
                for d, c in zip(h30.index, h30["Close"])
            ]
        except Exception:
            pass

        # Analyst consensus
        try:
            summ = ticker.recommendations_summary
            if summ is not None and not summ.empty:
                row = summ.iloc[0]
                scores = {"strongBuy": 5, "buy": 4, "hold": 3,
                          "underperform": 2, "sell": 1}
                labels = {"strongBuy": "Strong Buy", "buy": "Buy",
                          "hold": "Hold", "underperform": "Underperform",
                          "sell": "Sell"}
                best = max(
                    [(k, int(v)) for k, v in row.items() if k in scores],
                    key=lambda x: x[1] * scores[x[0]], default=(None, 0)
                )
                if best[0]:
                    result["consensus"] = labels[best[0]]
            else:
                recs = ticker.recommendations
                if recs is not None and not recs.empty:
                    latest = recs.iloc[-1]
                    for col in ["To Grade", "toGrade", "Recommendation"]:
                        if col in latest.index and str(latest[col]).strip():
                            result["consensus"] = str(latest[col]).strip()
                            break
        except Exception:
            pass

        # News and Reddit
        result["news"] = fetch_latest_news(symbol)
        result["reddit"] = fetch_reddit_buzz(symbol)

    except Exception as e:
        result["error"] = str(e)

    return result


# ══════════════════════════════════════════════════════════════════════════════
# Sector info (for Discover recommendations)
# ══════════════════════════════════════════════════════════════════════════════

def get_sector(symbol: str) -> str:
    """Get the sector for a ticker. Returns 'Unknown' if not found."""
    try:
        info = yf.Ticker(symbol).info
        return info.get("sector", "Unknown")
    except Exception:
        return "Unknown"
