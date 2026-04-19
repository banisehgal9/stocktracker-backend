"""
StockTracker API — FastAPI backend.
Serves stock data, manages watchlists via Supabase, handles auth.
"""

import os
import time
import logging
from datetime import datetime
from typing import Optional

import yfinance as yf
from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv
from supabase import create_client, Client

from stock_data import (
    fetch_stock_data,
    search_tickers,
    get_currencies,
    get_fx_cache,
    fetch_fx_rates,
    usd_to,
    CURRENCIES,
)

# ── Setup ─────────────────────────────────────────────────────────────────────

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")

if not SUPABASE_URL or not SUPABASE_ANON_KEY:
    raise RuntimeError(
        "Missing SUPABASE_URL or SUPABASE_ANON_KEY. "
        "Copy .env.example to .env and paste your keys."
    )

supabase: Client = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)
supabase_admin: Client = create_client(SUPABASE_URL, os.getenv("SUPABASE_SERVICE_KEY", ""))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("stocktracker")

app = FastAPI(title="StockTracker API", version="1.0.0")

# Allow the frontend to call this API from a different port during development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten this to your frontend URL in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Auth helper ───────────────────────────────────────────────────────────────

def get_user_id(authorization: Optional[str] = Header(None)) -> str:
    """
    Extract the user ID from the Supabase JWT token.
    The frontend sends: Authorization: Bearer <token>
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")

    token = authorization.split(" ", 1)[1]

    try:
        response = supabase.auth.get_user(token)
        user_id = response.user.id
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token")
        return user_id
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token")


# ── Request/Response models ───────────────────────────────────────────────────

class WatchlistUpdate(BaseModel):
    symbols: list[str]
    currency: str = "CAD"


# ── Routes: Public ────────────────────────────────────────────────────────────

@app.get("/")
def health_check():
    return {"status": "ok", "app": "StockTracker API", "time": datetime.now().isoformat()}


@app.get("/api/currencies")
def list_currencies():
    """Return all supported currencies for the dropdown."""
    return get_currencies()


@app.get("/api/fx-rates")
def get_exchange_rates():
    """Return current exchange rates with USD as the base."""
    return get_fx_cache()


@app.get("/api/search")
def ticker_search(q: str = ""):
    """Search for tickers by name or symbol."""
    if not q or len(q) < 1:
        return []
    results = search_tickers(q)
    return results


@app.get("/api/stock/{symbol}")
def get_stock(symbol: str):
    """
    Get full data for a single stock.
    This is the main endpoint the frontend calls for each card.
    """
    symbol = symbol.upper().strip()
    if not symbol:
        raise HTTPException(status_code=400, detail="Symbol required")

    data = fetch_stock_data(symbol)

    if data.get("error"):
        logger.warning(f"Error fetching {symbol}: {data['error']}")

    return data


# ── Routes: Authenticated (require login) ─────────────────────────────────────

@app.get("/api/watchlist")
def get_watchlist(user_id: str = Depends(get_user_id)):
    """Get the logged-in user's watchlist. Creates one if it doesn't exist."""
    try:
        result = (
            supabase_admin.table("watchlists")
            .select("*")
            .eq("user_id", user_id)
            .execute()
        )
        if result.data and len(result.data) > 0:
            return result.data[0]
        
        # No watchlist exists — create one with defaults
        new = (
            supabase_admin.table("watchlists")
            .insert({"user_id": user_id, "symbols": ["AAPL", "TSLA", "NVDA", "MSFT"], "currency": "CAD"})
            .execute()
        )
        return new.data[0]
    except Exception as e:
        logger.error(f"Error fetching watchlist for {user_id}: {e}")
        raise HTTPException(status_code=500, detail="Could not fetch watchlist")


@app.put("/api/watchlist")
def update_watchlist(body: WatchlistUpdate, user_id: str = Depends(get_user_id)):
    """Update the logged-in user's watchlist (symbols and/or currency)."""
    # Validate currency
    if body.currency not in CURRENCIES:
        raise HTTPException(status_code=400, detail=f"Unsupported currency: {body.currency}")

    # Clean symbols
    symbols = [s.upper().strip() for s in body.symbols if s.strip()]

    try:
        result = (
            supabase_admin.table("watchlists")
            .update({
                "symbols": symbols,
                "currency": body.currency,
                "updated_at": datetime.now().isoformat(),
            })
            .eq("user_id", user_id)
            .execute()
        )
        return result.data
    except Exception as e:
        logger.error(f"Error updating watchlist for {user_id}: {e}")
        raise HTTPException(status_code=500, detail="Could not update watchlist")


@app.post("/api/watchlist/add/{symbol}")
def add_to_watchlist(symbol: str, user_id: str = Depends(get_user_id)):
    """Add a single stock to the user's watchlist."""
    symbol = symbol.upper().strip()

    try:
        # Get current watchlist
        current = (
            supabase_admin.table("watchlists")
            .select("symbols")
            .eq("user_id", user_id)
            .single()
            .execute()
        )
        symbols = current.data.get("symbols", [])

        if symbol in symbols:
            return {"message": f"{symbol} already in watchlist", "symbols": symbols}

        symbols.append(symbol)

        supabase_admin.table("watchlists").update({
            "symbols": symbols,
            "updated_at": datetime.now().isoformat(),
        }).eq("user_id", user_id).execute()

        return {"message": f"{symbol} added", "symbols": symbols}

    except Exception as e:
        logger.error(f"Error adding {symbol} for {user_id}: {e}")
        raise HTTPException(status_code=500, detail="Could not add stock")


@app.delete("/api/watchlist/remove/{symbol}")
def remove_from_watchlist(symbol: str, user_id: str = Depends(get_user_id)):
    """Remove a single stock from the user's watchlist."""
    symbol = symbol.upper().strip()

    try:
        current = (
            supabase_admin.table("watchlists")
            .select("symbols")
            .eq("user_id", user_id)
            .single()
            .execute()
        )
        symbols = current.data.get("symbols", [])

        if symbol not in symbols:
            return {"message": f"{symbol} not in watchlist", "symbols": symbols}

        symbols.remove(symbol)

        supabase_admin.table("watchlists").update({
            "symbols": symbols,
            "updated_at": datetime.now().isoformat(),
        }).eq("user_id", user_id).execute()

        return {"message": f"{symbol} removed", "symbols": symbols}

    except Exception as e:
        logger.error(f"Error removing {symbol} for {user_id}: {e}")
        raise HTTPException(status_code=500, detail="Could not remove stock")


# ── Routes: Discover feed ─────────────────────────────────────────────────────

# Hardcoded sector→tickers map (avoids calling get_sector() at runtime)
SECTOR_TICKERS = {
    "Technology": ["AAPL", "MSFT", "GOOGL", "META", "NVDA", "AMD", "CRM",
                   "INTC", "ADBE", "ORCL", "PLTR", "CRWD", "SNOW", "NET"],
    "Healthcare": ["JNJ", "UNH", "PFE", "ABBV", "MRK", "LLY", "TMO",
                   "ABT", "BMY", "AMGN"],
    "Financial Services": ["JPM", "BAC", "GS", "MS", "V", "MA", "BLK",
                           "C", "WFC", "AXP"],
    "Consumer Cyclical": ["AMZN", "TSLA", "HD", "NKE", "MCD", "SBUX",
                          "TGT", "LOW", "BKNG", "CMG"],
    "Energy": ["XOM", "CVX", "COP", "SLB", "EOG", "MPC", "PSX",
               "OXY", "VLO", "DVN"],
    "Communication Services": ["GOOGL", "META", "DIS", "NFLX", "CMCSA",
                                "T", "VZ", "TMUS", "SNAP", "PINS"],
}

# Reverse lookup: ticker → sector (built once at import time)
TICKER_SECTOR: dict[str, str] = {}
for _sector, _tickers in SECTOR_TICKERS.items():
    for _t in _tickers:
        TICKER_SECTOR[_t] = _sector

FALLBACK_TICKERS = ["AMD", "PLTR", "SMCI", "MARA", "CRWD", "COIN",
                    "SOFI", "RIVN", "LCID", "RKLB"]

REASON_TEMPLATES = {
    "sector": "Because you hold {held} \u2014 same sector",
    "trending": "Trending on Reddit right now",
    "momentum": "Up {pct}% today",
}

_discover_cache: dict[str, dict] = {}  # user_id → cached response
_discover_cache_time: dict[str, float] = {}  # user_id → epoch seconds
DISCOVER_CACHE_TTL = 300  # 5 minutes


def _fetch_discover_item(ticker: str) -> dict | None:
    """Fetch only price + day change via fast_info. Returns None on error."""
    try:
        fi = yf.Ticker(ticker).fast_info
        price = float(fi.last_price)
        prev = float(fi.previous_close)
        change = round(price - prev, 2)
        change_pct = round((price - prev) / prev * 100, 2)
        return {
            "symbol": ticker,
            "price": round(price, 2),
            "change_day": change,
            "change_day_pct": change_pct,
        }
    except Exception:
        return None


@app.get("/api/discover")
def get_discover(user_id: str = Depends(get_user_id)):
    """
    Return personalized stock recommendations based on user's watchlist sectors.
    Results are cached per user for 5 minutes.
    """
    now = time.time()
    if (user_id in _discover_cache
            and now - _discover_cache_time.get(user_id, 0) < DISCOVER_CACHE_TTL):
        return _discover_cache[user_id]

    try:
        current = (
            supabase_admin.table("watchlists")
            .select("symbols")
            .eq("user_id", user_id)
            .single()
            .execute()
        )
        held = set(current.data.get("symbols", []))
    except Exception:
        held = set()

    # Determine user sectors using hardcoded map (no network calls)
    user_sectors: dict[str, list[str]] = {}
    for sym in held:
        sector = TICKER_SECTOR.get(sym)
        if sector:
            user_sectors.setdefault(sector, []).append(sym)

    recommendations = []
    seen = set(held)

    # Sector-matched candidates
    for sector, held_in_sector in user_sectors.items():
        for ticker in SECTOR_TICKERS.get(sector, []):
            if ticker in seen or len(recommendations) >= 10:
                continue
            seen.add(ticker)
            item = _fetch_discover_item(ticker)
            if item is None:
                continue
            reason_type = "sector"
            reason = REASON_TEMPLATES["sector"].format(held=held_in_sector[0])
            if item["change_day_pct"] > 3:
                reason_type = "momentum"
                reason = REASON_TEMPLATES["momentum"].format(
                    pct=round(item["change_day_pct"], 1))
            recommendations.append({**item, "reason": reason, "reason_type": reason_type})
        if len(recommendations) >= 10:
            break

    # Fill remaining slots with fallback trending tickers
    for ticker in FALLBACK_TICKERS:
        if len(recommendations) >= 10:
            break
        if ticker in seen:
            continue
        seen.add(ticker)
        item = _fetch_discover_item(ticker)
        if item is None:
            continue
        recommendations.append({
            **item,
            "reason": REASON_TEMPLATES["trending"],
            "reason_type": "trending",
        })

    priority = {"momentum": 0, "sector": 1, "trending": 2}
    recommendations.sort(key=lambda r: priority.get(r["reason_type"], 99))

    total = len(held) or 1
    sector_pcts = {
        sector: round(len(syms) / total * 100)
        for sector, syms in user_sectors.items()
    }

    result = {"recommendations": recommendations, "portfolio_breakdown": sector_pcts}
    _discover_cache[user_id] = result
    _discover_cache_time[user_id] = now
    return result


# ══════════════════════════════════════════════════════════════════════════════
# Startup
# ══════════════════════════════════════════════════════════════════════════════

@app.on_event("startup")
async def startup():
    """Pre-fetch FX rates on startup."""
    logger.info("Fetching initial FX rates...")
    fetch_fx_rates()
    logger.info("StockTracker API ready.")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
