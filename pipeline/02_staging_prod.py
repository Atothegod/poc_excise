# promote_to_production.py

from sqlalchemy import inspect
from validation import promote_replace, promote_append
from db import engine
from config import STAGING_SCHEMA, PROD_SCHEMA

inspector = inspect(engine)
staging_tables = inspector.get_table_names(schema=STAGING_SCHEMA)
prod_tables = inspector.get_table_names(schema=PROD_SCHEMA)

print("Tables in staging:", staging_tables)

for table in staging_tables:
    if table not in prod_tables:
        promote_replace(table)
    else:
        promote_replace(table)

print("=== Promotion Complete ===")
