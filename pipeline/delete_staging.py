from sqlalchemy import text, inspect
from db import engine
from config import STAGING_SCHEMA

inspector = inspect(engine)
tables = inspector.get_table_names(schema=STAGING_SCHEMA)

with engine.begin() as conn:
    for table in tables:
        conn.execute(
            text(f'DROP TABLE IF EXISTS "{STAGING_SCHEMA}"."{table}" CASCADE')
        )
        print(f"[DROPPED] {table}")

print("=== Staging Cleared ===")