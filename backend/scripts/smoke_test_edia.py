"""EDIA smoke test — verifies the real EDIA MySQL connection + all service functions."""
import os, sys

# Add backend/ to sys.path so 'app' is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Use SQLite for the main DB (avoids needing PostgreSQL running)
os.environ['DATABASE_URL'] = 'sqlite:///:memory:'

from app.config import settings
print(f'EDIA_MYSQL_URL: {"SET" if settings.EDIA_MYSQL_URL else "EMPTY"}')
print(f'is_edia_enabled: {settings.is_edia_enabled}')

from app.services.market_dashboard_service import (
    is_edia_enabled, is_edia_reachable_cached,
    get_market_snapshot, get_price_history,
    get_regional_prices, get_news_events,
)

print(f'\nenabled: {is_edia_enabled()}')
print(f'reachable: {is_edia_reachable_cached()}')

if not (is_edia_enabled() and is_edia_reachable_cached()):
    print('\n*** EDIA not reachable — smoke test cannot proceed ***')
    sys.exit(1)

print('\n=== Snapshot (upstream) ===')
snap = get_market_snapshot(group='upstream')
for s in snap:
    print(f'  {s.id}: price={s.price}, change={s.change_pct}%, date={s.date}, note={s.note}')

print('\n=== Snapshot (midstream) ===')
snap = get_market_snapshot(group='midstream')
for s in snap:
    print(f'  {s.id}: price={s.price}, change={s.change_pct}%, date={s.date}')

print('\n=== Snapshot (downstream) ===')
snap = get_market_snapshot(group='downstream')
for s in snap:
    print(f'  {s.id}: price={s.price}, change={s.change_pct}%, date={s.date}')

print('\n=== History (naphtha, 365d) ===')
hist = get_price_history('naphtha', days=365)
print(f'  {len(hist)} points')
for h in hist[-5:]:
    print(f'  {h.date}: {h.price}')

print('\n=== Regional (isoprene) ===')
regional = get_regional_prices('isoprene')
print(f'  regions: {len(regional.regions)}')
for r in regional.regions:
    print(f'    {r.label}: {r.price} ({r.date})')
print(f'  note: {regional.note}')

print('\n=== News (upstream) ===')
news = get_news_events(group='upstream', limit=5)
print(f'  items: {len(news.items)}')
print(f'  note: {news.note}')

print('\n*** SMOKE TEST PASSED ***')
