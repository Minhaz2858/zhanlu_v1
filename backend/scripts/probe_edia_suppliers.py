"""Probe all EDIA tables for their supplier names."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ['DATABASE_URL'] = 'sqlite:///:memory:'

from sqlalchemy import text
from app.core.mysql_db import get_mysql_engine

eng = get_mysql_engine()
tables = ['裂解C5', '异戊二烯', '间戊二烯', '双环戊二烯', 'SIS']

with eng.connect() as conn:
    for t in tables:
        print(f'\n=== {t} distinct suppliers ===')
        try:
            result = conn.execute(text(f"SELECT DISTINCT `厂商名supplier_name` FROM `{t}`"))
            for row in result:
                print(f'  {row[0]}')
        except Exception as e:
            print(f'  ERROR: {e}')
