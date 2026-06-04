import pandas as pd
import os
from sqlalchemy import create_engine, inspect
from dotenv import load_dotenv
load_dotenv()
# ---------- Engines ----------
sqlite_engine = create_engine("sqlite:///data/tax_star_schema2.db")

POSTGRES_USER = os.getenv("POSTGRES_USER")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD")

pg_engine = create_engine(
    f"postgresql+psycopg2://{os.getenv('POSTGRES_USER')}:"
    f"{os.getenv('POSTGRES_PASSWORD')}@db:5432/{os.getenv('LLM_DB')}"
)

# ---------- get tables (SQLAlchemy 2.x way) ----------
inspector = inspect(sqlite_engine)
tables = inspector.get_table_names()

print("Tables found:", tables)

# ---------- migrate ----------
for table in tables:
    print(f"Migrating {table}...")
    df = pd.read_sql_table(table, sqlite_engine)
    df.to_sql(
        table,
        pg_engine,
        if_exists="replace",
        index=False
    )

print("✅ Migration Complete")

