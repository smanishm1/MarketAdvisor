"""Best-effort news headlines + earnings dates via yfinance (free, no key).

Two uses, both bolted onto the *human* side of the loop rather than the signal:
  1. **News context** — top headlines for a name, shown on the approval card, in the
     Discord embed, and in the morning brief. Pure context; it changes no logic.
  2. **Earnings guard** — `in_earnings_blackout` lets the worker skip *opening* a
     single-stock position when its earnings fall within a blackout window (the
     sleeve's biggest risk is an overnight earnings gap through the stop).

Everything here is cached and fails SOFT:
  - news errors -> empty list (the card just shows no headlines);
  - the earnings guard fails **OPEN** — if the date can't be determined we do NOT
    block the buy (the tighter stock cap + wider stop remain the backstop).

Targets yfinance 1.4.x: news items are ``{'content': {...}}`` and earnings come
from ``Ticker.calendar['Earnings Date']`` (``get_earnings_dates`` needs lxml).
"""
from __future__ import annotations

import datetime as dt
import time
from typing import Any

NEWS_TTL = 3600.0      # headlines: refresh at most hourly
EARN_TTL = 86400.0     # earnings date: refresh at most daily
_news_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_earn_cache: dict[str, tuple[float, dt.date | None]] = {}


def _pub_date(iso: str | None) -> str | None:
    if not iso:
        return None
    try:
        return dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00")).date().isoformat()
    except Exception:  # noqa: BLE001
        return None


def _as_date(x: Any) -> dt.date | None:
    if isinstance(x, dt.datetime):
        return x.date()
    if isinstance(x, dt.date):
        return x
    try:
        import pandas as pd
        return pd.Timestamp(x).date()
    except Exception:  # noqa: BLE001
        return None


def headlines(symbol: str, n: int = 3) -> list[dict[str, Any]]:
    """Top recent headlines: [{title, publisher, url, date}]. Cached hourly, soft-fail."""
    now = time.time()
    hit = _news_cache.get(symbol)
    if hit and now - hit[0] < NEWS_TTL:
        return hit[1][:n]
    out: list[dict[str, Any]] = []
    try:
        import yfinance as yf
        for a in (yf.Ticker(symbol).news or []):
            c = a.get("content") if isinstance(a, dict) else None
            if not isinstance(c, dict):
                c = a if isinstance(a, dict) else {}
            title = c.get("title")
            if not title:
                continue
            prov = c.get("provider")
            publisher = prov.get("displayName") if isinstance(prov, dict) else (c.get("publisher") or prov)
            cu = c.get("canonicalUrl")
            url = cu.get("url") if isinstance(cu, dict) else (c.get("link") or cu)
            out.append({
                "title": str(title)[:200],
                "publisher": publisher,
                "url": url,
                "date": _pub_date(c.get("pubDate") or c.get("displayTime")),
            })
            if len(out) >= 6:
                break
    except Exception:  # noqa: BLE001 — news is best-effort context only
        out = []
    _news_cache[symbol] = (now, out)
    return out[:n]


def next_earnings_date(symbol: str) -> dt.date | None:
    """Next scheduled earnings date (or None). Cached daily, soft-fail."""
    now = time.time()
    hit = _earn_cache.get(symbol)
    if hit and now - hit[0] < EARN_TTL:
        return hit[1]
    edate: dt.date | None = None
    try:
        import yfinance as yf
        cal = yf.Ticker(symbol).calendar
        ed = cal.get("Earnings Date") if isinstance(cal, dict) else None
        raw = ed if isinstance(ed, (list, tuple)) else [ed]
        dates: list[dt.date] = []
        for x in raw:
            d = _as_date(x)
            if d is not None:
                dates.append(d)
        today = dt.date.today()
        future = sorted(d for d in dates if d >= today)
        edate = future[0] if future else (sorted(dates)[-1] if dates else None)
    except Exception:  # noqa: BLE001
        edate = None
    _earn_cache[symbol] = (now, edate)
    return edate


def in_earnings_blackout(
    symbol: str, days: int, today: dt.date | None = None
) -> tuple[bool, dt.date | None]:
    """(blocked, earnings_date). Blocked iff the next earnings is within `days` days.
    Fails OPEN — an unknown date returns (False, None)."""
    if days <= 0:
        return False, None
    ed = next_earnings_date(symbol)
    if ed is None:
        return False, None
    today = today or dt.date.today()
    return (today <= ed <= today + dt.timedelta(days=days)), ed


def prep(symbols, cfg: dict[str, Any], blackout_days: int = 0) -> dict[str, dict[str, Any]]:
    """Precompute {sym: {news, block, edate}} for buy candidates.

    Call OFF the event loop (asyncio.to_thread) — it makes blocking yfinance calls.
    Earnings are only looked up for single stocks (ETFs don't report earnings).
    """
    stocks = set(cfg.get("stocks") or [])
    out: dict[str, dict[str, Any]] = {}
    for s in symbols:
        m: dict[str, Any] = {"news": headlines(s, 3), "block": False, "edate": None}
        if s in stocks and blackout_days > 0:
            m["block"], m["edate"] = in_earnings_blackout(s, blackout_days)
        out[s] = m
    return out
