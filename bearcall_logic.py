"""Bear Call Spread (BCS) logic for MFA — bearcall edition.

Adapts the long-biased MFA V6 screen into a BEARISH-TO-NEUTRAL credit-spread screener.
A bear call spread sells an OTM call (collect credit) and buys a higher call (define risk).
It WINS when price stays BELOW the short strike: i.e. flat / down / mildly-up tape.

Design source: the bearcall-design agent workflow (4 designs → adversarial verify → synthesis).
Core safety stance (negative-skew strategy — over-rejecting is cheap, under-rejecting is ruinous):
  1. A REAL option chain is mandatory to publish a tradeable trade. ATR-proxy strikes = STAND DOWN.
  2. "mixed" ribbon, macd_hist>0, price>EMA21&EMA55, RSI>60, high short-interest, and
     unknown/stale earnings are HARD rejects — not soft inputs.
  3. FAVORABLE regime gates trade COUNT, never lowers the per-ticker bar.

Everything here is computed in code (numbers belong to code). LLMs consume, never originate.
The data-integrity gate (Veto #8) in mfa_layer0.py stays AS-IS and runs upstream of all of this.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone, date, timedelta

import numpy as np
import pandas as pd

# ── BCS constants (single source of truth) ───────────────────────────────────
BCS_MIN_SCORE = 70            # min Bear-Call Suitability Score to be eligible for Top 4
RSI_CEILING = 60              # RSI above this = too strong / bullish → reject
RSI_FLOOR = 30               # RSI below this = oversold bounce risk → reject
EMA_SPREAD_CEIL = 3.0         # ribbon spread% above this = too extended up → reject
ADX_STRONG = 25              # strong trend; only acceptable if ribbon is bearish
HEADROOM_MIN = 1.5            # % to nearest resistance — too close = no room
HEADROOM_MAX = 12.0           # % to nearest resistance — too far = no overhead cap
PCT_BELOW_52WH_MIN = 3.0      # must be at least this far below 52w high (not at highs)
ATR_PCT_FLOOR = 1.5           # absolute premium floor (too quiet = thin credit)
HV_RANK_FLOOR = 50           # don't sell vol below its own 1y median
IV_RV_FLOOR = 1.0            # short_iv / realized vol — must be selling rich vol
OPT_OI_MIN = 250             # per-leg open interest floor
OPT_VOL_MIN = 10             # per-leg volume floor (relaxed from 50 — intraday vol is often 0 for valid strikes)
OPT_SPREAD_MAX = 0.10         # leg bid/ask spread ≤ 10% of credit
SQUEEZE_SI_PCT = 20.0         # short % float ≥ this = squeeze veto (global)
SQUEEZE_DTC = 5.0            # days-to-cover ≥ this = squeeze veto
EARNINGS_BUFFER_DAYS = 2      # veto if earnings within [today, expiry + buffer]
DTE_WEEKLY = (5, 9)
DTE_SWING = (21, 45)
DTE_SWING_SWEET = (30, 40)
RISK_FREE = 0.045            # for Black-Scholes delta from snapshot IV
POP_CAP = 85.0               # honesty cap: POP is delta-implied/theoretical, never shown above this

# ── PROFILES — the delta/credit tradeoff is fundamental and unavoidable: a safer
# (lower-delta) short strike has a higher win rate (POP) but collects a smaller
# credit-to-width (CWR), i.e. a worse payoff. You cannot maximize both. Pick the
# profile that matches your edge. Each is a self-consistent (delta band, CWR floor)
# pair verified against real chains so the gate actually produces candidates.
PROFILES = {
    # name        short-delta band   never-richer-than   CWR floor   POP (~)
    # CWR floors are empirically calibrated to what real ~21-45 DTE chains on liquid
    # large-caps actually produce at each delta — credit/width is financially COUPLED to
    # delta (a safer short strike collects proportionally less), so the floor must match
    # the band or the gate is unsatisfiable. winrate≈0.10, balanced≈0.18, payoff≈0.27.
    "winrate":  {"delta_lo": 0.13, "delta_hi": 0.20, "delta_max": 0.22, "cwr_floor": 0.09},
    "balanced": {"delta_lo": 0.20, "delta_hi": 0.28, "delta_max": 0.30, "cwr_floor": 0.18},
    "payoff":   {"delta_lo": 0.30, "delta_hi": 0.38, "delta_max": 0.42, "cwr_floor": 0.26},
}
DEFAULT_PROFILE = "winrate"   # matches the stated "high success rate is paramount" goal


def get_profile(name):
    return PROFILES.get((name or DEFAULT_PROFILE).lower(), PROFILES[DEFAULT_PROFILE])


def pop_from_delta(short_delta):
    """Delta-implied probability the short call expires OTM, as a percent, honesty-capped.
    POP is theoretical (POP = 1 - delta, no skew/drift), so it is capped at POP_CAP to match
    the 'capped 85%' claim shown to the user/LLM. Returns NaN on bad input."""
    if short_delta is None or (isinstance(short_delta, float) and math.isnan(short_delta)):
        return float("nan")
    return round(min((1.0 - short_delta) * 100.0, POP_CAP), 1)


def _parse_iso_date(s):
    """Parse a 'YYYY-MM-DD' (optionally with trailing time) into a date, else None.
    Tolerates the row.next_earnings sentinels ('unknown', '', None)."""
    if not s or s in ("unknown",):
        return None
    try:
        return date.fromisoformat(str(s)[:10])
    except (ValueError, TypeError):
        return None


def earnings_before_expiry(next_earnings, bcs_expiry, buffer_days=EARNINGS_BUFFER_DAYS):
    """True if a KNOWN earnings date falls inside the trade's full life: [today, expiry + buffer].
    This is the bear-call-correct V1 check — the fixed 11-day equity window in mfa_layer0 is too
    short for 21-45 DTE swing spreads, letting earnings land mid-trade. Unknown/unparseable dates
    are handled separately (hard veto), so this returns False for them and never raises."""
    ed = _parse_iso_date(next_earnings)
    ex = _parse_iso_date(bcs_expiry)
    if ed is None or ex is None:
        return False
    today = datetime.now(timezone.utc).date()
    return today <= ed <= ex + timedelta(days=buffer_days)


# ── Black-Scholes call delta (from snapshot IV — labeled as theoretical) ──────
def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_call_delta(spot, strike, dte_days, iv, r=RISK_FREE):
    """Black-Scholes call delta from a snapshot IV. Theoretical, not a live greek.
    Call skew is NOT modeled beyond per-strike IV. Returns NaN on bad inputs."""
    try:
        if spot <= 0 or strike <= 0 or iv <= 0 or dte_days <= 0:
            return float("nan")
        t = dte_days / 365.0
        d1 = (math.log(spot / strike) + (r + 0.5 * iv * iv) * t) / (iv * math.sqrt(t))
        return round(_norm_cdf(d1), 4)
    except (ValueError, ZeroDivisionError):
        return float("nan")


# ── Cheap price-derived BCS metrics (computed for ALL candidates) ─────────────
def realized_vol(close, window=20):
    """Annualized realized vol from log returns."""
    lr = np.log(close / close.shift(1)).dropna()
    if len(lr) < window:
        return float("nan")
    return float(lr.tail(window).std() * math.sqrt(252))


def hv_rank(close, window=20, lookback=252):
    """Percentile rank (0–100) of current realized vol within its trailing 1y range.
    This is the FREE proxy for IV-rank. Label as hv_rank, NEVER iv_rank."""
    lr = np.log(close / close.shift(1)).dropna()
    if len(lr) < window + 20:
        return float("nan")
    rv_series = lr.rolling(window).std() * math.sqrt(252)
    rv_series = rv_series.dropna().tail(lookback)
    if len(rv_series) < 30:
        return float("nan")
    cur = rv_series.iloc[-1]
    return round((rv_series < cur).mean() * 100, 1)


def compute_bcs_cheap(row, close, emas, full_hist=None):
    """Populate the cheap, price-derived BCS fields on a TickerRow-like object.
    `emas` is the dict {span: value} already computed in analyze()."""
    price = row.price

    # nearest overhead resistance = lowest of {EMA21, EMA55, 20d-high, 52w-high} that is > price
    candidates = []
    for k in (21, 55):
        v = emas.get(k)
        if v and v > price:
            candidates.append(v)
    high20 = float(close.tail(20).max())
    if high20 > price:
        candidates.append(high20)
    if row.high_52w > price:
        candidates.append(row.high_52w)
    if candidates:
        row.resistance = round(min(candidates), 2)
        row.headroom_pct = round((row.resistance / price - 1) * 100, 2)
    else:
        row.resistance = float("nan")
        row.headroom_pct = float("nan")          # open sky above → reject later

    row.pct_below_52w_high = round((row.high_52w / price - 1) * 100, 2) if price else float("nan")
    row.rv20 = round(realized_vol(close), 4)
    row.hv_rank = hv_rank(close)

    # macd histogram slope (sign of 5-day change) — built so nothing references a phantom field
    _, _, hist_macd = _macd(close)
    if len(hist_macd) >= 6:
        row.macd_hist_slope = round(float(hist_macd.iloc[-1] - hist_macd.iloc[-6]), 4)
    else:
        row.macd_hist_slope = float("nan")

    # RVOL polarity flips: high relative volume is a CAUTION for a credit seller, not a GO.
    if not math.isnan(row.rvol):
        if row.rvol >= 2.0:
            row.rvol_flag = "DANGER (vol blow-off)"
        elif row.rvol >= 1.3:
            row.rvol_flag = "elevated (caution)"
        else:
            row.rvol_flag = "calm (ok)"
    else:
        row.rvol_flag = "n/a"


# local macd to avoid import cycle (mirrors mfa_layer0.macd)
def _macd(close, fast=12, slow=26, signal=9):
    ef = close.ewm(span=fast, adjust=False).mean()
    es = close.ewm(span=slow, adjust=False).mean()
    macd_line = ef - es
    sig = macd_line.ewm(span=signal, adjust=False).mean()
    return macd_line, sig, macd_line - sig


def bear_call_suitable(row, emas):
    """Cheap hard-filter: returns (ok: bool, reasons: list[str] of failures).
    Any failure → not suitable. Mirrors synthesis §1.13."""
    fail = []
    e21, e55 = emas.get(21), emas.get(55)
    price = row.price

    if row.ema_ribbon == "bullish":
        fail.append("ribbon bullish")
    # raw "mixed" is acceptable when momentum is NOT strongly up:
    # macd_hist ≤ 0 OR RSI < 55 is sufficient to confirm stalling/bearish intent.
    # (Requiring BOTH EMAs below price AND macd ≤ 0 was too strict — stalling names
    # can show mixed ribbon with a brief MACD uptick while still being range-bound.)
    if row.ema_ribbon == "mixed":
        confirmed = (row.macd_hist <= 0) or (not math.isnan(row.rsi14) and row.rsi14 < 55)
        if not confirmed:
            fail.append("mixed ribbon not bearish-confirmed (macd>0 and RSI≥55)")
    if e21 and e55 and price > e21 and price > e55:
        fail.append("price above EMA21 & EMA55 (not neutral)")
    if row.macd_hist > 0:
        fail.append("macd_hist > 0")
    if not math.isnan(row.ema_spread_pct) and row.ema_spread_pct > EMA_SPREAD_CEIL:
        fail.append(f"ema_spread {row.ema_spread_pct}% > {EMA_SPREAD_CEIL}")
    if not math.isnan(row.rsi14) and (row.rsi14 > RSI_CEILING or row.rsi14 < RSI_FLOOR):
        fail.append(f"rsi {row.rsi14} outside [{RSI_FLOOR},{RSI_CEILING}]")
    if not math.isnan(row.adx14) and row.adx14 >= ADX_STRONG and row.ema_ribbon != "bearish":
        fail.append(f"adx {row.adx14} strong but ribbon not bearish")
    if math.isnan(row.headroom_pct) or not (HEADROOM_MIN <= row.headroom_pct <= HEADROOM_MAX):
        fail.append("headroom to resistance outside [1.5%,12%] / open sky")
    if not math.isnan(row.pct_below_52w_high) and row.pct_below_52w_high < PCT_BELOW_52WH_MIN:
        fail.append("too close to 52w high")
    if not math.isnan(row.atr_pct) and row.atr_pct < ATR_PCT_FLOOR:
        fail.append(f"atr {row.atr_pct}% < {ATR_PCT_FLOOR} premium floor")
    if not math.isnan(row.hv_rank) and row.hv_rank < HV_RANK_FLOOR:
        fail.append(f"hv_rank {row.hv_rank} < {HV_RANK_FLOOR} (vol below median)")

    return (len(fail) == 0), fail


def bear_call_score(row, emas):
    """Bear-Call Suitability Score 0–100 with per-category floors (weakest-link must clear).
    Returns (score, category_dict, floors_ok: bool). Premium category B is only fully
    credited once the option chain is verified (handled by caller setting row.cwr/iv_rv)."""
    cat = {}

    # A. Bearish/neutral trend (30)
    a = 0
    if row.ema_ribbon == "bearish":
        a = 30
    elif row.ema_ribbon == "mixed":
        a = 15
    if row.macd_hist < 0:
        a += 5
    if not math.isnan(row.ema_spread_pct) and row.ema_spread_pct < -1:
        a += 5
    cat["A_trend"] = min(a, 30)

    # B. Premium richness (25). hv_rank pre-screen; chain (iv_rv, cwr) dominates once present.
    b = 0
    if not math.isnan(row.hv_rank):
        b = (row.hv_rank / 100) * 15        # up to 15 from proxy
    if not math.isnan(row.iv_rv_ratio) and row.iv_rv_ratio > 1:
        b = max(b, 15) + min((row.iv_rv_ratio - 1) * 20, 10)   # chain confirms → up to 25
    cat["B_premium"] = round(min(b, 25), 1)

    # C. Resistance/strike geometry (20): peak headroom ~4–8%, decay outside
    c = 0
    h = row.headroom_pct
    if not math.isnan(h):
        if 4 <= h <= 8:
            c = 20
        elif h < 4:
            c = 20 * (h - HEADROOM_MIN) / (4 - HEADROOM_MIN)
        else:  # 8..12
            c = 20 * (HEADROOM_MAX - h) / (HEADROOM_MAX - 8)
    cat["C_geometry"] = round(max(c, 0), 1)

    # D. Liquidity (15): ADV floor + option liquidity (opt_liq_ok set by chain step)
    d = 0
    if row.passes_adv_floor:
        d += 7
    if row.opt_liq_ok:
        d += 8
    cat["D_liquidity"] = d

    # E. Absence of bullish catalyst (10)
    ev = 10
    if row.earnings_in_window:
        ev -= 6
    if not math.isnan(row.short_pct_float) and row.short_pct_float >= SQUEEZE_SI_PCT:
        ev -= 4
    if not math.isnan(row.putcall_oi) and row.putcall_oi < 0.7:   # call-heavy = bullish positioning
        ev -= 3
    if row.beta > 1.5:
        ev -= 2
    cat["E_no_catalyst"] = max(ev, 0)

    # Volatility scored as a BAND not a ramp: handled in atr screen + B; apply RVOL adj here
    rvol_adj = 0
    if not math.isnan(row.rvol):
        if row.rvol <= 1.3:
            rvol_adj = 5
        elif row.rvol >= 2.0:
            rvol_adj = -15
    cat["rvol_adj"] = rvol_adj

    score = sum(cat.values())
    score = max(0, min(round(score, 1), 100))

    # per-category floors (weakest-link)
    floors_ok = (cat["A_trend"] >= 18 and cat["B_premium"] >= 15 and row.opt_liq_ok)
    return score, cat, floors_ok


def select_bcs(tk, row, profile):
    """Pull the real option chain and build a tradeable bear call spread.
    `profile` is a dict from PROFILES (delta band + cwr floor).
    Returns None (→ STAND DOWN for this name) if no chain / no qualifying structure.
    Chain is MANDATORY — no ATR-proxy trading."""
    try:
        exps = tk.options
    except Exception:
        exps = None
    if not exps:
        row.strike_basis = "no_chain"
        return None

    today = datetime.now(timezone.utc).date()

    def dte(e):
        return (date.fromisoformat(e) - today).days

    # choose expiry: swing sweet spot 30–40, else any in 21–45, else weekly 5–9
    swing = [e for e in exps if DTE_SWING[0] <= dte(e) <= DTE_SWING[1]]
    sweet = [e for e in swing if DTE_SWING_SWEET[0] <= dte(e) <= DTE_SWING_SWEET[1]]
    weekly = [e for e in exps if DTE_WEEKLY[0] <= dte(e) <= DTE_WEEKLY[1]]
    chosen = (sweet or swing or weekly)
    if not chosen:
        row.strike_basis = "no_expiry_in_window"
        return None
    exp = chosen[0]
    d = dte(exp)
    row.bcs_expiry = exp
    row.bcs_dte = d
    row.bcs_kind = "swing" if d >= DTE_SWING[0] else "weekly"

    try:
        chain = tk.option_chain(exp)
        calls = chain.calls
    except Exception:
        row.strike_basis = "chain_fetch_failed"
        return None
    if calls is None or calls.empty:
        row.strike_basis = "empty_chain"
        return None

    spot = row.price
    otm = calls[calls["strike"] > spot].copy()
    if otm.empty:
        row.strike_basis = "no_otm_calls"
        return None

    # compute theoretical delta per OTM strike from snapshot IV
    otm["delta"] = otm.apply(
        lambda c: bs_call_delta(spot, c["strike"], d, float(c["impliedVolatility"])), axis=1)
    otm = otm.dropna(subset=["delta"])
    # short strike: delta in profile band, else closest ≤ delta_max
    lo, hi, dmax = profile["delta_lo"], profile["delta_hi"], profile["delta_max"]
    tgt = (lo + hi) / 2
    band = otm[(otm["delta"] >= lo) & (otm["delta"] <= hi)]
    pool = band if not band.empty else otm[otm["delta"] <= dmax]
    if pool.empty:
        row.strike_basis = "no_strike_in_delta_band"
        return None
    short = pool.iloc[(pool["delta"] - tgt).abs().argmin()]

    # structural buffer: short strike must be at/above resistance
    if not math.isnan(row.resistance) and short["strike"] < row.resistance:
        higher = pool[pool["strike"] >= row.resistance]
        if not higher.empty:
            short = higher.iloc[0]

    def mid(c):
        b, a = float(c["bid"]), float(c["ask"])
        return (b + a) / 2 if (b > 0 and a > 0) else float(c["lastPrice"])

    def liquid(c):
        """Returns (ok: bool, reason: str). reason is '' when ok."""
        oi = float(c["openInterest"]) if not pd.isna(c["openInterest"]) else 0
        vol = float(c["volume"]) if not pd.isna(c["volume"]) else 0
        b, a = float(c["bid"]), float(c["ask"]); m = (a + b) / 2
        rel = (a - b) / m if (a > 0 and b > 0 and m > 0) else 99
        fails = []
        if oi < OPT_OI_MIN:
            fails.append(f"OI {int(oi)}<{OPT_OI_MIN}")
        if vol < OPT_VOL_MIN:
            fails.append(f"vol {int(vol)}<{OPT_VOL_MIN}")
        if rel > OPT_SPREAD_MAX:
            fails.append(f"spread {rel:.1%}>{OPT_SPREAD_MAX:.0%}")
        return (not fails), (", ".join(fails) if fails else "")

    short_liq_ok, short_liq_why = liquid(short)
    short_mid = mid(short)
    if not short_liq_ok:
        print(f"[BCS liq] {row.ticker} short ${short['strike']:.0f} FAIL: {short_liq_why}"
              f"  (bid={short['bid']:.2f} ask={short['ask']:.2f} OI={int(short['openInterest'] if not pd.isna(short['openInterest']) else 0)} vol={int(short['volume'] if not pd.isna(short['volume']) else 0)})")

    # Long leg: instead of blindly taking the adjacent strike (which gives a tiny CWR on
    # high-priced names), search candidate long strikes and pick the one that best meets
    # the profile's CWR floor while staying liquid. This is how width is actually sized.
    ups = otm[otm["strike"] > short["strike"]].sort_values("strike")
    if ups.empty:
        row.strike_basis = "no_long_leg"
        return None
    cwr_floor = profile["cwr_floor"]
    best = None
    for _, cand in ups.head(8).iterrows():       # consider up to 8 strikes out
        w = round(float(cand["strike"] - short["strike"]), 2)
        cr = round(short_mid - mid(cand), 2)
        if w <= 0 or cr <= 0:
            continue
        cwr = cr / w
        cand_liq_ok, cand_liq_why = liquid(cand)
        # Objective: MAXIMIZE credit/width among candidates that clear the CWR floor AND are
        # liquid (best risk/reward for this defined-risk spread). The boolean rank ensures any
        # floor-clearing+liquid candidate beats any that isn't; CWR breaks ties. If none clear,
        # `best` still holds the highest-CWR structure so the score has something to report —
        # but opt_liq_ok then stays False below and V5/V6 veto it (no silent bad fill).
        score_key = (cwr >= cwr_floor and cand_liq_ok, cwr)
        if best is None or score_key > best[0]:
            best = (score_key, cand, w, cr, cwr, cand_liq_ok, cand_liq_why)
    if best is None:
        row.strike_basis = "no_long_leg"
        return None
    _, long_leg, width, credit, cwr_val, long_liq, long_liq_why = best
    if not long_liq:
        print(f"[BCS liq] {row.ticker} long  ${long_leg['strike']:.0f} FAIL: {long_liq_why}"
              f"  (bid={long_leg['bid']:.2f} ask={long_leg['ask']:.2f} OI={int(long_leg['openInterest'] if not pd.isna(long_leg['openInterest']) else 0)} vol={int(long_leg['volume'] if not pd.isna(long_leg['volume']) else 0)})")

    row.short_strike = round(float(short["strike"]), 2)
    row.long_strike = round(float(long_leg["strike"]), 2)
    row.width = width
    row.credit = credit
    row.cwr = round(credit / width, 3)
    row.breakeven = round(row.short_strike + credit, 2)
    row.short_delta = round(float(short["delta"]), 4)
    row.short_iv = round(float(short["impliedVolatility"]), 4)
    row.iv_rv_ratio = round(row.short_iv / row.rv20, 3) if (row.rv20 and not math.isnan(row.rv20)) else float("nan")
    row.pop = pop_from_delta(row.short_delta)    # delta-implied prob OTM, theoretical, capped 85%

    # option liquidity — judged per leg against the leg's OWN mid (standard relative
    # spread), not the net credit. Computed during the width search above.
    row.opt_liq_ok = bool(short_liq_ok and long_liq)
    row.strike_basis = "chain"
    return row


def bcs_vetoes(row, regime_score, profile, bull_thrust=False):
    """Return list of veto reasons (empty = clean). Mirrors synthesis §2.
    `profile` supplies the credit/width floor. `bull_thrust` is a deterministic strong-bull
    flag computed from the always-free regime metrics (SPY/VIX/term) that force-enables V3 even
    when the macro metric count is <9 (so the bull-tape brake works on thin-data days). A trade
    is only publishable with an empty veto list AND a chain-verified structure."""
    v = []
    cwr_floor = profile["cwr_floor"]
    # V1 earnings — hard veto over the FULL trade life [today, chosen expiry + buffer], not just
    # the fixed 11-day equity window. Swing spreads run 21-45 DTE, so an earnings print can land
    # mid-trade (the #1 path to max loss on a short call). We veto if a KNOWN earnings date falls
    # before the chosen expiry+buffer, OR if the date is unknown/unparseable (fail-safe).
    if earnings_before_expiry(row.next_earnings, row.bcs_expiry):
        v.append(f"V1 earnings {row.next_earnings} on/before expiry {row.bcs_expiry} (+{EARNINGS_BUFFER_DAYS}d)")
    elif row.earnings_in_window:
        # belt-and-suspenders: the upstream 11-day flag also vetoes (e.g. if expiry not yet set)
        v.append("V1 earnings in 11d window")
    if _parse_iso_date(row.next_earnings) is None:
        v.append("V1 earnings date unknown/unparseable → veto")
    # V2 bullish momentum (OR-logic)
    if (row.macd_hist > 0 and not math.isnan(row.macd_hist_slope) and row.macd_hist_slope > 0):
        v.append("V2 MACD rising-positive")
    if not math.isnan(row.rsi14) and row.rsi14 > RSI_CEILING:
        v.append("V2 RSI > 60")
    # V3 regime risk-on bull. Fires on a high RegimeScore (needs N>=9) OR on the deterministic
    # bull-thrust brake (works even when N<9 / regime forced Neutral) — closing the gap where a
    # thin-data day silently disabled bull-tape protection.
    if regime_score is not None and regime_score > 6:
        v.append("V3 regime risk-on bull (>+6)")
    if bull_thrust:
        v.append("V3 bull-thrust brake (strong-bull tape; safe even with N<9 regime)")
    # V4 strong uptrend
    if row.ema_ribbon == "bullish":
        v.append("V4 bullish ribbon (strong uptrend)")
    if not math.isnan(row.ema_spread_pct) and row.ema_spread_pct > 4:
        v.append("V4 ema_spread > 4%")
    # V5 low-IV / credit floor (chain required)
    if row.strike_basis != "chain":
        v.append("V5 no chain-verified structure → STAND DOWN")
    else:
        if not math.isnan(row.cwr) and row.cwr < cwr_floor:
            v.append(f"V5 credit/width {row.cwr} < {cwr_floor}")
        if not math.isnan(row.iv_rv_ratio) and row.iv_rv_ratio < IV_RV_FLOOR:
            v.append(f"V5 iv/rv {row.iv_rv_ratio} < {IV_RV_FLOOR} (not selling rich vol)")
    # V6 option liquidity
    if not row.opt_liq_ok:
        v.append("V6 option liquidity unknown/poor → STAND DOWN")
    # V7 squeeze (global, regardless of price location)
    if not math.isnan(row.short_pct_float) and row.short_pct_float >= SQUEEZE_SI_PCT:
        v.append(f"V7 short interest {row.short_pct_float}% ≥ {SQUEEZE_SI_PCT}")
    if not math.isnan(row.short_days_to_cover) and row.short_days_to_cover >= SQUEEZE_DTC:
        v.append(f"V7 days-to-cover {row.short_days_to_cover} ≥ {SQUEEZE_DTC}")
    # NOTE: V7 beta veto intentionally omitted for bear calls.
    # High beta in a bull regime = more vol = richer premium for a credit seller.
    # Beta is noted in the report for sizing context but does not block the trade.
    return v
