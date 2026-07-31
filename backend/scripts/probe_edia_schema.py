"""Quick EDIA schema probe — see what data the tables actually contain."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ['DATABASE_URL'] = 'sqlite:///:memory:'

from sqlalchemy import text
from app.core.mysql_db import get_mysql_engine

eng = get_mysql_engine()
if not eng:
    print('EDIA not configured')
    sys.exit(1)

with eng.connect() as conn:
    # Show columns + sample rows from 裂解C5
    print('=== 裂解C5 columns ===')
    result = conn.execute(text("SELECT * FROM `裂解C5` LIMIT 3"))
    cols = list(result.keys())
    print(f'Columns: {cols}')
    for row in result:
        print(f'  Row: {dict(zip(cols, row))}')

    print('\n=== 裂解C5 distinct product names ===')
    result = conn.execute(text("SELECT DISTINCT `产品名material_name` FROM `裂解C5` LIMIT 10"))
    for row in result:
        print(f'  {row[0]}')

    print('\n=== 裂解C5 distinct supplier names ===')
    result = conn.execute(text("SELECT DISTINCT `厂商名supplier_name` FROM `裂解C5` LIMIT 10"))
    for row in result:
        print(f'  {row[0]}')

    print('\n=== SIS table sample ===')
    result = conn.execute(text("SELECT * FROM `SIS` LIMIT 3"))
    cols = list(result.keys())
    print(f'Columns: {cols}')
    for row in result:
        print(f'  Row: {dict(zip(cols, row))}')
