"""Free-cash-flow screen for the options stream (the wheel).

Selling a cash-secured put means you may end up OWNING the stock, so the stream only
sells puts on companies you'd be comfortable owning: ones that generate real cash.

"Healthy free cash flow" (all thresholds in config/options.yaml -> screen:)
  * FCF = operating cash flow - capital spending, last 12 months, from the cash-flow
    STATEMENTS (Yahoo's summary "freeCashflow" field is levered FCF and is often wrong);
  * positive in every annual report on record (3-4 years);
  * FCF margin >= 10% of revenue (and <= 60%: higher is almost always a data error);
  * FCF yield >= 4% of market value (not priced for perfection);
  * net debt <= 3x annual FCF (the cash isn't all owed to lenders);
  * financials and REITs excluded (FCF isn't a meaningful measure for them);
  * NOT in a downtrend: price >= 95% of its 200-day average and not down 20%+ in six
    months — selling puts on a falling stock is the classic beginner trap;
  * affordable: one contract's collateral (strike x 100) must fit one wheel slot.

Runs over the S&P 500 (list from Wikipedia, cached), weekly, in a background thread.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from . import db

_UA = {"User-Agent": "Mozilla/5.0 (hermes-trading research)"}
_universe_cache: tuple[float, dict[str, str]] | None = None


def sp500() -> dict[str, str]:
    """{ticker: GICS sector} for the S&P 500 (cached for a day)."""
    global _universe_cache
    if _universe_cache and time.time() - _universe_cache[0] < 86400:
        return _universe_cache[1]
    import pandas as pd
    import requests

    html = requests.get("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
                        headers=_UA, timeout=30).text
    t = pd.read_html(io.StringIO(html))[0]
    out = {s.replace(".", "-"): sec for s, sec in zip(t["Symbol"], t["GICS Sector"])}
    _universe_cache = (time.time(), out)
    return out


def _ttm(q, row: str) -> float | None:
    if q is None or row not in q.index:
        return None
    v = q.loc[row].dropna()
    return float(v.iloc[:4].sum()) if len(v) >= 4 else None


def _fundamentals(t: str) -> tuple[str, dict[str, Any]]:
    import yfinance as yf
    try:
        tk = yf.Ticker(t)
        i = tk.info
        ac = tk.cashflow
        fcf = _ttm(tk.quarterly_cashflow, "Free Cash Flow")
        ann = ([float(x) for x in ac.loc["Free Cash Flow"].dropna().tolist()[:4]]
               if ac is not None and "Free Cash Flow" in ac.index else [])
        if fcf is None and ann:
            fcf = ann[0]
        return t, {"name": i.get("shortName"), "industry": i.get("industry"),
                   "mcap": i.get("marketCap"), "rev": i.get("totalRevenue"),
                   "debt": i.get("totalDebt") or 0.0, "cash": i.get("totalCash") or 0.0,
                   "fcf": fcf, "fcf_annual": ann, "dividend_yield": i.get("dividendYield"),
                   "beta": i.get("beta")}
    except Exception as exc:  # noqa: BLE001
        return t, {"error": str(exc)[:80]}


def explain(c: dict[str, Any]) -> str:
    """One plain-English line on why a company passed."""
    nd = c["net_debt_to_fcf"]
    debt = "more cash than debt" if nd < 0 else f"net debt {nd:.1f}x its annual FCF"
    div = c.get("dividend_yield")
    div_s = f", pays a {div:.1f}% dividend" if div else ""
    return (f"{c['name']} turns {c['fcf_margin']:.0%} of revenue into free cash "
            f"(${c['fcf'] / 1e9:.1f}B over 12 months, positive {c['fcf_years']} years running) — "
            f"a {c['fcf_yield']:.1%} FCF yield on its market value; {debt}{div_s}; "
            f"trading {c['vs_sma200']:+.0%} vs its 200-day average ({c['return_6m']:+.0%} in 6 months).")


def run(cfg: dict[str, Any]) -> dict[str, Any]:
    """Run the screen. Blocking (~30s of network) — call from a thread."""
    import pandas as pd
    import yfinance as yf

    sc = cfg["screen"]
    lo, hi = sc["price_range"]
    uni = sp500()
    tick = [t for t, sec in uni.items() if sec not in set(sc["exclude_sectors"])]
    hist = yf.download(tick, period="1y", progress=False, auto_adjust=True, threads=True)["Close"].ffill()
    last, sma = hist.iloc[-1], hist.rolling(200, min_periods=150).mean().iloc[-1]
    r6 = hist.iloc[-1] / hist.iloc[-126] - 1
    afford = [t for t in tick if pd.notna(last.get(t)) and lo <= float(last[t]) <= hi]
    with ThreadPoolExecutor(6) as ex:
        funds = dict(ex.map(_fundamentals, afford))

    passed, rejected = [], {"downtrend": [], "fcf": 0, "data": 0}
    for t in afford:
        f = funds.get(t) or {}
        if "error" in f or not f.get("fcf") or not f.get("rev") or not f.get("mcap"):
            rejected["data"] += 1
            continue
        fcf, rev, mcap = float(f["fcf"]), float(f["rev"]), float(f["mcap"])
        ann = f["fcf_annual"]
        c = {
            "symbol": t, "name": f["name"], "sector": uni[t], "industry": f["industry"],
            "price": float(last[t]), "fcf": fcf, "fcf_margin": fcf / rev, "fcf_yield": fcf / mcap,
            "net_debt_to_fcf": (float(f["debt"]) - float(f["cash"])) / fcf if fcf > 0 else 99.0,
            "fcf_years": len(ann), "vs_sma200": float(last[t] / sma[t] - 1) if pd.notna(sma.get(t)) else 0.0,
            "return_6m": float(r6[t]) if pd.notna(r6.get(t)) else 0.0,
            # yfinance reports dividendYield already in PERCENT (6.5 = 6.5%)
            "dividend_yield": float(f["dividend_yield"]) if f.get("dividend_yield") else None,
            "beta": f.get("beta"),
        }
        healthy = (fcf > 0 and len(ann) >= sc["min_positive_fcf_years"] and all(x > 0 for x in ann)
                   and sc["min_fcf_margin"] <= c["fcf_margin"] <= sc["max_fcf_margin"]
                   and c["fcf_yield"] >= sc["min_fcf_yield"]
                   and c["net_debt_to_fcf"] <= sc["max_net_debt_to_fcf"])
        if not healthy:
            rejected["fcf"] += 1
            continue
        if not (c["vs_sma200"] >= sc["min_price_vs_sma200"] - 1 and c["return_6m"] > sc["min_return_6m"]):
            rejected["downtrend"].append(t)
            continue
        passed.append(c)

    if passed:   # rank: FCF yield (value) + FCF margin (quality) + balance sheet, as percentiles
        df = pd.DataFrame(passed).set_index("symbol")
        df["score"] = (df.fcf_yield.rank(pct=True) + df.fcf_margin.rank(pct=True)
                       + (-df.net_debt_to_fcf).rank(pct=True) * 0.5)
        passed = [dict(r, symbol=s) for s, r in df.sort_values("score", ascending=False).iterrows()]
    for c in passed:
        c["why"] = explain(c)
    return {
        "date": dt.date.today().isoformat(), "ts": time.time(),
        "funnel": {"sp500": len(uni), "ex_fin_reit": len(tick), "affordable": len(afford),
                   "healthy_fcf": len(passed) + len(rejected["downtrend"]), "passed": len(passed)},
        "excluded_downtrend": rejected["downtrend"],
        "candidates": passed,
    }


def store(conn: sqlite3.Connection, result: dict[str, Any]) -> None:
    conn.execute("INSERT OR REPLACE INTO opt_screen(date, ts, json) VALUES(?,?,?)",
                 (result["date"], result["ts"], json.dumps(result, default=float)))
    conn.commit()


def latest(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute("SELECT json FROM opt_screen ORDER BY ts DESC LIMIT 1").fetchone()
    return json.loads(row["json"]) if row else None


def due(conn: sqlite3.Connection, cfg: dict[str, Any]) -> bool:
    s = latest(conn)
    return s is None or time.time() - float(s["ts"]) > float(cfg["screen"]["refresh_days"]) * 86400


def refresh(cfg: dict[str, Any]) -> dict[str, Any]:
    """Run + store (own connection). Blocking — call from a thread."""
    res = run(cfg)
    conn = db.connect()
    try:
        store(conn, res)
    finally:
        conn.close()
    return res
