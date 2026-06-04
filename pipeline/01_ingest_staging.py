# 01_ingest_staging.py

import glob
import os
import json
import pandas as pd

from db import engine, init_schemas
from config import ADHOC_PATH, ENCODING, STAGING_SCHEMA, MAPPING_FILE
from transforms import transform_thai_month, normalize_dtypes
from validation import validate_schema

init_schemas()

# --------------------
# Load mapping file
# --------------------
mapping = {}
if os.path.exists(MAPPING_FILE):
    with open(MAPPING_FILE, "r") as f:
        mapping = json.load(f)
    print("[MAPPING LOADED]")
    print(mapping)

# --------------------
# Load CSV files
# --------------------
csv_files = [
    f for f in glob.glob(ADHOC_PATH)
    if not f.endswith("file_table_mapping.json")
]

for file in csv_files:
    file_name = os.path.basename(file)

    # 🔥 Use mapping if available
    if file_name in mapping:
        table_name = mapping[file_name]
        print(f"[MAPPED] {file_name} → {table_name}")
    else:
        table_name = file_name.replace(".csv", "").lower()
        print(f"[DEFAULT] {file_name} → {table_name}")

    print(f"\n=== Ingesting {table_name} into STAGING ===")

    df = pd.read_csv(file, encoding=ENCODING)
    df = transform_thai_month(df)
    df = normalize_dtypes(df)

    df.to_sql(
        table_name,
        engine,
        schema=STAGING_SCHEMA,
        if_exists="replace",
        index=False,
    )

    print("[STAGED]")

    if validate_schema(table_name, df):
        print("[VALIDATION PASSED]")
    else:
        print("[VALIDATION FAILED]")