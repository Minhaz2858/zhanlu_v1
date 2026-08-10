# Forecasting Engine Accuracy Report — 2026-08-08
Methodology: Walk-forward backtest (true OOS) + Engine internal expanding-window CV

## Executive Summary
| Metric | Value | Assessment |
|--------|-------|-----------|
| 7-day MAPE (walk-forward) | 9.4% avg (1.4%-22.2%) | Moderate |
| 14-day MAPE (walk-forward) | 14.0% avg (3.8%-32.0%) | Below target |
| 30-day MAPE (walk-forward) | 19.7% avg (8.7%-39.7%) | Poor |
| 7-day DirAcc (walk-forward) | 17.9% avg | Worse than random |
| Model vs Naive (DB) | Beats naive on 3/22 products | Ensemble underperforms |
| Realized accuracy tracking | Not implemented (all null) | Missing |

Overall: Directional forecasts less accurate than coin flip. 86% of products have ensemble worse than holding last price.

## 1. Walk-Forward Backtest (True OOS, 8 products)
| Product | 7d MAPE | 14d MAPE | 30d MAPE |
|---------|---------|----------|----------|
| Dicyclopentadiene | 1.3% | 10.7% | 21.1% |
| Isoprene | 22.2% | 32.0% | 39.7% |
| Cracked C5 | 7.7% | 12.8% | 18.6% |
| Cracked C9 | 18.7% | 24.4% | 24.2% |
| Piperylene | 17.7% | 12.2% | 12.9% |
| Naphtha | 2.8% | 5.9% | 20.7% |
| SIS | 10.4% | 16.9% | 20.6% |
| Styrene | 3.8% | 3.8% | 8.7% |
| **AVERAGE** | **10.6%** | **14.8%** | **20.8%** |

## 2. Engine CV Backtest (22 products via forecast_accuracy_log)
Top 3: Raffinate C5 (202000037) 4.4%, C5 Resin (201000018) 6.5%, C5 Resin (201000022) 7.1%
Worst 3: Piperylene 40.0%, DCPD 30.0%, Isoprene 19.6%
Naive comparison: Model beats naive on 3/22 (14%), worse on 19/22 (86%)

## 3. Per-Model Breakdown (Cracked C5, 7d)
| Model | MAPE |
|-------|------|
| ARIMA | 4.2% |
| Naive(last) | 4.5% |
| ETS | 5.7% |
| XGBoost(reg) | 9.1% |
| STL | 11.9% |
| **ENSEMBLE** | **9.2%** |

Ensemble is 2.2x worse than best individual model (ARIMA).

## 4. Directional Accuracy
Walk-forward 7d DirAcc = 17.9%. Engine explanation shows n_test=0, accuracy=null for all horizons.

## 5. Realized Accuracy
forecast_accuracy_log.realized_mape/realized_error both null across all products. No feedback loop.

## 6. Root Causes
1. Ensemble degradation: softmax blending dilutes strongest model
2. No regime awareness: same weights regardless of market (flag OFF)
3. Directional blindness: p_rise always 0.5 for short horizons
4. Feature selection absent (flag OFF)
5. No realized feedback: forecasts never compared against actuals
6. STL model drag: worst performer still contributes to ensemble

## 7. Recommended Actions
P0: Enable FORECAST_REGIME_DETECTION_ENABLED, FORECAST_FEATURE_SELECTION_ENABLED; skip STL in ensemble
P1: Implement realized accuracy tracking; enable FORECAST_XGB_TUNING_ENABLED
P2: Directional classifier gate; performance-based ensemble weighting
