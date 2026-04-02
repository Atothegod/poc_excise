import os
import duckdb
from dotenv import load_dotenv

load_dotenv()


def _load_credentials() -> tuple[str, str, str]:
    """โหลดและตรวจสอบ Credentials จาก Environment Variables"""
    endpoint = os.environ.get("GCS_ENDPOINT_URL", "")
    key = os.environ.get("GCS_ACCESS_KEY")
    secret = os.environ.get("GCS_SECRET_KEY")

    if not all([endpoint, key, secret]):
        raise ValueError("❌ ข้อมูล Credentials ไม่ครบ กรุณาตรวจสอบไฟล์ .env")

    return endpoint, key, secret


def get_fs() -> duckdb.DuckDBPyConnection:
    """
    สร้าง DuckDB Connection ที่เชื่อมต่อกับ GCS
    ผ่าน S3 Compatibility Mode
    """
    raw_endpoint, key, secret = _load_credentials()

    # ตัด https:// / http:// ออก เพราะ DuckDB ไม่ต้องการ scheme
    clean_endpoint = raw_endpoint.replace("https://", "").replace("http://", "").rstrip("/")

    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute(f"""
        CREATE OR REPLACE SECRET shared_gcs_secret (
            TYPE S3,
            KEY_ID '{key}',
            SECRET '{secret}',
            ENDPOINT '{clean_endpoint}',
            URL_STYLE 'path'
        );
    """)

    return con


def get_duck_con() -> duckdb.DuckDBPyConnection:
    """ 
    สร้าง DuckDB Connection ที่เชื่อมต่อกับ GCS
    ผ่าน Native GCS Secret (TYPE GCS)
    """
    _, key, secret = _load_credentials()

    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    
    # ใช้ TYPE S3 อย่างเดียว ไม่ต้องมี TYPE GCS
    con.execute(f"""
        CREATE OR REPLACE SECRET gcs_secret (
            TYPE S3,
            KEY_ID '{key}',
            SECRET '{secret}',
            REGION 'auto',
            ENDPOINT 'storage.googleapis.com',
            URL_STYLE 'path',
            USE_SSL true
        );
    """)

    return con