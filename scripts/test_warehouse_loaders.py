#!/usr/bin/env python3
"""Test the warehouse Tier 3 loaders against the live database."""
import os
import sys

# Ensure we're in the right environment
sys.path.insert(0, "/app")

from app.services.forecasting.features.warehouse_loaders import (
    WarehouseProductionLoader,
    WarehouseInventoryLoader,
    WarehousePurchasePriceLoader,
    WarehouseUpstreamLoader,
)
from app.services.forecasting.features.exogenous_loaders import (
    OperatingRateLoader,
    InventoryLoader,
    ImportPriceLoader,
)

PRODUCTS = ["isoprene", "piperylene", "dcpd", "cracked_c5", "cracked_c9", "styrene"]


def test_production():
    print("\n" + "=" * 80)
    print("T3.1: WarehouseProductionLoader (production throughput)")
    print("=" * 80)
    for pid in PRODUCTS:
        loader = WarehouseProductionLoader(product_id=pid, lookback_days=365)
        df = loader.load()
        if df.empty:
            print(f"  {pid:<20} EMPTY")
        else:
            print(f"  {pid:<20} {len(df):>4} days  "
                  f"range={df['date'].min().date()} → {df['date'].max().date()}  "
                  f"total_tons={df['production_t'].sum():.1f}")


def test_inventory():
    print("\n" + "=" * 80)
    print("T3.2: WarehouseInventoryLoader (inventory time-series)")
    print("=" * 80)
    for pid in PRODUCTS:
        loader = WarehouseInventoryLoader(product_id=pid, lookback_days=365)
        df = loader.load()
        if df.empty:
            print(f"  {pid:<20} EMPTY")
        else:
            print(f"  {pid:<20} {len(df):>4} days  "
                  f"range={df['date'].min().date()} → {df['date'].max().date()}  "
                  f"latest_tons={df['inventory_t'].iloc[-1]:.1f}")


def test_purchase_price():
    print("\n" + "=" * 80)
    print("T3.3: WarehousePurchasePriceLoader (purchase prices)")
    print("=" * 80)
    for pid in PRODUCTS:
        loader = WarehousePurchasePriceLoader(product_id=pid, lookback_days=365)
        df = loader.load()
        if df.empty:
            print(f"  {pid:<20} EMPTY")
        else:
            daily = loader.load_daily()
            n_import = df["is_import"].sum() if "is_import" in df.columns else 0
            print(f"  {pid:<20} {len(df):>4} txns  "
                  f"daily={len(daily):>3} days  "
                  f"imports={n_import}  "
                  f"price_range={df['purchase_price'].min():.1f}-{df['purchase_price'].max():.1f}")


def test_upstream():
    print("\n" + "=" * 80)
    print("T3.4: WarehouseUpstreamLoader (oilchem market data)")
    print("=" * 80)
    loader = WarehouseUpstreamLoader(lookback_days=365)
    df = loader.load()
    if df.empty:
        print("  EMPTY")
    else:
        print(f"  {len(df)} days  range={df.index.min().date()} → {df.index.max().date()}")
        print(f"  Columns: {list(df.columns)}")
        print(f"  Non-null counts:")
        for col in df.columns:
            print(f"    {col:<25} {df[col].notna().sum():>3}/{len(df)}")
        print(f"  Latest row:")
        print(f"    {df.index[-1].date()}: {df.iloc[-1].to_dict()}")


def test_fallback():
    print("\n" + "=" * 80)
    print("FALLBACK: OperatingRateLoader (PG → warehouse fallback)")
    print("=" * 80)
    for pid in PRODUCTS:
        loader = OperatingRateLoader(product_id=pid, lookback_days=365)
        df = loader.load()
        if df.empty:
            print(f"  {pid:<20} EMPTY (both PG and warehouse returned nothing)")
        else:
            print(f"  {pid:<20} {len(df):>4} days  "
                  f"range={df['date'].min().date()} → {df['date'].max().date()}")

    print("\n" + "=" * 80)
    print("FALLBACK: InventoryLoader (PG → warehouse fallback)")
    print("=" * 80)
    for pid in PRODUCTS:
        loader = InventoryLoader(product_id=pid, lookback_days=365)
        df = loader.load()
        if df.empty:
            print(f"  {pid:<20} EMPTY")
        else:
            print(f"  {pid:<20} {len(df):>4} days  "
                  f"range={df['date'].min().date()} → {df['date'].max().date()}")

    print("\n" + "=" * 80)
    print("FALLBACK: ImportPriceLoader (PG → warehouse fallback)")
    print("=" * 80)
    for pid in PRODUCTS:
        loader = ImportPriceLoader(product_id=pid, lookback_days=365)
        df = loader.load()
        if df.empty:
            print(f"  {pid:<20} EMPTY")
        else:
            print(f"  {pid:<20} {len(df):>4} days  "
                  f"range={df['date'].min().date()} → {df['date'].max().date()}")


if __name__ == "__main__":
    test_production()
    test_inventory()
    test_purchase_price()
    test_upstream()
    test_fallback()
    print("\n# Done.")
