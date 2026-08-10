"""Directional (up/down) classifier — Phase E Task E1.

Optimized for **sign** (P(rise), P(fall)), not point MAPE. Logistic
regression compared via 5-fold time-series CV (expanding window). Edge
claim requires **statistical significance** vs the null hypothesis
``accuracy = 0.5`` (binomial test, alpha=0.01). Without significance we
surface ``"no_edge"`` so the dashboard honestly says "no directional
signal" rather than a fake one.

Why binomial test, not a fixed threshold (e.g. 0.55)?
On short series a fixed threshold either misses real edges (small N) or
overfits noise (large N). A binomial test at alpha=0.01 controls the
false-positive rate regardless of N.

Features: recent returns, momentum (7/14d), volatility, calendar month,
optional exogenous signals (naphtha delta, warehouse production, inventory,
supplier dispersion).
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.stats import binomtest

# Lazy sklearn import — keep import-time light.
_sklearn_warned = False


def _get_lr():
    """Lazy-import sklearn LogisticRegression."""
    global _sklearn_warned
    try:
        from sklearn.linear_model import LogisticRegression
        return LogisticRegression
    except ImportError:
        if not _sklearn_warned:
            import logging
            logging.getLogger(__name__).warning(
                "sklearn not installed — directional classifier will use a "
                "naive fallback (returns 0.5 probability)."
            )
            _sklearn_warned = True
        return None


_MIN_TRAIN = 60
_KFOLDS = 5  # 5-fold time-series CV
_SIGNIFICANCE_ALPHA = 0.01  # p-value threshold for "edge"


def build_features(y: pd.Series, exog: pd.DataFrame | None = None) -> pd.DataFrame:
    """Build feature matrix from a price series.

    Features: ret_lag1, ret_lag3, momentum_7, momentum_14, vol_7,
    calendar month, and optional exogenous columns (naphtha_delta).
    """
    df = pd.DataFrame({"y": y.values})
    df["ret_lag1"] = df["y"].pct_change(1)
    df["ret_lag3"] = df["y"].pct_change(3)
    df["momentum_7"] = df["y"].pct_change(7)
    df["momentum_14"] = df["y"].pct_change(14)
    df["vol_7"] = df["ret_lag1"].rolling(7).std()
    if hasattr(y.index, "dtype") and np.issubdtype(y.index.dtype, np.datetime64):
        df["month"] = pd.Series(y.index).dt.month.values
    elif hasattr(y.index, "month"):
        df["month"] = np.array([getattr(idx, "month", 1) for idx in y.index])
    else:
        df["month"] = 1
    # --- Exogenous signals (merge all columns from exog DataFrame) ---
    if exog is not None:
        _exog_df = exog.copy()
        # If exog has DatetimeIndex, keep first N rows matching price length
        if hasattr(_exog_df.index, "freq") or isinstance(
            _exog_df.index, pd.DatetimeIndex,
        ):
            if len(_exog_df) > len(df):
                _exog_df = _exog_df.iloc[:len(df)]
        else:
            # Default RangeIndex: ensure same length
            if len(_exog_df) > len(df):
                _exog_df = _exog_df.iloc[:len(df)]
        # Align: if exog is shorter, pad with NaN; if longer, trim
        if len(_exog_df) < len(df):
            _exog_df = _exog_df.reindex(range(len(df)))
        for col in _exog_df.columns:
            if col not in df.columns:
                df[col] = _exog_df[col].values
    # Replace inf/-inf (from pct_change on near-zero values) before fillna
    df = df.replace([np.inf, -np.inf], np.nan).fillna(0.0)
    return df


class DirectionalClassifier:
    """Logistic regression classifier for P(rise)."""

    def __init__(self, kind: str = "logistic"):
        self.kind = kind
        LogisticRegression = _get_lr()
        if LogisticRegression is None:
            self.model = None
        elif kind == "logistic":
            self.model = LogisticRegression(max_iter=200)
        else:
            raise ValueError(f"Unknown kind: {kind}")

    def fit(self, X, y_sign):
        if self.model is None:
            return self
        self.model.fit(X, y_sign)
        return self

    def predict_proba(self, X):
        if self.model is None:
            return np.full(len(X), 0.5)
        return self.model.predict_proba(X)[:, 1]


def _cv_eval(y: pd.Series, h: int):
    """5-fold time-series CV. Returns (accuracy, n_test_samples).

    Expanding window: for fold k in {1..K-1}, train on samples [0 : k*N/K],
    test on samples [k*N/K : (k+1)*N/K].
    """
    n = len(y)
    # Reset index to default RangeIndex to avoid alignment issues when
    # filtering feats with a boolean Series from a different index.
    y = y.reset_index(drop=True)
    feats = build_features(y)
    future_sign = (y.shift(-h) > y).astype(int)

    # Drop rows where future_sign is NaN (last h rows have no label)
    valid_mask = future_sign.notna().reset_index(drop=True)
    feats = feats.loc[valid_mask].reset_index(drop=True)
    labels = future_sign.loc[valid_mask].astype(int).reset_index(drop=True)
    n_valid = len(labels)
    if n_valid < _MIN_TRAIN + 30:
        return None, 0

    fold_size = n_valid // _KFOLDS
    if fold_size < 5:
        return None, 0

    n_correct = 0
    n_total = 0
    for k in range(1, _KFOLDS):
        train_end = k * fold_size
        test_end = (k + 1) * fold_size
        X_tr = feats.iloc[:train_end]
        y_tr = labels.iloc[:train_end]
        X_te = feats.iloc[train_end:test_end]
        y_te = labels.iloc[train_end:test_end]

        if len(X_te) < 5 or len(y_tr.dropna()) < 30:
            continue
        try:
            clf = DirectionalClassifier("logistic").fit(X_tr, y_tr)
            preds = (clf.predict_proba(X_te) >= 0.5).astype(int)
            n_correct += int((preds == y_te.values).sum())
            n_total += len(y_te)
        except Exception:
            continue

    if n_total == 0:
        return None, 0
    return float(n_correct / n_total), n_total


def backtest_directional(
    y: pd.Series,
    horizons: tuple[int, ...] = (7,),
) -> dict[str, float]:
    """Walk-forward directional accuracy with significance test.

    Returns a dict with keys:
      - ``logistic``: accuracy in [0,1] if sklearn available
      - ``n_test``: number of test samples used in CV
      - ``p_value``: binomial p-value under null ``p=0.5``
      - ``status``: ``"edge"`` if p_value < alpha (significant edge),
        ``"no_edge"`` otherwise.

    Short-series inputs (< MIN_TRAIN + 30) return ``{"status": "no_edge"}``.
    """
    y = y.dropna()
    n = len(y)
    if n < _MIN_TRAIN + 30:
        return {"status": "no_edge"}

    LogisticRegression = _get_lr()
    if LogisticRegression is None:
        return {"status": "no_edge"}

    h = horizons[0]
    acc, n_test = _cv_eval(y, h)
    if acc is None:
        return {"status": "no_edge"}

    n_correct = int(round(acc * n_test))
    # Binomial test: probability of >= n_correct successes under null p=0.5
    # Use two-sided to detect either direction of edge
    p_value = binomtest(n_correct, n_test, p=0.5, alternative="greater").pvalue

    results: dict[str, float] = {
        "logistic": acc,
        "n_test": float(n_test),
        "p_value": float(p_value),
    }
    if p_value < _SIGNIFICANCE_ALPHA:
        results["status"] = "edge"
    else:
        results["status"] = "no_edge"
    return results