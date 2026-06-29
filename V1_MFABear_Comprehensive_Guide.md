# Comprehensive Guide to MFA Bear — Bear Call Spread Edition
## Version 1.0 — built on MFA V6 (Deterministic Data Layer)

> **What this is.** A fork of MFA V6 that hunts the OPPOSITE setup. Where MFA V6 finds bullish
> breakouts to buy, **MFA Bear finds bearish-to-neutral names to sell call premium against** — i.e.
> the top 4 tickers each day for which a **BEAR CALL SPREAD** (short call vertical / call credit
> spread) can be opened, weekly or swing.
>
> **It inherits 100% of V6's data architecture** — the deterministic Layer 0, the data-integrity
> gate (Veto #8), "numbers belong to code, judgment to LLMs," the 12-metric regime, and the honesty
> rule. What changes is the *direction* of the screen, the scoring, the vetoes, and the addition of
> an option-chain step that builds and verifies the actual spread.
>
> **Design provenance:** an agent team (4 independent designs → adversarial verification →
> synthesis) produced this spec. Its governing stance: a bear call spread has **negative skew**
> (max loss > max gain), so **over-rejecting is cheap and under-rejecting is ruinous.** Every
> ambiguous choice was resolved toward the safer, higher-win-rate-protecting option.

---

## ⚠️ Disclaimer

A bear call spread is a **defined-risk, premium-selling** strategy with an **asymmetric payoff**:
you win often but small (you keep the credit), and lose rarely but larger (width − credit). A high
win RATE does not mean a high expectancy. Backtested credit-spread win rates of 65–85% are
plausible in optimized conditions; any figure above 85% is aspirational/simulated. POP shown by
this system is **delta-implied (theoretical), not a measured win rate.** No trade is presented as
">85% confidence." **This is not financial advice.**

---

## 0. WHAT A BEAR CALL SPREAD IS (orientation)

- **Structure:** SELL an out-of-the-money call (short leg, collect credit), BUY a higher-strike call
  (long leg, defines risk). Net **credit** received up front.
- **Max profit** = net credit — realized if price closes **below the short strike** at expiry.
- **Max loss** = width − credit — if price closes **above the long strike**.
- **Breakeven** = short strike + credit.
- **You win when** the stock stays flat, falls, or rises only modestly (stays below the short strike).
- **Your enemy** is a rally through the short strike: an earnings gap, a squeeze, a melt-up.
- **You are selling volatility** — elevated implied vol = richer credit = better entry.

```
            profit
              │   ┌──────────  +credit (max profit, price ≤ short strike)
              │  /
   ───────────┼─/──────────────────  price at expiry →
              │/        short K   long K
       −(w−c) ┘──────────  max loss (price ≥ long strike)
```

> **The core tension you must choose (PROFILES).** A safer short strike (low delta) wins more often
> (high POP) but collects a smaller credit relative to width (CWR) — worse payoff. A richer short
> strike (higher delta) pays better but wins less often. **You cannot maximize both.** MFA Bear
> exposes this as a `--profile` switch (see §5). Pick the one matching your edge; the guide documents
> all three.

---

## 1. THE THREE-LAYER ARCHITECTURE (inherited from V6)

```
LAYER 0 — DETERMINISTIC DATA + BCS ENGINE  (code, NO LLM)   →  mfa_layer0.py + bearcall_logic.py
  Fetch OHLCV/splits/earnings/ADV/short-interest (yfinance) + regime macro (yfinance+FRED)
  Compute technicals locally; run the V6 data-integrity gate (Veto #8) UNCHANGED
  BEAR-CALL SCREEN (cheap, price-derived): suitability filter + Bear-Call Suitability Score
  OPTION-CHAIN STEP (survivors only): build the real spread (strikes/credit/delta/POP), verify
  Emit Top 4 bear call spreads — or STAND DOWN
        │
        ▼
LAYER 1 — GROK  (UPSIDE-RISK sentiment: what could make this RALLY through the short strike?)
        │
        ▼
LAYER 2 — CLAUDE  (confirm the Top 4: bullish-catalyst check, squeeze check, management plan)
```

**Division of labor:** API = all numbers, technicals, and the option-chain economics. Grok =
bullish-catalyst / squeeze / hype risk. Claude = final confirmation + downgrade on upside risk.

---

## 2. LAYER 0 — THE BEAR-CALL SCREEN

### 2A. Data-integrity gate (Veto #8) — UNCHANGED from V6 ✅
52-week bounds, true-ATH sanity, freshness, split/day-jump. It runs first and is now **more**
load-bearing: strike anchoring leans on ATH / 52w-high / resistance levels, so corrupt price data
would corrupt strike placement. Any CONFLICT → dropped before the BCS screen.

### 2B. Suitability filter (cheap, price-derived) — any failure = NOT suitable
A name must look bearish-to-neutral and have room to sell calls above it:

| Check | Reject if |
|---|---|
| Trend (ribbon) | `ema_ribbon == "bullish"`; or `"mixed"` UNLESS bearish-confirmed (price < EMA21 & EMA55 & macd_hist ≤ 0) |
| Price vs EMAs | `price > EMA21 AND price > EMA55` (not neutral) |
| Momentum | `macd_hist > 0` |
| Extension | `ema_spread_pct > 3%` |
| RSI | `> 60` (too strong) or `< 30` (oversold-bounce risk) |
| Trend strength | `ADX ≥ 25` while ribbon not bearish |
| Overhead room | headroom to nearest resistance NaN or outside **[1.5%, 12%]** |
| Near highs | `pct_below_52w_high < 3%` |
| Premium floor | `ATR% < 1.5%` (too quiet → thin credit) |
| Vol rank | `hv_rank < 50` (don't sell vol below its own 1-year median) |

> **Resistance** = the lowest of {EMA21, EMA55, 20-day high, 52w high} that sits above price — the
> ceiling the short strike will be placed above. No ceiling above price ("open sky") → reject.
>
> **RVOL polarity FLIPS.** In MFA V6, RVOL > 150% was a GO (breakout confirmation). For a credit
> seller, high relative volume is a **caution** (`≥2.0` = danger / blow-off), not a green light.

### 2C. Bear-Call Suitability Score (0–100) — replaces V6's 40/30/30 confluence
Per-category floors (weakest-link must clear — a high total is not enough):

| Category | Wt | Floor | Inputs |
|---|---|---|---|
| A. Bearish/neutral trend | 30 | **≥18** | ribbon (bearish 30 / bearish-confirmed-mixed 15), macd_hist<0 +5, ema_spread<−1 +5 |
| B. Premium richness | 25 | **≥15, chain-verified** | hv_rank pre-screen; once chain pulled, iv/rv>1 and CWR dominate |
| C. Resistance geometry | 20 | — | headroom peaks at ~4–8%, decays outside |
| D. Liquidity | 15 | **opt_liq_ok** | ADV ≥ 2M + option-leg OI/vol/spread |
| E. Absence of bullish catalyst | 10 | — | no earnings, low short-interest, P/C not call-heavy, beta ok |
| (RVOL adjust) | ± | — | +5 if RVOL ≤ 1.3; −15 if ≥ 2.0 |

**Eligibility for Top 4:** `bear_call_score ≥ 70` AND every category floor met AND no veto AND a
**chain-verified** structure. Rank survivors by score, take the top 4. **No name below 70 is ever
published to fill a slot.** Fewer than 4 qualify → publish fewer; zero → **STAND DOWN.**

---

## 3. THE OPTION-CHAIN STEP (mandatory to publish a trade)

A bear call spread is only tradeable with **real strikes and a real credit.** ATR-proxy strike
estimates are STAND-DOWN only — the system pulls the actual yfinance option chain for suitable
survivors and builds the spread:

1. **Expiry:** swing sweet spot **30–40 DTE** (default), else any 21–45; weekly **5–9 DTE** only if
   regime is not bullish. Reject < 5 (gamma) or > 50.
2. **Short strike:** the strike whose **Black-Scholes delta** (from the chain's snapshot IV) sits in
   the profile's band (see §5). Must be **above price** and, where possible, **at/above resistance**
   (a structural buffer beyond the statistical one). Never sell ATM/ITM.
3. **Long strike / width:** searched across candidate strikes to best meet the profile's
   credit/width floor while keeping both legs liquid.
4. **Verification:** credit, width, CWR, breakeven, short delta, short IV, iv/rv ratio, and POP
   (= 1 − short delta, theoretical) are all computed from the chain. Option liquidity (OI ≥ 250,
   vol ≥ 50, per-leg relative bid/ask ≤ 10%) must pass, or the trade is STAND DOWN.

> **Delta is Black-Scholes from a delayed snapshot IV — theoretical, not a live greek. Call skew is
> not modeled.** POP is delta-implied and capped at 85% in all output.

### Management (document in every trade)
- **Take profit** at ~50% of max credit.
- **Stop** at ~2× credit received (i.e. loss ≈ 1× credit).
- **Tested-strike alarm:** short delta rising to ~0.45–0.50 → roll up-and-out **for a credit only**
  (never debit, never into earnings).
- **Close before any earnings, no exception.** Swing time-stop at DTE ≤ 7.
- **Naked calls banned.** Per-trade risk ≤ 1–2% equity, aggregate ≤ 6%, ≤ 2 BCS per sector.

---

## 4. VETOES (bear-call set) — any ONE = no trade

| # | Veto | Trigger |
|---|---|---|
| **V1** | Earnings (HARD) | next earnings within [today, expiry + 2d] — **or earnings date unknown/stale → veto.** Covers the full trade life. |
| **V2** | Bullish momentum | macd_hist > 0 with rising slope; **or** RSI > 60; **or** price > EMA8 > EMA21 stacked up (any one) |
| **V3** | Regime risk-on | regime score > +6 (broad bull) |
| **V4** | Strong uptrend | bullish ribbon; **or** ema_spread > 4% |
| **V5** | Low-IV / thin credit | no chain → STAND DOWN; or CWR < profile floor; or iv/rv < 1.0 |
| **V6** | Liquidity | ADV < 2M; or option legs fail OI/vol/spread; unknown option liquidity → STAND DOWN |
| **V7** | Squeeze / beta | short interest ≥ 20% of float; or days-to-cover ≥ 5 (global, any price); beta > 1.5 in a risk-on regime |
| **V8** | Data integrity | UNCHANGED from V6 — runs first |

---

## 5. PROFILES — the win-rate ↔ payoff switch (`--profile`)

The delta/credit tradeoff is unavoidable, so MFA Bear makes it an explicit choice. Floors are
empirically calibrated to what real ~21–45 DTE liquid large-cap chains actually produce (credit/
width is financially coupled to delta — the floor must match the band or the gate is unsatisfiable):

| Profile | Short delta band | CWR floor | ~POP | Character |
|---|---|---|---|---|
| **winrate** (default) | 0.13–0.20 | ≥ 0.09 | ~80–85% | Highest win rate; smallest credit relative to risk. Matches "high success rate is paramount." |
| **balanced** | 0.20–0.28 | ≥ 0.18 | ~73–78% | Practitioner default; reasonable win rate, less brutal payoff. |
| **payoff** | 0.30–0.38 | ≥ 0.26 | ~63–68% | Best risk:reward per trade; lower win rate, more frequent (smaller) losses. |

> **Break-even reality:** at the winrate profile, CWR ≈ 0.10 means you risk ~10× the credit — you
> need a very high win rate just to break even after costs. This is the classic high-win-rate /
> small-edge profile; the gates exist to protect that edge. **Never loosen a gate to fill four
> slots** — for a negative-skew strategy the forced marginal trade is the most expensive mistake.

---

## 6. REGIME (inherited collection, inverted interpretation)

Same 12-metric collection + the `<9 metrics available → force Neutral` rule. But the axis inverts:
a bear call spread wants flat/down/mildly-up tape; a broad bull melt-up is its worst environment.

- **FAVORABLE** (SPY at/below falling 21/55 EMA, weak breadth, VIX ~18–28 stable): full trade count
  allowed — but the per-ticker bar is **never lowered** (downtrends have the most violent snap-back
  rallies; loosening into them concentrates face-ripper risk).
- **NEUTRAL** (chop, VIX 14–18): allowed, count-capped.
- **HOSTILE / BLOCK** (broad bull thrust, new highs on expanding breadth, VIX < 13 falling): block
  all new entries → regime-level STAND DOWN.
- **VIX both-tails block:** < 13 (thin premium / melt-up) and > 30–35 or a fast daily spike (crisis
  gap risk — defined risk doesn't stop a same-session gap). The sweet spot is the **middle** band.

> Regime is **necessary, not sufficient** — per-ticker squeeze (V7) and uptrend (V4) vetoes still
> fire inside a FAVORABLE regime.

---

## 7. HONEST LIMITATIONS (must appear in output)

- **No free real-time greeks/IV feed.** `hv_rank` and ATR are PROXIES (never labeled `iv_rank`/
  `delta`). The option chain is a delayed snapshot; delta is BS-from-snapshot-IV; call skew unmodeled.
- **POP is delta-implied, capped at 85%, NOT a measured win rate** — reduced by drift, gaps, early
  assignment, slippage, and forced early closes. Always pair with R:R.
- **Gap risk** is only mitigated by the VIX / earnings / squeeze blocks; a single-name overnight gap
  through both strikes is the residual danger of the strategy.
- **Dark pool / GEX = N/A** (no free feed). `putcall_oi` is nearest-expiry only — a coarse caution.
- **STAND DOWN (including 0 of 4) is a valid, common, correct output**, especially in bull/quiet/
  crisis regimes. On a strongly bullish mega-cap tape, the right answer is usually no trade.

## Appendix — honest probability statement (use verbatim)
> "This is a defined-risk, negative-skew credit strategy: it wins often and small, loses rarely and
> larger. POP shown is delta-implied (theoretical), capped at 85%, and is NOT a measured win rate.
> Numbers came from a market-data feed, not a language model. A setup failing any gate has an
> undefined win rate and is a no-trade. STAND DOWN is the correct output when no candidate is safe."
