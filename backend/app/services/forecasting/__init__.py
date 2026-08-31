"""Forecasting Engine (generic — serves any bound data source).

This package provides:

* Discovery – auto-detect forecastable time series from any datasource
* Quality  – 6-factor scoring (A/B/C/D grade)
* Models   – 6 statistical models with uniform fit/forecast interface
* Ensemble – per-series weighted blend (softmax weights from MAPE)
* Guard    – naive-baseline honesty gate (the critical check)
* Scenarios – base/bull/bear for 3 horizons with confidence labels
* Backtest – expanding-window holdout evaluation
* Engine   – orchestrator that chains everything and reads/writes Section 1 tables

The engine is pure Python — no LLM inside.  It is called by agent tools
(Section 3) and the nightly automation cron.
"""
