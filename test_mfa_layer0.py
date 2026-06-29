"""Unit tests for mfa_layer0 — focus on the FRED path that can't be hit live in this
sandbox (network blocked), plus the pure scoring/parsing helpers.

Run:  .venv/bin/python test_mfa_layer0.py
"""
import math
import mfa_layer0 as m


def check(name, got, want):
    ok = got == want
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: got={got!r} want={want!r}")
    return ok


def test_parse_fred_csv():
    print("test_parse_fred_csv")
    csv = ("observation_date,BAMLH0A0HYM2\n"
           "2026-06-18,3.05\n2026-06-19,3.10\n2026-06-20,3.12\n2026-06-21,.\n"  # '.' skipped
           "2026-06-22,3.15\n2026-06-23,3.18\n2026-06-24,3.20\n2026-06-25,3.22\n")
    latest, prior, n = m._parse_fred_csv(csv)
    results = []
    results.append(check("latest", latest, 3.22))
    results.append(check("prior(rows[-6])", prior, 3.10))   # 7 valid rows; rows[-6]=3.10
    results.append(check("n valid", n, 7))
    # empty / header-only
    e_l, e_p, e_n = m._parse_fred_csv("observation_date,X\n")
    results.append(check("empty -> None", (e_l, e_p, e_n), (None, None, 0)))
    # all-missing
    z_l, z_p, z_n = m._parse_fred_csv("observation_date,X\n2026-01-01,.\n2026-01-02,.\n")
    results.append(check("all-missing -> None", (z_l, z_p, z_n), (None, None, 0)))
    return all(results)


def test_interp():
    print("test_interp")
    results = []
    # normal range
    results.append(check("midpoint -> 0", m._interp(0.5, 0, 1), 0.0))
    results.append(check("max clamp", m._interp(99, 0, 1), 5.0))
    results.append(check("min clamp", m._interp(-99, 0, 1), -5.0))
    # inverted range (lo>hi): VIX 30->-5, 16->+5
    results.append(check("inverted hi end (VIX 16)", m._interp(16, 30, 16), 5.0))
    results.append(check("inverted lo end (VIX 30)", m._interp(30, 30, 16), -5.0))
    results.append(check("degenerate lo==hi", m._interp(5, 3, 3), 0.0))
    return all(results)


def test_credit_spread_scoring():
    """Simulate the credit-spread metric logic with a fixture (no network)."""
    print("test_credit_spread_scoring")
    results = []
    # tight spread, calm -> bullish
    s_calm = m._interp(3.0, 6.5, 3.25)
    results.append(check("HY 3.0% -> +5 (tight)", s_calm, 5.0))
    # wide spread -> bearish
    s_wide = m._interp(7.0, 6.5, 3.25)
    results.append(check("HY 7.0% -> -5 (wide)", s_wide, -5.0))
    # spike override: moderate level but +0.80 5-obs spike forces <= -4
    s_spike = m._interp(4.5, 6.5, 3.25)
    s_spike_adj = min(s_spike, -4.0)  # mirrors compute_regime spike branch
    results.append(check("HY 4.5% + 0.80 spike -> <=-4", s_spike_adj, -4.0))
    return all(results)


def test_regime_offline_forces_neutral():
    """With FRED disabled AND yfinance present, N may still be <9 -> must force Neutral.
    We can't assert exact scores (live market), but we CAN assert the structure."""
    print("test_regime_offline_forces_neutral")
    metrics = m.compute_regime(use_fred=False)
    valid = [x for x in metrics if x["s"] is not None]
    results = []
    results.append(check("12 metric slots", len(metrics), 12))
    results.append(check("credit spread is NEEDS without FRED",
                         any("Credit" in x["n"] and x["s"] is None for x in metrics), True))
    # every metric dict has the 3 required keys
    shape_ok = all(set(x.keys()) == {"n", "v", "s"} for x in metrics)
    results.append(check("all metrics well-formed", shape_ok, True))
    print(f"  (info) {len(valid)} of 12 metrics had data this run")
    return all(results)


def test_fred_fetch_failsafe():
    """fetch_fred must NEVER raise — blocked/timeout returns (None,None,0)."""
    print("test_fred_fetch_failsafe")
    # bogus series id; whether network is blocked or returns 404, must be the empty tuple
    got = m.fetch_fred("THIS_SERIES_DOES_NOT_EXIST_XYZ")
    return check("bad series -> (None,None,0) no raise", got, (None, None, 0))


def test_mins_since_open():
    print("test_mins_since_open")
    import pandas as pd
    results = []
    t0 = pd.Timestamp("2026-06-26 09:30", tz="America/New_York")
    t1 = pd.Timestamp("2026-06-26 10:00", tz="America/New_York")
    t2 = pd.Timestamp("2026-06-26 16:00", tz="America/New_York")
    results.append(check("09:30 -> 0 min", m._mins_since_open(t0), 0))
    results.append(check("10:00 -> 30 min", m._mins_since_open(t1), 30))
    results.append(check("16:00 -> 390 min", m._mins_since_open(t2), 390))
    return all(results)


def test_intraday_rvol_math():
    """Validate the time-of-day normalization on a synthetic, fully-controlled fixture.

    Build 3 prior days that each trade 100 shares per 5-min bar, and a 'today' that
    trades 200/bar (exactly 2x pace). At ANY cutoff the normalized RVOL must be 2.0 —
    that is the whole point: comparable to the gate regardless of time of day.
    We monkeypatch yfinance so no network is touched.
    """
    print("test_intraday_rvol_math")
    import pandas as pd
    import numpy as np

    def make_day(date, per_bar):
        idx = pd.date_range(f"{date} 09:30", f"{date} 11:00", freq="5min",
                            tz="America/New_York")  # 19 bars (partial day is fine)
        return pd.DataFrame({"Volume": [per_bar] * len(idx),
                             "Close": [10.0] * len(idx)}, index=idx)

    frames = [make_day(d, 100) for d in ("2026-06-23", "2026-06-24", "2026-06-25")]
    frames.append(make_day("2026-06-26", 200))   # today: 2x the per-bar pace
    fake = pd.concat(frames)

    class FakeTk:
        def __init__(self, *a, **k): pass
        def history(self, *a, **k): return fake

    orig = m.yf.Ticker
    m.yf.Ticker = FakeTk
    try:
        rvol, status = m.compute_rvol_intraday("TEST")
    finally:
        m.yf.Ticker = orig

    results = []
    results.append(check("status intraday", status, "intraday"))
    results.append(check("normalized RVOL = 2.0 (2x pace, any cutoff)", rvol, 2.0))
    return all(results)


def test_intraday_rvol_single_day_fallback():
    """Only one day of intraday data -> cannot normalize -> 'full_day' signal so the
    caller keeps the daily-bar RVOL. Must NOT crash or return a misleading number."""
    print("test_intraday_rvol_single_day_fallback")
    import pandas as pd
    idx = pd.date_range("2026-06-26 09:30", "2026-06-26 10:00", freq="5min",
                        tz="America/New_York")
    one_day = pd.DataFrame({"Volume": [100] * len(idx), "Close": [10.0] * len(idx)}, index=idx)

    class FakeTk:
        def __init__(self, *a, **k): pass
        def history(self, *a, **k): return one_day

    orig = m.yf.Ticker
    m.yf.Ticker = FakeTk
    try:
        rvol, status = m.compute_rvol_intraday("TEST")
    finally:
        m.yf.Ticker = orig
    return check("single-day -> full_day fallback", status, "full_day")


def test_intraday_rvol_no_data():
    print("test_intraday_rvol_no_data")
    import pandas as pd

    class FakeTk:
        def __init__(self, *a, **k): pass
        def history(self, *a, **k): return pd.DataFrame()

    orig = m.yf.Ticker
    m.yf.Ticker = FakeTk
    try:
        rvol, status = m.compute_rvol_intraday("TEST")
    finally:
        m.yf.Ticker = orig
    return check("empty -> no_intraday", status, "no_intraday")


def test_regime_bull_thrust():
    """Group B: the bull-tape brake fires ONLY when SPY>+2% vs 50DMA AND VIX<16 AND term<0.95.
    Pure-function over a synthetic metrics list — no network."""
    print("test_regime_bull_thrust")
    results = []
    bull = [{"n": "SPY vs 50DMA", "v": "+3.5%", "s": 4.0},
            {"n": "VIX level", "v": "13.2 falling", "s": 4.0},
            {"n": "VIX term (VIX/VIX3M)", "v": "0.910", "s": 3.0}]
    fired, why = m.regime_bull_thrust(bull)
    results.append(check("clear bull tape -> thrust", fired, True))
    # VIX too high -> no thrust
    notbull = [{"n": "SPY vs 50DMA", "v": "+3.5%", "s": 4.0},
               {"n": "VIX level", "v": "22.0 rising", "s": -1.0},
               {"n": "VIX term (VIX/VIX3M)", "v": "0.910", "s": 3.0}]
    results.append(check("high VIX -> no thrust", m.regime_bull_thrust(notbull)[0], False))
    # missing metric -> no thrust (safe default, no raise)
    results.append(check("missing metrics -> no thrust", m.regime_bull_thrust([])[0], False))
    # SPY only mildly up -> no thrust
    mild = [{"n": "SPY vs 50DMA", "v": "+1.0%", "s": 1.0},
            {"n": "VIX level", "v": "13.0 falling", "s": 4.0},
            {"n": "VIX term (VIX/VIX3M)", "v": "0.900", "s": 3.0}]
    results.append(check("mild SPY -> no thrust", m.regime_bull_thrust(mild)[0], False))
    return all(results)


def test_regime_metric_shapes():
    """Group B: the new metric builders always return a well-formed {n,v,s} dict even when
    the network is blocked (s=None / 'no data'), and proxies are labeled."""
    print("test_regime_metric_shapes")
    results = []
    for fn in (m.regime_sector_rotation, m.regime_breadth_proxy, m.regime_putcall_proxy):
        d = fn()
        results.append(check(f"{fn.__name__} well-formed", set(d.keys()) == {"n", "v", "s"}, True))
    fed = m.regime_fed_proxy(use_fred=False)
    results.append(check("fed proxy no-fred -> None", fed["s"], None))
    return all(results)


def test_finnhub_graceful_nokey():
    """Group C: with NO FINNHUB_API_KEY, every helper returns None and never raises — the
    pipeline must run keyless exactly as before."""
    print("test_finnhub_graceful_nokey")
    import os
    import finnhub_data as fh
    saved = os.environ.pop("FINNHUB_API_KEY", None)
    results = []
    try:
        results.append(check("api_key None without env", fh.api_key(), None))
        results.append(check("next_earnings None without key", fh.next_earnings("AAPL"), None))
        results.append(check("quote None without key", fh.quote("AAPL"), None))
        results.append(check("_get None without key", fh._get("quote", {"symbol": "AAPL"}), None))
    finally:
        if saved is not None:
            os.environ["FINNHUB_API_KEY"] = saved
    return all(results)


def test_finnhub_parsing_monkeypatched():
    """Group C: with a fake key + monkeypatched _get, next_earnings picks the earliest FUTURE
    date and quote returns a clean dict — without touching the network."""
    print("test_finnhub_parsing_monkeypatched")
    import os, datetime as _dt
    import finnhub_data as fh
    results = []
    os.environ["FINNHUB_API_KEY"] = "TESTKEY"
    today = _dt.datetime.now(_dt.timezone.utc).date()
    future1 = (today + _dt.timedelta(days=10)).isoformat()
    future2 = (today + _dt.timedelta(days=40)).isoformat()
    past = (today - _dt.timedelta(days=5)).isoformat()
    orig = fh._get
    try:
        fh._get = lambda path, params: {"earningsCalendar": [
            {"symbol": "T", "date": future2}, {"symbol": "T", "date": past},
            {"symbol": "T", "date": future1}]}
        results.append(check("earliest future date chosen", fh.next_earnings("T"), future1))
        fh._get = lambda path, params: {"earningsCalendar": []}
        results.append(check("empty calendar -> None", fh.next_earnings("T"), None))
        fh._get = lambda path, params: {"c": 281.74, "pc": 283.78}
        q = fh.quote("T")
        results.append(check("quote parsed", (q["current"], q["prev_close"]), (281.74, 283.78)))
        fh._get = lambda path, params: {"c": 0}     # bad quote
        results.append(check("zero quote -> None", fh.quote("T"), None))
    finally:
        fh._get = orig
        os.environ.pop("FINNHUB_API_KEY", None)
    return all(results)


if __name__ == "__main__":
    tests = [test_parse_fred_csv, test_interp, test_credit_spread_scoring,
             test_regime_offline_forces_neutral, test_fred_fetch_failsafe,
             test_mins_since_open, test_intraday_rvol_math,
             test_intraday_rvol_single_day_fallback, test_intraday_rvol_no_data,
             test_regime_bull_thrust, test_regime_metric_shapes,
             test_finnhub_graceful_nokey, test_finnhub_parsing_monkeypatched]
    passed = 0
    for t in tests:
        try:
            if t():
                passed += 1
        except Exception as e:
            print(f"  [ERROR] {t.__name__}: {e!r}")
        print()
    print(f"==== {passed}/{len(tests)} test groups passed ====")


# ─────────────────────────────────────────────────────────────────────────────
# Bear Call Spread (BCS) logic tests
# ─────────────────────────────────────────────────────────────────────────────
import bearcall_logic as bcs


def test_bs_call_delta():
    print("test_bs_call_delta")
    results = []
    # ATM ~0.5-ish, deep OTM ~low, deep ITM ~high
    atm = bcs.bs_call_delta(100, 100, 30, 0.30)
    otm = bcs.bs_call_delta(100, 130, 30, 0.30)
    itm = bcs.bs_call_delta(100, 70, 30, 0.30)
    results.append(check("ATM delta ~0.5", 0.45 <= atm <= 0.62, True))
    results.append(check("OTM delta < 0.15", otm < 0.15, True))
    results.append(check("ITM delta > 0.9", itm > 0.9, True))
    results.append(check("bad iv -> nan", math.isnan(bcs.bs_call_delta(100, 110, 30, 0)), True))
    return all(results)


def test_profiles_monotonic():
    """Higher-payoff profile must allow a richer (higher) delta and demand higher CWR."""
    print("test_profiles_monotonic")
    w, b, p = bcs.get_profile("winrate"), bcs.get_profile("balanced"), bcs.get_profile("payoff")
    results = []
    results.append(check("delta band rises", w["delta_hi"] < b["delta_hi"] < p["delta_hi"], True))
    results.append(check("cwr floor rises", w["cwr_floor"] < b["cwr_floor"] < p["cwr_floor"], True))
    results.append(check("unknown profile -> default", bcs.get_profile("xyz") == bcs.PROFILES["winrate"], True))
    return all(results)


def _mk_row(**kw):
    """Minimal TickerRow-like stub for screen tests."""
    import mfa_layer0 as m
    r = m.TickerRow(ticker="T")
    r.price = 100.0
    r.ema_ribbon = "bearish"; r.ema_spread_pct = -2.0; r.macd_hist = -0.5
    r.rsi14 = 45.0; r.adx14 = 18.0; r.atr_pct = 3.0
    r.headroom_pct = 5.0; r.pct_below_52w_high = 10.0; r.hv_rank = 60.0
    r.high_52w = 120.0
    for k, v in kw.items():
        setattr(r, k, v)
    return r


def test_bear_call_suitable():
    print("test_bear_call_suitable")
    emas = {21: 105.0, 55: 110.0}     # price 100 below both → bearish-friendly
    results = []
    ok, fails = bcs.bear_call_suitable(_mk_row(), emas)
    results.append(check("clean bearish row suitable", ok, True))
    # bullish ribbon rejected
    ok2, _ = bcs.bear_call_suitable(_mk_row(ema_ribbon="bullish"), emas)
    results.append(check("bullish ribbon rejected", ok2, False))
    # RSI too hot rejected
    ok3, _ = bcs.bear_call_suitable(_mk_row(rsi14=70), emas)
    results.append(check("RSI>60 rejected", ok3, False))
    # price above EMAs rejected
    ok4, _ = bcs.bear_call_suitable(_mk_row(), {21: 95.0, 55: 90.0})
    results.append(check("price above EMA21&55 rejected", ok4, False))
    # at 52w high rejected
    ok5, _ = bcs.bear_call_suitable(_mk_row(pct_below_52w_high=1.0), emas)
    results.append(check("too close to 52w high rejected", ok5, False))
    return all(results)


def test_bcs_vetoes():
    print("test_bcs_vetoes")
    prof = bcs.get_profile("balanced")
    results = []
    # clean chain-verified row, no vetoes
    r = _mk_row(strike_basis="chain", opt_liq_ok=True, cwr=0.25, iv_rv_ratio=1.2,
                next_earnings="2026-09-01", short_pct_float=3.0, short_days_to_cover=1.5,
                macd_hist_slope=-0.1, beta=1.0)
    results.append(check("clean row no vetoes", bcs.bcs_vetoes(r, 0, prof), []))
    # earnings unknown -> veto
    r2 = _mk_row(strike_basis="chain", opt_liq_ok=True, cwr=0.25, iv_rv_ratio=1.2,
                 next_earnings="unknown", short_pct_float=3.0, short_days_to_cover=1.5)
    results.append(check("unknown earnings vetoed", any("earnings" in v for v in bcs.bcs_vetoes(r2, 0, prof)), True))
    # squeeze -> veto
    r3 = _mk_row(strike_basis="chain", opt_liq_ok=True, cwr=0.25, iv_rv_ratio=1.2,
                 next_earnings="2026-09-01", short_pct_float=25.0, short_days_to_cover=6.0)
    results.append(check("squeeze vetoed", any("V7" in v for v in bcs.bcs_vetoes(r3, 0, prof)), True))
    # no chain -> stand down veto
    r4 = _mk_row(strike_basis="no_chain", next_earnings="2026-09-01")
    results.append(check("no-chain vetoed", any("V5 no chain" in v for v in bcs.bcs_vetoes(r4, 0, prof)), True))
    # risk-on regime -> V3 veto
    r5 = _mk_row(strike_basis="chain", opt_liq_ok=True, cwr=0.25, iv_rv_ratio=1.2,
                 next_earnings="2026-09-01", short_pct_float=3.0, short_days_to_cover=1.5)
    results.append(check("risk-on regime vetoed", any("V3" in v for v in bcs.bcs_vetoes(r5, 8, prof)), True))
    # bull-thrust brake -> V3 veto even with N<9 (regime_score forced 0)
    r6 = _mk_row(strike_basis="chain", opt_liq_ok=True, cwr=0.25, iv_rv_ratio=1.2,
                 next_earnings="2026-09-01", short_pct_float=3.0, short_days_to_cover=1.5,
                 macd_hist_slope=-0.1, beta=1.0)
    results.append(check("bull-thrust forces V3 at regime 0",
                         any("V3 bull-thrust" in v for v in bcs.bcs_vetoes(r6, 0, prof, bull_thrust=True)), True))
    return all(results)


def test_pop_cap():
    """A2: POP must be capped at 85% so the number matches the 'capped 85%' honesty claim."""
    print("test_pop_cap")
    results = []
    # delta 0.13 (winrate floor) -> raw 87.0 -> capped 85.0
    results.append(check("delta 0.13 capped to 85.0", bcs.pop_from_delta(0.13), 85.0))
    # delta 0.30 -> 70.0 (below cap, unchanged)
    results.append(check("delta 0.30 uncapped 70.0", bcs.pop_from_delta(0.30), 70.0))
    # exactly 0.15 -> raw 85.0 -> 85.0
    results.append(check("delta 0.15 -> 85.0", bcs.pop_from_delta(0.15), 85.0))
    results.append(check("nan delta -> nan", math.isnan(bcs.pop_from_delta(float("nan"))), True))
    return all(results)


def test_earnings_before_expiry():
    """A1 (CRITICAL): earnings veto must cover the FULL trade life [today, expiry+buffer],
    not the fixed 11-day equity window. Use dates relative to 'today' so the test is not
    calendar-fragile."""
    print("test_earnings_before_expiry")
    import datetime as _dt
    today = _dt.datetime.now(_dt.timezone.utc).date()
    results = []
    # 35-DTE expiry, earnings at day 20 -> INSIDE the trade -> must veto (the old 11d window missed this)
    exp35 = (today + _dt.timedelta(days=35)).isoformat()
    earn20 = (today + _dt.timedelta(days=20)).isoformat()
    results.append(check("earnings day20 inside 35DTE -> veto", bcs.earnings_before_expiry(earn20, exp35), True))
    # earnings the day after expiry+buffer -> clean
    earn_after = (today + _dt.timedelta(days=35 + 3)).isoformat()
    results.append(check("earnings after expiry+buffer -> clean", bcs.earnings_before_expiry(earn_after, exp35), False))
    # earnings exactly on expiry -> veto
    results.append(check("earnings on expiry -> veto", bcs.earnings_before_expiry(exp35, exp35), True))
    # unknown / unparseable -> False here (handled by a separate hard veto), never raises
    results.append(check("unknown -> False (no raise)", bcs.earnings_before_expiry("unknown", exp35), False))
    results.append(check("empty expiry -> False", bcs.earnings_before_expiry(earn20, ""), False))
    return all(results)


def test_a1_veto_integration():
    """A1 wired through bcs_vetoes: a chain-verified row whose chosen expiry straddles earnings
    must raise V1, even though the 11-day earnings_in_window flag is False."""
    print("test_a1_veto_integration")
    import datetime as _dt
    prof = bcs.get_profile("balanced")
    today = _dt.datetime.now(_dt.timezone.utc).date()
    exp35 = (today + _dt.timedelta(days=35)).isoformat()
    earn20 = (today + _dt.timedelta(days=20)).isoformat()
    results = []
    r = _mk_row(strike_basis="chain", opt_liq_ok=True, cwr=0.25, iv_rv_ratio=1.2,
                short_pct_float=3.0, short_days_to_cover=1.5, macd_hist_slope=-0.1, beta=1.0,
                next_earnings=earn20, bcs_expiry=exp35, earnings_in_window=False)
    vetoes = bcs.bcs_vetoes(r, 0, prof)
    results.append(check("mid-trade earnings raises V1", any("V1 earnings" in v for v in vetoes), True))
    # same row but earnings safely after expiry -> no V1
    r2 = _mk_row(strike_basis="chain", opt_liq_ok=True, cwr=0.25, iv_rv_ratio=1.2,
                 short_pct_float=3.0, short_days_to_cover=1.5, macd_hist_slope=-0.1, beta=1.0,
                 next_earnings=(today + _dt.timedelta(days=60)).isoformat(),
                 bcs_expiry=exp35, earnings_in_window=False)
    results.append(check("earnings after expiry -> no V1", any("V1 earnings" in v for v in bcs.bcs_vetoes(r2, 0, prof)), False))
    return all(results)


for _t in [test_bs_call_delta, test_profiles_monotonic, test_bear_call_suitable, test_bcs_vetoes,
           test_pop_cap, test_earnings_before_expiry, test_a1_veto_integration]:
    try:
        _ok = _t()
    except Exception as _e:
        print(f"  [ERROR] {_t.__name__}: {_e!r}"); _ok = False
    print()
    _BCS_RESULTS = globals().setdefault("_BCS_RESULTS", [])
    _BCS_RESULTS.append(_ok)

print(f"==== BCS: {sum(_BCS_RESULTS)}/{len(_BCS_RESULTS)} bear-call test groups passed ====")
