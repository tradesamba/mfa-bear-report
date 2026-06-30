# MFA Bear (Bear Call Spread) — Session Handoff

**Purpose:** Complete record of the `bearcall/` fork so a new session can resume. Read this +
`MFA_V6_Session_Handoff.md` (the base MFA V6 / cloud context) first.

**Last updated:** 2026-06-29 (audit + regime/Finnhub/3-profile upgrade session)
**Working dir:** `/local/mnt/workspace/mfa/bearcall`  ·  **Target repo:** `mfa-bear-report` (separate from cloud's `mfa-report`)
**Venv:** `/local/mnt/workspace/mfa/.venv` (python3.12; yfinance, pandas, numpy)

---

## 0. TL;DR

`bearcall/` is a fork of `cloud/` (MFA V6) that screens for the **opposite** setup: the top 4 tickers
each day for a **BEAR CALL SPREAD** (sell OTM call + buy higher call → credit; win if price stays
BELOW the short strike). It keeps V6's deterministic Layer 0 + data-integrity gate (Veto #8) and the
"numbers belong to code" principle, but inverts the screen/scoring/vetoes and adds a real
option-chain step that builds + verifies the actual spread. Governing stance: **negative skew
(loss > gain) → over-rejecting is cheap, under-rejecting is ruinous.** STAND DOWN is common and correct.

**Status: COMPLETE, TESTED (13 base + 7 bear-call groups pass). Repo `mfa-bear-report` is LIVE on
GitHub Actions + Pages (3 profiles generate correctly).** One fix pending push: the Group F
wizard-visibility fix (the Grok/Claude wizard was hidden on STAND-DOWN days). On today's bullish
mega-cap tape all 3 profiles correctly STAND DOWN (0 tradeable) — expected.

**The 2026-06-29 session** did an adversarial audit then implemented 5 work-groups (A–E). Everything
below the audit line is NEW this session. The full local 2-slot workflow was simulated end-to-end.

---

## 1. Files in bearcall/

| File | What |
|---|---|
| `mfa_layer0.py` | V6 data layer + BCS threading + regime (now ~9–10/12) + `--bearcall`/`--profile`/`--all-profiles`/`--slot`/`--out-dir`. Has `analyze()`, `run_bcs_step()` (NEW, profile step split out), `build_profile_reports()` (NEW, builds 3 profiles per run), `_index_redirect()` (NEW), `compute_regime()` (expanded), `regime_bull_thrust()` (NEW). |
| `bearcall_logic.py` | All bear-call logic: BS delta, hv_rank, suitability, score, `select_bcs()` (chain→real spread), vetoes, PROFILES. NEW this session: `pop_from_delta()`, `earnings_before_expiry()`, `_parse_iso_date()`, POP_CAP. |
| `finnhub_data.py` | **NEW** — optional Finnhub helper: `api_key()`, `next_earnings()` (forward date), `quote()` (cross-check), `_get()`. Reads `FINNHUB_API_KEY` env; stdlib urllib; fail-safe to None; fully keyless-safe. |
| `report_html.py` | Bear-branded mobile report + Grok/Claude wizard. NEW: `build_html(..., slot=)` + a 3-profile tab bar. |
| `test_mfa_layer0.py` | 13 base + 7 bear-call test groups. NEW groups: pop-cap, earnings-before-expiry, A1 veto integration, bull-thrust, regime-metric shapes, finnhub graceful-nokey, finnhub parsing. |
| `.github/workflows/mfa.yml` | "MFA Bear Daily Report"; builds all 3 profiles per slot; 6/7 AM PT crons; FINNHUB_API_KEY env. |
| `history.html` | 6-report matrix (pre-market ×3 + mid-session ×3 + latest). |
| `README.md` | Updated: 3-profile URLs, secret setup, schedule, push steps. |
| `V1_MFABear_Comprehensive_Guide.md`, `MFABear_V1_Daily_Cheatsheet.txt` | Operating manual + runbook (attach to LLM steps). NOTE: not re-checked line-by-line this session for the new POP-cap/earnings wording — minor doc drift possible. |
| `requirements.txt` | yfinance, pandas, numpy (Finnhub uses stdlib — no new dep). |

---

## 2. The audit (done first, 2026-06-29) — findings + their fixes

A 5-dimension adversarial workflow ran (math/veto/chain/docs/redteam). **The 5 finder agents STALLED
on this sandbox's 180s watchdog** (the known "heavyweight agent times out" failure from the V6
handoff) — only the synthesis agent survived; findings were then hand-verified against the code.
Verdict was **do-not-ship-until-#1-fixed**. All 4 confirmed issues are now FIXED:

1. **(CRITICAL) Earnings veto only covered an 11-day window** but swing spreads run 21–45 DTE →
   earnings could land mid-trade (the #1 path to max loss). FIXED: see Group A1.
2. **Bull-tape vetoes inert when N<9** (regime forced Neutral, score 0 → V3/beta never fire). FIXED:
   Group B bull-thrust brake.
3. **POP never capped at 85%** despite the claim. FIXED: Group A2.
4. **Long-leg comment vs code mismatch.** FIXED: Group A3.

---

## 3. What this session built (Groups A–E)

**Group A — audit safety fixes (bearcall_logic.py):**
- **A1 (CRITICAL):** `earnings_before_expiry(next_earnings, bcs_expiry, buffer=EARNINGS_BUFFER_DAYS=2)`
  vetoes if a known earnings date is in `[today, chosen expiry + 2d]`. Wired into `bcs_vetoes()` V1.
  Unknown/unparseable earnings still hard-veto. (The old 11-day `earnings_in_window` flag is kept as a
  belt-and-suspenders pre-chain check.)
- **A2:** `pop_from_delta()` + `POP_CAP=85.0` → POP = `min((1-Δ)*100, 85)`. All display sites read the
  capped `row.pop`.
- **A3:** long-leg comment corrected to match the max-CWR-among-floor-clearing-liquid objective.
- **A4:** removed dead `regime_block` param from `select_bcs()`.

**Group B — regime → N≥9 + bull-tape brake (mfa_layer0.py):**
- `compute_regime()` adds: **sector rotation** (canonical, 11 SPDR ETFs vs 50DMA), **market breadth**
  (proxy, 30-name basket >200DMA), **Fed/macro** (proxy, FRED `DFF`), **equity put/call** (proxy,
  SPY/QQQ/IWM nearest-expiry OI). Proxies are value-prefixed `proxy:`. Econ-Surprise + NYSE A/D stay
  `NEEDS FEED` (no free source — honest). Net **N≈8 keyless / 9–10 with FRED**.
- `regime_bull_thrust(metrics)` — deterministic strong-bull detector from ONLY accurate metrics
  (SPY>+2% vs 50DMA AND VIX<16 AND VIX/VIX3M<0.95). Returns `(bool, why)`. Threaded `analyze(...,
  bull_thrust=)` → `bcs_vetoes(..., bull_thrust=)` → forces V3 even when N<9.

**Group C — Finnhub via GitHub secret (finnhub_data.py, NEW):**
- VERIFIED against the user's real free-tier key (2026-06-29): `calendar/earnings` ✅200 (forward
  date), `quote` ✅200, **`stock/candle` ❌403 PREMIUM**, rate-limit 60/min. So Finnhub = earnings-date
  accuracy + price cross-check ONLY; adds **zero** regime metrics; never replaces yfinance for prices.
- `analyze()` now prefers Finnhub's forward earnings date over yfinance `tk.calendar` (sets
  `row.earnings_source`), and does a non-fatal price cross-check flag (`row.price_xcheck`).
- No key → everything returns None and the pipeline runs exactly as before (keyless).

**Group D — 3-profile report matrix (mfa_layer0.py + report_html.py):**
- `--all-profiles --slot {premarket|midsession} --out-dir public [--json]` → `build_profile_reports()`
  fetches each ticker's data ONCE, then runs the profile-specific BCS step 3× and renders
  `{slot}-{winrate,balanced,payoff}.html` (+ optional `.json`). `index.html` = `_index_redirect()`
  landing that meta-refreshes to that slot's winrate and links all 3 + history.
- `report_html.build_html(..., slot=)` renders a 3-profile **tab bar**; each report keeps the full
  one-click copy-Grok → paste → copy-Claude wizard (visibility rules: see Group F).

**Group E — deploy (history.html, mfa.yml, README.md):**
- `history.html` → 6-report matrix (pre-market ×3, mid-session ×3, + Latest).
- `mfa.yml`: crons **`7 13 * * 1-5`** (6 AM PT pre-market, EOD) + **`7 14 * * 1-5`** (7 AM PT
  mid-session, intraday) — UTC for PDT; DST note to +1h each in PST winter. `workflow_dispatch` for
  manual runs. `env: FINNHUB_API_KEY: ${{ secrets.FINNHUB_API_KEY }}` on the generate step. Carries
  forward the OTHER slot's 3 profile files via curl before deploy.
- **Bug caught & fixed during testing:** `--json` arg-parsing (now `nargs="?"` with `__AUTO__`
  sentinel — bare `--json` works in all-profiles mode, `--json PATH` still works single-profile).

**Group F — wizard-visibility fix (report_html.py, post-deploy 2026-06-29):**
- After the user ran the real GitHub Action, the 3 profile pages rendered but the **Grok/Claude
  wizard (Step 1 copy-Grok-prompt, Step 2 paste-reply, Step 3 copy-Claude-prompt) was INVISIBLE**.
- Root cause: the template's JS had `if(STAND_DOWN){ hide step1+step2 }`. The wizard was always
  built into every report but force-hidden on any STAND-DOWN day — and all 3 profiles were STAND
  DOWN, so it vanished on all of them. This conflicted with the explicit requirement that every
  profile report carry the copy-Grok → paste → copy-Claude wizard.
- FIX: visibility now keys off **survivors** (names that CLEARED the integrity gate), not tradeable
  status. New JS uses `HAS_SURVIVORS = D.survivors.length > 0`:
  - tradeable spreads exist → full wizard (unchanged);
  - STAND DOWN **but survivors cleared the gate** (the common case) → wizard SHOWN + an amber
    `#wizardNote` banner explaining "0 tradeable, but N names cleared — run as a watchlist/upside-risk
    check";
  - **zero survivors** (everything failed the integrity gate) → input steps hidden + a "nothing to
    assess" note (genuinely no tickers).
- The Grok prompt already falls back to `survivors` when `top4` is empty (`grok_focus =
  top4_tickers or survivors`), so the prompt is correctly populated (verified: NVDA, AAPL, MSFT).
- Added a `<div id="wizardNote" class="banner hidden">` element above STEP 1. Presentation-only
  change; 13+7 tests still pass. **Needs commit + push + re-run to appear live.**

---

## 4. CLI

```
# single profile, ad-hoc HTML:
python mfa_layer0.py --bearcall --profile winrate --regime --alt [--intraday] --html report.html

# all 3 profiles for a slot (what the workflow runs):
FINNHUB_API_KEY=... python mfa_layer0.py --bearcall --regime --alt [--intraday] \
    --all-profiles --slot premarket --out-dir public --json

python test_mfa_layer0.py        # 13 base + 7 bear-call groups
```
`--no-fred` skips FRED (drops the Fed proxy + credit/curve → N may fall below 9; bull-thrust brake
still works). Default tickers = the 10-name DEFAULT_TICKERS (bullish mega-caps → frequent STAND DOWN).

**PROFILES** (delta and credit/width are financially COUPLED — floors calibrated to real chains):
| profile | short Δ | CWR floor | ~POP |
|---|---|---|---|
| winrate (default) | 0.13–0.20 | 0.09 | ~80–85% |
| balanced | 0.20–0.28 | 0.18 | ~73–78% |
| payoff | 0.30–0.38 | 0.26 | ~63–68% |

---

## 5. Verification status (this session)

- **Tests:** 13 base + 7 bear-call groups pass (`.venv/bin/python test_mfa_layer0.py`).
- **byte-compile:** all 5 modules OK.
- **Live, real Finnhub key:** earnings (AAPL 2026-07-29, NVDA 2026-08-25) + quote ($281.74) work.
  yfinance ALSO works in this sandbox right now (network healthier than the V6 handoff implied).
- **Full pipeline live:** NVDA/AAPL/MSFT — regime N=8 (no-fred), POP capped (NVDA 84%), NVDA built a
  real chain but vetoed on V5 iv/rv<1, 0 tradeable. Correct.
- **Full 2-slot workflow SIMULATED** into `/tmp/wf_sim`: 6 HTML + 6 JSON + index (redirects to latest
  slot) + history; every history.html link resolves. This is exactly what Pages will serve.

---

## 6. Deployment (repo IS live as of 2026-06-29 — Group F fix still to push)

**Status:** the user pushed `bearcall/` to GitHub, added the `FINNHUB_API_KEY` secret, enabled Pages,
and **ran the Action successfully** — 3 profile pages generated and served. That surfaced the Group F
wizard-visibility bug, now fixed in `report_html.py` locally. **Remaining: commit + push the Group F
fix and re-run the Action** so the wizard shows:
```
git add report_html.py MFABear_Session_Handoff.md
git commit -m "Show Grok/Claude wizard on STAND-DOWN days when survivors cleared the gate"
git push
```
Then Actions → MFA Bear Daily Report → Run workflow (or wait for the next cron).

First-time setup steps (for reference / a fresh clone):
GitHub auth: PAT (classic, `repo` scope) as git password; PUBLIC repo for free Pages.
1. **Rotate the Finnhub key** — it was pasted in plaintext in the 2026-06-29 chat. Regenerate at
   finnhub.io dashboard and update the repo secret.
2. `cd bearcall; git init; git add mfa_layer0.py bearcall_logic.py finnhub_data.py report_html.py
   requirements.txt test_mfa_layer0.py history.html V1_MFABear_Comprehensive_Guide.md
   MFABear_V1_Daily_Cheatsheet.txt .github; git commit -m "..."; git branch -M main; git remote add
   origin https://github.com/<you>/mfa-bear-report.git; git push -u origin main`
3. Settings → Pages → Source: **GitHub Actions**.
4. Settings → Secrets and variables → Actions → New repository secret: `FINNHUB_API_KEY` = (rotated key).
5. Actions → MFA Bear Daily Report → Run workflow (manual trigger; works from mobile).
6. Bookmark `https://<you>.github.io/mfa-bear-report/`.
README has the exact steps.

---

## 7. Honest limitations / known gaps

- **POP = (1-BS-delta), capped 85%** — theoretical, not a measured win rate; call skew unmodeled.
- **3 regime metrics are LABELED proxies** (breadth/Fed/put-call); econ-surprise + NYSE A/D unsourced.
- **Finnhub free tier: no candles (premium)** — yfinance stays the price source.
- **yfinance ~15-min delayed**; Finnhub quote cross-check flags >3% divergence but never overrides.
- **Dark pool + true GEX = N/A** (FlashAlpha's domain doesn't resolve — fabricated/defunct; SpotGamma/
  Intrinio paid; Polygon free too heavy). Never estimated. Decision locked: keep N/A.
- **Docs (guide/cheatsheet) not re-audited** line-by-line for the new POP-cap/earnings/regime wording.
- **STAND DOWN (0 of 4) is common and correct.** Never loosen gates to fill 4.

---

## 8. Open items / pick up here

1. **Push the Group F wizard fix + re-run the Action** (Section 6). Repo is already live and the
   Action runs green; this push just makes the Grok/Claude wizard visible on STAND-DOWN days.
2. Optional: re-audit `V1_MFABear_Comprehensive_Guide.md` + cheatsheet for the new POP-cap/earnings/
   N≥9 wording (minor drift possible).
3. Optional: widen DEFAULT_TICKERS toward names that can actually be bearish (default 10 are bullish
   mega-caps → frequent STAND DOWN).
4. Optional: tune profile floors / `OPT_SPREAD_MAX` after observing live fills.
5. Alt-data (dark pool/GEX) still unsourced — paid-feed decision (shared with V6).

## 9. Bottom line for a new session
- Authoritative bear logic = `bearcall/bearcall_logic.py` + `bearcall/mfa_layer0.py` (+ `finnhub_data.py`).
- This session: audit (4 fixes) + regime N≥9 w/ labeled proxies + bull-tape brake + Finnhub secret +
  3-profile reports + 6/7 AM PT schedule + Group F wizard-visibility fix. 13+7 tests pass; repo is
  live on Actions/Pages (3 profiles generate).
- Remaining work: push the Group F `report_html.py` fix + re-run the Action (Section 6); rotate the
  Finnhub key that was shared in chat.
- **Heavyweight parallel subagents STALL in this sandbox** — implement inline/sequentially, not via
  agent teams. Web search is also blocked here; use WebFetch (routes through Anthropic) or domain
  knowledge cross-checked against probes.
