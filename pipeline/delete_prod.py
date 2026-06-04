from sqlalchemy import text, inspect
from db import engine
from config import PROD_SCHEMA

inspector = inspect(engine)
tables = inspector.get_table_names(schema=PROD_SCHEMA)

with engine.begin() as conn:
    for table in tables:
        conn.execute(
            text(f'DROP TABLE IF EXISTS "{PROD_SCHEMA}"."{table}" CASCADE')
        )
        print(f"[DROPPED] {table}")

print("=== Production Cleared ===")