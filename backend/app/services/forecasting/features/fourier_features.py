"""Fourier features for explicit seasonal decomposition.

Adds sin/cos pairs at multiple frequencies to capture multi-scale
periodic patterns in a more expressive way than raw calendar features.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def compute_fourier_terms(
    y: pd.Series,
    n_harmonics: int = 3,
    target_period: int = 7,
    include_originals: bool = False,
) -> pd.DataFrame:
    """Compute sin/cos Fourier pairs for a given seasonal period.

    Parameters
    ----------
    y : pd.Series
        Time series (used only for index alignment).
    n_harmonics : int
        Number of harmonics (produces 2 × n_harmonics columns).
    target_period : int
        The seasonal period to encode (e.g. 7 for weekly, 365 for annual).
    include_originals : bool
        If True, also include the raw linear position.

    Returns
    -------
    pd.DataFrame
        Columns: fourier_sin_1, fourier_cos_1, …, fourier_sin_{n_harmonics},
        fourier_cos_{n_harmonics}.  Index matches *y*.
    """
    n = len(y)
    t = np.arange(n)
    result = pd.DataFrame(index=y.index)

    for k in range(1, n_harmonics + 1):
        result[f"fourier_sin_{k}"] = np.sin(2 * np.pi * k * t / target_period)
        result[f"fourier_cos_{k}"] = np.cos(2 * np.pi * k * t / target_period)

    if include_originals:
        result["fourier_t"] = t

    return result


def add_fourier_features(
    y: pd.Series,
    output: pd.DataFrame | None = None,
    weekly_harmonics: int = 3,
    annual_harmonics: int = 0,
) -> pd.DataFrame:
    """Add weekly (and optionally annual) Fourier terms to a feature DataFrame.

    Parameters
    ----------
    y : pd.Series
        Target series.
    output : pd.DataFrame or None
        Existing features (columns appended in-place).
    weekly_harmonics : int
        Number of 7-day harmonics.
    annual_harmonics : int
        Number of 365-day harmonics (0 = skip).

    Returns
    -------
    pd.DataFrame
    """
    if output is None:
        output = pd.DataFrame(index=y.index)

    if weekly_harmonics > 0:
        weekly = compute_fourier_terms(y, n_harmonics=weekly_harmonics, target_period=7)
        for col in weekly.columns:
            output[col] = weekly[col]

    if annual_harmonics > 0:
        annual = compute_fourier_terms(y, n_harmonics=annual_harmonics, target_period=365)
        for col in annual.columns:
            output[col] = annual[col]

    return output
