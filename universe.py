"""Bear-call universe: ~300 optionable, liquid, high-IV US equities.

This module owns the full ticker list and the cheap pre-screen that reduces it
to ~25 survivors before the expensive 1y-history analyze() loop runs.

Flow:
  load_universe() → 300 tickers
  cheap_prescreen(tickers, max_keep=25) → 25 survivors
      Stage A: single yf.download(all, period='5d') — batch, fast
               reject: price ≤ 0, notional volume < $50M
               score: 52w-range IV proxy × ADV rank
               keep top 50
      Stage B: per-ticker fast_info on top 50 only
               reject: beta > 3.0, earnings within 45 days
               return top max_keep by score

Override hook (future LLM feed — no code change needed):
  Write a JSON array of tickers to bearcall_universe_override.json.
  load_universe() picks it up automatically on the next run.
  Example:
    claude -p "List 80 optionable US equities for bear-call spreads ..." \\
      > bearcall/bearcall_universe_override.json
"""

import json
import math
import os
from datetime import datetime, timedelta, timezone

import yfinance as yf

# ── Universe file paths ────────────────────────────────────────────────────────
_DIR = os.path.dirname(os.path.abspath(__file__))
UNIVERSE_FILE = os.path.join(_DIR, "bearcall_universe.json")
UNIVERSE_OVERRIDE_FILE = os.path.join(_DIR, "bearcall_universe_override.json")

# ── Pre-screen thresholds ──────────────────────────────────────────────────────
NOTIONAL_VOL_MIN = 50_000_000   # price × volume; below this = illiquid
BETA_MAX = 3.0                  # too erratic for defined-risk spread
EARNINGS_DAYS = 45              # reject if earnings within this many days
STAGE_A_KEEP = 75               # top N by volume score into Stage B
PRESCREEN_DEFAULT_KEEP = 25     # final survivors passed to analyze()

# ── Master universe list (~300 tickers) ────────────────────────────────────────
# Criteria: optionable US equity, ADV typically > 5M shares, beta > 0.7,
# historically elevated IV. Covers NDX-100, S&P high-beta, financials, energy,
# biotech, sector ETFs. Update by editing bearcall_universe.json and committing,
# or by writing bearcall_universe_override.json from a CLI tool.
BEAR_CALL_UNIVERSE = [
    # ── Tech / high-IV NDX-100 ────────────────────────────────────────────────
    "NVDA", "AMD", "TSLA", "META", "AAPL", "MSFT", "AMZN", "GOOGL", "GOOG",
    "NFLX", "ADBE", "CRM", "ABNB", "MSTR", "COIN", "HOOD", "PLTR", "CRWD",
    "SNOW", "DDOG", "ZS", "PANW", "OKTA", "FTNT", "NET", "MDB", "SHOP",
    "SQ", "PYPL", "UBER", "LYFT", "SPOT", "RBLX", "RIVN", "SOFI", "UPST",
    "AFRM", "BILL", "DOCN", "GTLB", "CFLT", "SMCI", "IONQ", "ARM", "AVGO",
    "QCOM", "INTC", "MU", "LRCX", "AMAT", "KLAC", "ON", "MRVL", "ORCL",
    "DELL", "SNAP", "PINS", "TTD", "DKNG", "PENN", "IBKR", "SCHW",
    "SOUN", "ACHR", "JOBY", "RGTI", "LUNR", "APP", "APLT",
    # ── Semiconductors / hardware ─────────────────────────────────────────────
    "TSM", "ASML", "TXN", "ADI", "MCHP", "SWKS", "QRVO", "MPWR", "ENTG",
    "ONTO", "ACLS", "WOLF", "AMBA", "CRUS", "SLAB", "DIOD",
    # ── Software / cloud ─────────────────────────────────────────────────────
    "NOW", "WDAY", "TEAM", "ZM", "DOCU", "HUBS", "DOMO", "APPF", "PCOR",
    "AZPN", "MDLA", "AI", "PATH", "BBAI", "SAMSF",
    # ── S&P mid / high-beta ───────────────────────────────────────────────────
    "MELI", "SE", "BABA", "JD", "PDD", "BIDU", "NIO", "XPEV", "LI",
    "LCID", "F", "GM", "STLA", "CELH", "DUOL", "CAVA", "LULU", "NKE",
    "DECK", "CROX", "UAA", "TPR", "RL", "PVH", "CPRI", "GPS", "ANF",
    "URBN", "FIVE", "OLLI", "BBWI", "ULTA", "ELF", "KVYO", "HIMS",
    "RXRX", "GLBE", "PCVX", "SPHR", "HCP",
    # ── Consumer / media / travel ─────────────────────────────────────────────
    "DIS", "PARA", "WBD", "CMCSA", "CHTR", "FUBO", "ROKU", "SIRI",
    "BKNG", "EXPE", "ABNB", "MAR", "HLT", "MGM", "LVS", "WYNN", "CZR",
    "DKNG", "RIVN", "TSLA",
    # ── Financials / high-beta ────────────────────────────────────────────────
    "JPM", "GS", "MS", "BAC", "C", "WFC", "BX", "KKR", "APO", "BN",
    "CG", "ARES", "OWL", "NU", "LC", "OPEN", "COOP", "RKT", "SOFI",
    # ── Energy / commodities ─────────────────────────────────────────────────
    "XOM", "CVX", "COP", "OXY", "HAL", "MPC", "VLO", "DVN", "FANG",
    "EOG", "SLB", "BKR", "HES", "APA", "CTRA", "SM", "RRC", "AR", "EQT",
    "ARCH", "CEIX", "BTU", "METC",
    # ── Biotech / healthcare ─────────────────────────────────────────────────
    "MRNA", "BNTX", "VRTX", "REGN", "GILD", "BIIB", "ALNY", "ARWR",
    "BEAM", "CRSP", "EDIT", "NTLA", "FATE", "SAGE", "ACAD", "INVA",
    "ITCI", "AXSM", "SRTX", "PRGO", "JAZZ", "EXEL", "IOVA", "TGTX",
    "RCUS", "KYMR", "KURA", "FOLD", "PTCT", "RARE",
    # ── Sector ETFs (optionable, liquid) ─────────────────────────────────────
    "XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "ARKK", "ARKG",
    "SMH", "SOXX", "KWEB", "GDX", "GDXJ", "USO", "UNG",
    "TLT", "HYG", "EMB", "EEM", "EWZ", "FXI",
    # ── Misc high-vol / meme-adjacent ────────────────────────────────────────
    "GME", "AMC", "BBBY", "SPCE", "NKLA", "WKHS", "RIDE", "HYLN",
    "PTRA", "REI", "BLNK", "CHPT", "EVGO", "FFIE",
]

# De-duplicate while preserving order
_seen = set()
_deduped = []
for _t in BEAR_CALL_UNIVERSE:
    if _t not in _seen:
        _seen.add(_t)
        _deduped.append(_t)
BEAR_CALL_UNIVERSE = _deduped


def load_universe(path: str = UNIVERSE_FILE,
                  override_path: str = UNIVERSE_OVERRIDE_FILE) -> list:
    """Return the active ticker universe.

    Priority:
      1. override_path (bearcall_universe_override.json) — LLM-generated list
      2. path (bearcall_universe.json) — user-edited list from HTML editor
      3. BEAR_CALL_UNIVERSE constant — embedded fallback
    """
    for p, label in [(override_path, "LLM override"), (path, "JSON file")]:
        if os.path.exists(p):
            try:
                with open(p) as f:
                    tickers = json.load(f)
                if isinstance(tickers, list) and tickers:
                    print(f"[UNIVERSE] loaded {len(tickers)} tickers from {label} ({p})")
                    return [str(t).upper().strip() for t in tickers if t]
            except Exception as e:
                print(f"[UNIVERSE] warning: could not load {p}: {e}")
    print(f"[UNIVERSE] using embedded BEAR_CALL_UNIVERSE ({len(BEAR_CALL_UNIVERSE)} tickers)")
    return list(BEAR_CALL_UNIVERSE)


def cheap_prescreen(tickers: list, max_keep: int = PRESCREEN_DEFAULT_KEEP) -> list:
    """Reduce universe to top max_keep candidates without fetching 1y history.

    Stage A — batch yf.download(period='5d'):
      Gets Close + Volume for all tickers in one call (~10-15s for 300).
      Rejects: price ≤ 0, notional volume (price × volume) < NOTIONAL_VOL_MIN.
      Scores each ticker: 52w-range IV proxy × log(ADV).
      Keeps top STAGE_A_KEEP by score.

    Stage B — per-ticker fast_info on Stage A survivors:
      Checks: beta > BETA_MAX, next earnings within EARNINGS_DAYS days.
      Returns top max_keep by Stage A score (stable ordering).

    All rejects are printed as [PRESCREEN] lines for diagnostic visibility.
    """
    if not tickers:
        return []

    print(f"[PRESCREEN] stage A: batch fetch for {len(tickers)} tickers ...")
    try:
        raw = yf.download(
            tickers, period="5d", auto_adjust=True,
            progress=False, threads=True
        )
    except Exception as e:
        print(f"[PRESCREEN] batch download failed: {e} — falling back to DEFAULT_TICKERS")
        return tickers[:max_keep]

    # yf.download returns multi-level columns when >1 ticker
    # Structure: (field, ticker) — we need Close and Volume
    if isinstance(raw.columns, type(None)) or raw.empty:
        print("[PRESCREEN] empty download result — skipping pre-screen")
        return tickers[:max_keep]

    scores = {}
    stage_a_rejects = {}

    for t in tickers:
        try:
            if isinstance(raw.columns.get_level_values(0) if hasattr(raw.columns, 'get_level_values') else raw.columns, object):
                # Multi-ticker download: columns are (field, ticker)
                try:
                    close_series = raw["Close"][t].dropna()
                    vol_series = raw["Volume"][t].dropna()
                except (KeyError, TypeError):
                    stage_a_rejects[t] = "not in batch download"
                    continue
            else:
                close_series = raw["Close"].dropna()
                vol_series = raw["Volume"].dropna()

            if close_series.empty or vol_series.empty:
                stage_a_rejects[t] = "no price data"
                continue

            price = float(close_series.iloc[-1])
            avg_vol = float(vol_series.mean())

            if price <= 0:
                stage_a_rejects[t] = f"price {price:.2f} ≤ 0"
                continue

            notional = price * avg_vol
            if notional < NOTIONAL_VOL_MIN:
                stage_a_rejects[t] = (f"notional vol ${notional/1e6:.1f}M "
                                      f"< ${NOTIONAL_VOL_MIN/1e6:.0f}M floor")
                continue

            # 52w-range IV proxy: (52w high - 52w low) / 52w low
            # Use the 5d batch data as a cheap proxy; full 52w range comes from
            # fast_info in Stage B. For Stage A scoring, use price range / price.
            price_range = float(close_series.max() - close_series.min())
            iv_proxy = price_range / price if price > 0 else 0

            # Score: IV proxy × log(ADV) — favours volatile + liquid names
            adv_score = math.log(max(avg_vol, 1))
            scores[t] = iv_proxy * adv_score

        except Exception as e:
            stage_a_rejects[t] = f"error: {e}"

    for t, reason in stage_a_rejects.items():
        print(f"[PRESCREEN] stage A reject: {t} — {reason}")

    # Keep top STAGE_A_KEEP by score for Stage B
    stage_a_survivors = sorted(scores, key=lambda x: scores[x], reverse=True)[:STAGE_A_KEEP]
    print(f"[PRESCREEN] stage A: {len(stage_a_survivors)} survivors from {len(tickers)} "
          f"({len(stage_a_rejects)} rejected)")

    # ── Stage B: per-ticker fast_info on top survivors ─────────────────────────
    print(f"[PRESCREEN] stage B: checking earnings + beta on {len(stage_a_survivors)} tickers ...")
    today = datetime.now(timezone.utc).date()
    earnings_cutoff = today + timedelta(days=EARNINGS_DAYS)
    stage_b_rejects = {}
    stage_b_ok = []

    for t in stage_a_survivors:
        try:
            info = yf.Ticker(t).fast_info

            # Beta check
            beta = getattr(info, "beta", None)
            if beta is None:
                # fast_info may not have beta; fall back to info dict
                try:
                    beta = yf.Ticker(t).info.get("beta", None)
                except Exception:
                    beta = None
            if beta is not None and float(beta) > BETA_MAX:
                stage_b_rejects[t] = f"beta {beta:.2f} > {BETA_MAX}"
                continue

            # Earnings proximity check
            # Try fast calendar first, fall back to .calendar
            earn_date = None
            try:
                cal = yf.Ticker(t).calendar
                if cal is not None and not (hasattr(cal, 'empty') and cal.empty):
                    if hasattr(cal, 'get'):
                        ed = cal.get("Earnings Date")
                    elif hasattr(cal, 'iloc'):
                        # DataFrame — first row, "Earnings Date" column
                        ed = cal.get("Earnings Date", [None])
                        if hasattr(ed, 'iloc'):
                            ed = ed.iloc[0] if len(ed) > 0 else None
                    else:
                        ed = None
                    if ed is not None:
                        if hasattr(ed, 'date'):
                            earn_date = ed.date()
                        elif hasattr(ed, '__iter__') and not isinstance(ed, str):
                            items = list(ed)
                            if items:
                                first = items[0]
                                earn_date = first.date() if hasattr(first, 'date') else None
            except Exception:
                earn_date = None

            if earn_date is not None and today <= earn_date <= earnings_cutoff:
                stage_b_rejects[t] = (f"earnings {earn_date} within "
                                      f"{EARNINGS_DAYS}d ({earnings_cutoff})")
                continue

            # Upgrade Stage A score using real 52w range from fast_info.
            # Bear-call scoring: favour names with high IV *and* meaningful pullback.
            # Score = IV proxy × pullback factor × log(ADV)
            # where pullback = (1 - price/52w_high): a name at its 52w high scores 0,
            # a name 20% below scores 0.20. This ensures the pre-screen passes names
            # that bear_call_suitable() can actually approve (not at-highs momentum names).
            try:
                high52 = getattr(info, "year_high", None)
                low52 = getattr(info, "year_low", None)
                price_now = getattr(info, "last_price", None)
                if high52 and low52 and low52 > 0 and price_now and price_now > 0:
                    # Hard reject: too close to 52w high — bear_call_suitable will fail these
                    pct_below_high = (high52 - price_now) / high52
                    if pct_below_high < 0.10:  # within 10% of 52w high
                        stage_b_rejects[t] = (f"price {price_now:.2f} within 10% of "
                                              f"52w high {high52:.2f} ({pct_below_high:.1%} below)")
                        continue
                    iv_52w = (high52 - low52) / low52
                    pullback = pct_below_high   # 0.0 (at high) → 1.0 (at low)
                    adv_score = math.log(max(avg_vol, 1))
                    scores[t] = iv_52w * pullback * adv_score
            except Exception:
                pass  # keep Stage A score

            stage_b_ok.append(t)

        except Exception as e:
            print(f"[PRESCREEN] stage B error for {t}: {e} — keeping")
            stage_b_ok.append(t)  # on error, keep (fail-open for data gaps)

    for t, reason in stage_b_rejects.items():
        print(f"[PRESCREEN] stage B reject: {t} — {reason}")

    # Final: sort surviving set by (updated) score, return top max_keep
    result = sorted(stage_b_ok, key=lambda x: scores.get(x, 0), reverse=True)[:max_keep]

    print(f"[PRESCREEN] stage B: {len(result)} final survivors "
          f"(from {len(stage_a_survivors)}, {len(stage_b_rejects)} rejected)")
    print(f"[PRESCREEN] survivors: {result}")
    return result
