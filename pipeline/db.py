# db.py

from sqlalchemy import create_engine, text
from config import DB_URL, STAGING_SCHEMA, PROD_SCHEMA

engine = create_engine(DB_URL)

def init_schemas():
    with engine.begin() as conn:
        conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {STAGING_SCHEMA}"))
        conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {PROD_SCHEMA}"))