# validation.py

from sqlalchemy import inspect, text
from db import engine
from config import STAGING_SCHEMA, PROD_SCHEMA
from sqlalchemy import text
from sqlalchemy.sql import quoted_name

def validate_schema(table_name, df):
    inspector = inspect(engine)
    prod_tables = inspector.get_table_names(schema=PROD_SCHEMA)

    if table_name not in prod_tables:
        print(f"[NEW TABLE] {table_name}")
        return True

    prod_columns = inspector.get_columns(table_name, schema=PROD_SCHEMA)
    prod_column_names = set([c['name'] for c in prod_columns])
    new_columns = set(df.columns)

    missing_cols = prod_column_names - new_columns
    if missing_cols:
        print(f"[BLOCK] Missing columns: {missing_cols}")
        return False

    extra_cols = new_columns - prod_column_names
    if extra_cols:
        print(f"[ALTER] New columns: {extra_cols}")
        with engine.begin() as conn:
            for col in extra_cols:
                conn.execute(
                    text(f'ALTER TABLE {PROD_SCHEMA}.{table_name} ADD COLUMN "{col}" TEXT')
                )

    return True


def promote_replace(table_name):
    from config import PROD_SCHEMA
    from db import engine
    with engine.begin() as conn:
        safe_table = quoted_name(table_name, quote=True)

        conn.execute(
            text(f'DROP TABLE IF EXISTS "{PROD_SCHEMA}"."{table_name}" CASCADE')
        )

        conn.execute(
            text(
                f'CREATE TABLE "{PROD_SCHEMA}"."{table_name}" AS '
                f'SELECT * FROM staging."{table_name}"'
            )
        )
        print(f"[PROMOTED - REPLACE] {table_name}")


def promote_append(table_name):
    inspector = inspect(engine)
    prod_columns = [c['name'] for c in inspector.get_columns(table_name, schema=PROD_SCHEMA)]
    col_list = ", ".join(f'"{c}"' for c in prod_columns)

    with engine.begin() as conn:
        conn.execute(text(f"""
            INSERT INTO {PROD_SCHEMA}.{table_name} ({col_list})
            SELECT {col_list} FROM {STAGING_SCHEMA}.{table_name}
        """))

    print(f"[PROMOTED - APPEND] {table_name}")