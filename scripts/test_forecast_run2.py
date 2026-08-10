#!/usr/bin/env python3
"""Run forecasts for all ecisco.* products and print results + metrics."""
import logging, time, json, sys
logging.basicConfig(level=logging.INFO, format="%(name)s | %(message)s")
for n in ["sqlalchemy","urllib3","chromadb","sentence_transformers","httpx","httpcore"]:
    logging.getLogger(n).setLevel(logging.WARNING)
sys.path.insert(0, "/app")

from app.database import SessionLocal
from app.models.forecasting import ForecastTarget
from app.services.forecasting.engine import ForecastEngine

PRODUCTS = [
    # Updated to ERP primary (should now have 400+ rows → XGBoost activates)
    "ecisco.cracked_c5",
    "ecisco.isoprene",
    "ecisco.piperylene",
    "ecisco.dcpd",
    "ecisco.blowing_agent",
    # Updated extra_sources (marginal ERP data added)
    "ecisco.cracked_c9",
    # New ERP product families (dynamic SKU discovery)
    "ecisco.ethylene_carbon_black.202000022",
    "ecisco.industrial_hexane.202000009",
    "ecisco.mixed_trimethylbenzene.202000014",
    # Already working with XGBoost (baseline reference)
    "ecisco.c5_resin.201000001",
]

db = SessionLocal()
results = []

for pk in PRODUCTS:
    t = db.query(ForecastTarget).filter(
        ForecastTarget.product_key == pk,
        ForecastTarget.is_deleted == False,
    ).first()
    if not t:
        print(f"\n{pk}: NOT FOUND")
        continue

    table = t.datasource.get("table", "?")
    print(f"\n{'='*80}")
    print(f"FORECAST: {pk}  (source: {table})")
    print(f"{'='*80}")

    eng = ForecastEngine(db)
    t0 = time.time()
    try:
        r = eng.compute_target_anchored(t.id, horizons=[7, 14, 30])
    except Exception as exc:
        print(f"  ERROR: {type(exc).__name__}: {str(exc)[:200]}")
        results.append({"product": pk, "error": str(exc)[:100]})
        continue
    elapsed = time.time() - t0

    if r is None:
        print(f"  Result=None")
        continue

    run = r.get("run")
    sample = r.get("sample_size", 0)
    print(f"  Time: {elapsed:.1f}s  Sample: {sample}")

    if run is None:
        print(f"  Run=None (compute failed)")
        results.append({"product": pk, "sample": sample, "run": None, "time": elapsed})
        continue

    # Results: {"7d": {"base": x, "bull": y, "bear": z}, ...}
    fc = run.results or {}
    print(f"  Confidence: {run.confidence}")
    print(f"  Below naive: {run.below_naive_baseline}")
    print(f"  Exog features used: {run.exog_features_used}")
    print(f"  Exog degraded: {run.exog_degraded}")

    print(f"\n  Forecast values:")
    for h in [7, 14, 30]:
        v = fc.get(str(h), fc.get(h, {}))
        if isinstance(v, dict):
            print(f"    h={h:>2}d: base={v.get('base','?')}  bull={v.get('bull','?')}  bear={v.get('bear','?')}")
        else:
            print(f"    h={h:>2}d: {v}")

    # Explanation
    expl = run.explanation or {}
    if expl:
        print(f"\n  Explanation keys: {list(expl.keys())}")
        # Check for signals (warehouse-derived)
        for sig_key in ["signals", "demand_signal", "supply_demand", "external_signals"]:
            sig = expl.get(sig_key)
            if sig:
                print(f"  {sig_key}: {json.dumps(sig, ensure_ascii=False, default=str)[:400]}")

    # Model detail
    md = run.model_detail or {}
    if md:
        models_run = md.get("models_run", [])
        weights = md.get("weights", {})
        failed = md.get("failed", [])
        print(f"\n  Models run: {models_run}")
        if weights:
            print(f"  Weights: {json.dumps(weights, default=str)[:200]}")
        if failed:
            print(f"  Failed: {failed}")

    results.append({
        "product": pk,
        "sample": sample,
        "time": elapsed,
        "confidence": run.confidence,
        "below_naive": run.below_naive_baseline,
        "exog_used": run.exog_features_used,
        "fc_7d": fc.get("7", {}).get("base") if isinstance(fc.get("7"), dict) else None,
        "fc_14d": fc.get("14", {}).get("base") if isinstance(fc.get("14"), dict) else None,
        "fc_30d": fc.get("30", {}).get("base") if isinstance(fc.get("30"), dict) else None,
    })

    db.commit()

# Summary
print(f"\n\n{'='*80}")
print("SUMMARY")
print(f"{'='*80}")
print(f"\n{'Product':<25} {'Time':>6} {'Sample':>6} {'Conf':>8} {'BelowNaive':>11} {'7d':>10} {'14d':>10} {'30d':>10}  ExogUsed")
print("-" * 110)
for r in results:
    pk = r.get("product", "?")
    t = f"{r.get('time', 0):.0f}s"
    ss = str(r.get("sample", 0))
    conf = str(r.get("confidence", "?"))[:8]
    bn = str(r.get("below_naive", "?"))[:11]
    f7 = str(r.get("fc_7d", "?"))[:10] if r.get("fc_7d") else "-"
    f14 = str(r.get("fc_14d", "?"))[:10] if r.get("fc_14d") else "-"
    f30 = str(r.get("fc_30d", "?"))[:10] if r.get("fc_30d") else "-"
    exog = str(r.get("exog_used", []))[:30]
    print(f"{pk:<25} {t:>6} {ss:>6} {conf:>8} {bn:>11} {f7:>10} {f14:>10} {f30:>10}  {exog}")

print(f"\nBaseline (2026-08-06): 7d MAPE=11.4%, 14d=17.3%, 30d=23.5%, DirAcc=24-38%")
db.close()
print("\n# Done.")
