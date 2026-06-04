# config.py
import os
from dotenv import load_dotenv
load_dotenv()

DB_URL = (
    f"postgresql+psycopg2://{os.getenv('POSTGRES_USER')}:"
    f"{os.getenv('POSTGRES_PASSWORD')}@db:5432/"
    f"{os.getenv('LLM_DB')}"
)

STAGING_SCHEMA = "staging"
PROD_SCHEMA = "production"

ADHOC_PATH = "../ad_hoc/*.csv"
ENCODING = "cp874"
MAPPING_FILE = "../ad_hoc/file_table_mapping.json"
