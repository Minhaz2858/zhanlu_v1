# How Our Price Forecasting Works

> A plain-English guide to the forecasting system.
> No technical background required.

---

## The Big Picture

We built a system that looks at historical chemical prices and tries to answer three simple questions for each product:

1. **Will the price go up or down?**
2. **By how much?**
3. **What should we do about it — buy more, sell, or wait?**

The system does not just guess. It uses statistics, multiple forecasting models, market news, and a feedback loop that learns from past mistakes. Every recommendation comes with a confidence label and a plain-language explanation so buyers and sellers can decide whether to trust it.

This document explains the journey from "raw price data" to "a recommendation you can read on the dashboard" — without code.

---

## The Journey, Step by Step

Think of the system as a factory assembly line. Raw material (price history) goes in one end, and a finished product (a recommendation with explanation) comes out the other. There are 20 workstations along the line.

### Step 1 — Find What to Forecast

The system scans the database for products that have a price history and registers them for forecasting. Each product (for example "C5 resin" or "isoprene") becomes a tracked target. It only adds new ones — it never duplicates work.

### Step 2 — Gather the Data

For each product, the system pulls:

- **The product's own daily or weekly price history** (the core signal)
- **Extra context** (only when switched on):
  - How much of the product was actually sold (from the ERP system)
  - Customer demand signals and how many suppliers are active
  - Factory operating rates, warehouse inventories, and import prices
  - Upstream feedstock prices from market data providers (隆众资讯)
  - Oil prices (Brent crude, naphtha)

These extras are optional and can be turned on or off per product. They help the models understand *why* a price might move, not just *that* it moved.

### Step 3 — Clean the Data

Real-world data is messy. Prices are sometimes missing, sometimes wrong (typos, stale feeds), and sometimes recorded at irregular intervals. Before anything else happens, the system:

- Fills in missing values sensibly
- Flags and smooths suspicious outliers
- Aligns everything to a regular daily or weekly rhythm
- Scores the overall data quality so we know how much to trust the rest of the pipeline

If the data is too sparse (for example fewer than 50 price points), the product is flagged as "insufficient data" and gets a low-confidence label later.

### Step 4 — Test the Models Honestly (Backtest)

Before we trust any model on the future, we test it on the past. The system uses a method called **walk-forward backtesting**:

1. Take the first chunk of history, predict the next few days, compare to what actually happened.
2. Slide the window forward, repeat.
3. Do this for 3-day, 7-day, and 30-day horizons.

This produces an **error score** (MAPE — "Mean Absolute Percentage Error") for every model, at every horizon. A model that is off by 5% on average is better than one off by 15%.

Crucially, we always compare against the dumbest possible strategy: **"tomorrow's price equals today's price"** (the "naive" baseline). If a fancy model cannot beat that, we do not use it.

### Step 5 — Run All the Forecasting Models

The system keeps a pool of models, each with a different philosophy:

| Model | In Plain English |
|---|---|
| **Naive (last price)** | "Price stays the same." Our floor — everything must beat this. |
| **Seasonal naive** | "Price repeats last week's pattern." |
| **ETS (exponential smoothing)** | "Recent prices matter more than old ones." Weights decay over time. |
| **ARIMA** | "The price has momentum and mean-reversion patterns I can model." The workhorse for products with real structure. |
| **STL decomposition** | "Let me split trend + season + noise, then extrapolate the trend." |
| **Mean reversion** | "Prices swing but always pull back toward the average." |
| **XGBoost** | A machine-learning model that finds subtle patterns across many features. Only used when there is enough history (90+ days). |
| **Chronos-Bolt / Moirai** | Pre-trained "foundation" AI models that forecast zero-shot. Optional, used for richer products. |
| **VAR** | Models several correlated products together (e.g., a chemical and its feedstock). |

Not all models run for every product. Some require more data; some are switched on or off by feature flags. A "model selector" also retires models that have performed poorly for several runs in a row.

### Step 6 — Blend the Models (Ensemble)

No single model is right all the time. Instead of picking a winner, we **blend** them. Each model gets a weight based on how well it did in the backtest:

- Good models get more weight
- Bad models get less, or zero if they are more than twice as bad as the best
- The weighting automatically adapts — when models disagree a lot, the blend diversifies; when they agree, it concentrates on the best one

The blend is also **regime-aware**: in a bull market we trust trend-following models more; in a bear market we trust mean-reversion more; in volatile markets we lean on the simple baselines.

### Step 7 — The Honesty Gate

This is the most important quality control in the whole system.

**The rule**: if the blended model's backtest error is *worse* than simply holding the last price, we throw the model out and use the naive baseline instead. We never silently ship a forecast that is worse than doing nothing.

There is also a "soft" version: if the model is only slightly worse than naive, we blend part-model / part-naive rather than hard-switching. And the "advanced guard" adds extra safety nets:

- **Stale data check**: if the latest price is more than 2 weeks old, flag it
- **Volatility blend**: in wild markets, lean harder on the simple baseline
- **Change clamp**: cap day-to-day forecast moves at ±15% so we never predict an absurd spike

The dashboard always shows when a forecast was downgraded to the naive baseline, so users know.

### Step 8 — Check Coherence With Feedstock

Some products are made *from* others (e.g., C5 resin is made from naphtha). If our forecast says the product price will rise while its feedstock price falls — beyond what the historical spread allows — the system clamps the forecast back into a sensible relationship. This prevents economically impossible predictions.

### Step 9 — Layer in Market News (Intelligence Overlay)

Numbers are not everything. The system reads approved market intelligence events from the last 48 hours (supply disruptions, plant outages, geopolitical news, etc.) and matches them to each product. Each event has:

- A **direction** (pushes price up or down)
- A **magnitude** (minor / moderate / major / critical)
- A **certainty** (how sure we are it matters)

The system converts these into a percentage adjustment and applies it to the forecast. The effect is scaled by horizon — a supply disruption matters most in the first week, then fades as markets adjust.

When the "event calibration" feature is on, we use *historically measured* impact sizes (from past events) instead of gut-feel numbers. This makes the adjustment evidence-based.

### Step 10 — Apply Policy (Bias Correction + Volatility)

Two more adjustments:

**Bias correction**: if our forecasts have been systematically too high or too low over the last 30 days, we nudge them back. The correction is damped (only 35% of the measured bias) and capped at ±2.5% — we fix drift, we don't overreact.

**Volatility regime**: we classify the current market as Normal / Moderate / High volatility based on recent daily price swings. In high-volatility markets we widen uncertainty; in calm markets we tighten it. This affects the bull/bear scenarios in the next step.

### Step 11 — Build Scenarios (Base / Bull / Bear)

Nobody can predict the future exactly. Instead of one number, we give three:

- **Base case**: the central forecast
- **Bull case**: a plausible "things go well" upside
- **Bear case**: a plausible "things go wrong" downside

The width of the bull-bear range comes from how wrong our models have been in the past (conformal prediction). A product we forecast well gets a tight range; one we struggle with gets a wide range. The system also labels overall confidence as **High / Medium / Low** based on the backtest error.

### Step 12 — Probability of a Price Rise

We convert the forecast into a single, intuitive number: **the probability the price will be higher in `h` days than it is today** (called `p_rise`).

- `p_rise = 70%` means "we think there's a 70% chance the price goes up"
- `p_rise = 25%` means "we think it's more likely to fall"

This number, combined with the expected size of the move, is what drives the final recommendation.

### Step 13 — The Directional Classifier

Separately from the price forecast, we train a small machine-learning model whose only job is to predict **up or down** (a sign, not a magnitude). It uses features like recent returns, momentum, volatility, seasonality, and optionally upstream/feedstock signals.

We do not trust it blindly. We run a **statistical significance test** (a binomial test, p < 0.01) to check whether its accuracy is real or just luck. Only if it passes do we say the product has a "directional edge." Most products currently do **not** pass — and the system honestly reports that.

### Step 14 — Assign a Trust Tier

Each product gets a user-facing trust label so buyers know how much weight to put on the recommendation:

| Tier | Color | What it means |
|---|---|---|
| **High confidence** | Green | A product we historically forecast well AND the model beats the naive baseline |
| **Medium confidence** | Yellow | Decent track record, beats naive |
| **Directional reference** | Orange | Model is worse than naive — use the direction as a rough guide only |
| **Insufficient data** | Red | Not enough history to say anything meaningful |

This tier is shown directly on the dashboard next to each product.

### Step 15 — Make the Recommendation (Decision Engine)

This is where everything comes together. The decision engine looks at:

- The probability of a rise (`p_rise`)
- The expected size of the move (`expected_change_pct`)
- Whether the directional classifier has a real edge
- The trust tier

**The rules** (simplified):

1. If there is no statistical edge, or the data is too thin → **WATCH** (do nothing, low confidence)
2. If `p_rise` is high (≥ 70%) AND the expected rise is big enough (≥ 3%) AND we have a directional edge → **BUY** (备货)
3. If `p_rise` is low (≤ 30%) AND the expected drop is big enough (≥ 3%) AND we have a directional edge → **SELL** (出货)
4. Otherwise → **HOLD** (按需跟进)

**The key principle**: the system will not issue a BUY or SELL unless there is *statistically significant* evidence of a directional edge. Most products currently land on WATCH — and that is the honest, correct answer. A wrong "do nothing" is far cheaper than a wrong "buy everything."

The thresholds (70%, 3%, etc.) can be tuned per product and are optimized automatically over time (see Step 19).

### Step 16 — Explain the Drivers

For products using the XGBoost model, the system reports the **top 5 factors** driving the forecast — for example "naphtha price +32%, operating rate +21%, inventory -14%...". This helps users sanity-check the recommendation against their own market knowledge.

It also produces a combined confidence label (high / medium / low) that factors in both the trust tier and whether the honesty gate triggered.

### Step 17 — Write the Analyst Brief

A plain-language, 7-section Chinese report is generated for each product, mirroring how a human analyst would write a weekly market note:

| Section | Title | What it says |
|---|---|---|
| 1 | 市场动态 (Market Update) | Recent events and news affecting this product |
| 2 | 价格数据 (Price Data) | Current price, 7-day and 30-day changes, 30-day moving average |
| 3 | 上游传导 (Upstream Transmission) | How feedstock prices are flowing through |
| 4 | 供需研判 (Supply & Demand) | Demand signal, inventory, operating rate, import parity |
| 5 | 价格预测 (Price Forecast) | Base/bull/bear numbers, directional signal, confidence |
| 6 | 关注触发 (Watch Triggers) | What would change the recommendation |
| 7 | 风险提示 (Risk Warning) | Key risks and caveats |

**Important**: the LLM (AI) only *writes the prose*. It never decides the buy/hold/sell action. That decision is 100% rule-based and deterministic, so it is fully auditable and never hallucinated.

### Step 18 — Generate the Narrative

Two short Chinese blocks are added under the recommendation:

- **【预测依据】** — why we think the price will move this way (trend, model agreement, uncertainty)
- **【建议逻辑】** — why this specific action, and what would need to change to flip it

For example, a HOLD might say: "There is a directional edge but the expected move does not meet the buy/sell threshold. Recommend following demand. This would flip to BUY if the rise probability exceeds 70% and the expected gain exceeds 3%."

### Step 19 — Log the Decision and Score It Later (ROI Loop)

Every recommendation is saved to a decision log with:

- The product, the date, the action, the confidence
- The probability and expected change that drove it
- The price *at the time of the decision* (critical for scoring later)

When the forecast horizon passes (e.g., 7 days later), the system looks up the *actual* price and scores whether the call was right:

- A BUY is "correct" if the price actually went up
- A SELL is "correct" if the price actually went down
- The ROI (return on following the advice) is computed and stored

Over time this builds a **track record** for each product: hit rate, average ROI, number of realized decisions. This is shown on the dashboard as "decision ROI."

### Step 20 — Automatically Tune the Thresholds (Nightly Job)

Once we have enough scored decisions (≥ 30) and the directional classifier is at least better than a coin flip (≥ 45% accuracy), a nightly job searches for better buy/sell thresholds. It does this by grid-searching combinations and measuring which would have produced the best historical ROI.

**Safety**: the new thresholds are always written as **STAGED**, never auto-activated. A human admin must review and promote them. This prevents the system from silently talking itself into risky behavior.

---

## The Self-Improving Loop

The most powerful feature of the system is that it learns from its own mistakes:

```
  Forecast  →  Decision Log  →  Wait for horizon  →  Score ROI
      ↑                                                    ↓
      ←  Bias Correction  ←  Drift Adjust  ←  Accuracy Log
      ↑                                                    ↓
      ←  Threshold Autotune (staged)  ←  Admin review  ←——┘
```

- If forecasts drift, bias correction pulls them back
- If a model starts failing, drift auto-adjust blends toward the naive baseline
- If thresholds are suboptimal, the autotuner proposes better ones (staged, human-approved)
- If accuracy degrades, the system flags it

The system never stops learning, and never changes its own behavior without a human gate for the high-stakes decisions.

---

## What the Dashboard Shows You

### The Decision Board

A table with one row per product. For each product you see:

- **Current price** and the date it was recorded
- **Forecast price** for the next 7 days and the expected % change
- **Probability of a rise** (`p_rise`) as a percentage
- **The recommendation**: BUY (green, 建议备货), SELL (red, 建议出货), HOLD (blue, 按需跟进), or WATCH (amber, 建议观望)
- **Confidence**: High / Medium / Low
- **Trust tier badge**: colored dot (green/yellow/orange/red) with a Chinese label
- **Directional accuracy**: how often the up/down call has been right historically
- **Market events**: how many recent intelligence events affect this product, and their net bias
- **Volatility regime**: Normal / Moderate / High
- **One-liner summary** and a longer narrative explaining the logic
- **Analyst brief**: the 7-section report (expandable)
- **Decision ROI**: the track record (hit rate, realized count, average ROI)
- **Top drivers**: the 5 factors most influencing this forecast
- **SKU breakdown**: for ERP-linked products, the material-code-level detail

### The Forecast Chart

A line chart for a single product showing:

- **Actual history** (solid line)
- **Base / Bull / Bear forecast** (shaded fan)
- **Previous AI forecast** (for comparison — did we get it right last time?)
- **Execution bands** (target buy/sell zones)
- An **overlay panel** with all the metadata: intelligence events, policy adjustments, trust tier, probability, directional signal, and the decision

---

## Design Principles (In Plain English)

1. **Honesty over optimism.** If we can't beat "price stays the same," we say so. The dashboard shows when a forecast was downgraded.

2. **Statistical proof, not gut feel.** A directional call is only issued if it passes a rigorous significance test. Most products honestly land on "watch."

3. **The AI explains, never decides.** The language model writes the report, but the buy/hold/sell action is 100% rule-based and auditable. No hallucinated decisions.

4. **Nothing breaks the dashboard.** Every add-on (ROI, drivers, brief, news) is wrapped in safety nets. If one fails, the rest still loads.

5. **Everything is switchable.** Every feature is behind a flag. We can turn anything off instantly with zero regression.

6. **It learns from mistakes.** Decision logs → ROI scoring → bias correction → threshold tuning. The loop is always running.

7. **Per-product, not one-size-fits-all.** What works for isoprene does not work for cracked C9. Thresholds, trust tiers, and corrections are all per-product.

---

## Current Performance (August 2026 Benchmark)

| Metric | Value | What it means |
|---|---|---|
| 7-day forecast error (MAPE) | 9.4% | Good — under the 10% "High confidence" bar |
| 7-day directional accuracy | 17.9% | **Worse than a coin flip** — the up/down classifier needs improvement |
| Products with a real directional edge | 3 of 22 (14%) | Most products currently get "watch" — which is the honest answer |
| Most common recommendation | WATCH | Expected — the system refuses to guess without statistical proof |

**The takeaway**: our *price level* forecasts are good (9.4% error). Our *direction* forecasts are currently poor. The system is honest about this — it will not issue buy/sell calls until the directional evidence is statistically solid. Improving the directional classifier is the top priority going forward.

---

## Glossary (Plain-English Definitions)

| Term | What it means |
|---|---|
| **MAPE** | Mean Absolute Percentage Error — "on average, how far off our forecasts are, as a % of the true price." Lower is better. 10% means we're within 10% on average. |
| **Naive baseline** | The dumbest forecast: "tomorrow = today." Every model must beat this to be trusted. |
| **Ensemble** | Blending multiple models' forecasts together, weighted by how accurate each has been. |
| **Honesty gate** | The rule that downgrades to the naive baseline whenever the model isn't actually better. |
| **Conformal interval** | A statistically honest uncertainty band — "we expect the truth to land in this range 90% of the time." |
| **p_rise** | The probability (0–100%) that the price will be higher in N days than today. |
| **Directional edge** | Statistically significant evidence that we can predict up vs. down better than chance. |
| **Trust tier** | A colored badge (green/yellow/orange/red) telling users how much to trust a given product's forecast. |
| **ROI loop** | The closed feedback system that scores past decisions and uses the results to improve future ones. |
| **Feature flag** | An on/off switch for a capability, so we can enable or disable features per product or environment. |
| **SKU** | Stock-Keeping Unit — a specific material code within a product family (e.g., a specific grade of resin). |
