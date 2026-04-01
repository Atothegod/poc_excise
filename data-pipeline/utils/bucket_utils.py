import os
import duckdb
import pandas as pd
from dotenv import load_dotenv

# 1. โหลด Environment Variables
load_dotenv()
key = os.environ.get("GCS_ACCESS_KEY")
secret = os.environ.get("GCS_SECRET_KEY")
# gcs_connection.py
import os
import duckdb
from dotenv import load_dotenv

def get_fs():
    """
    ฟังก์ชันสำหรับสร้าง DuckDB Connection ที่เชื่อมต่อกับ GCS 
    ผ่าน S3 Compatibility Mode สำหรับให้ทีม Data Engineer ใช้งาน
    """
    # 1. โหลด Environment Variables
    load_dotenv()
    raw_endpoint = os.environ.get("GCS_ENDPOINT_URL", "")
    key = os.environ.get("GCS_ACCESS_KEY")
    secret = os.environ.get("GCS_SECRET_KEY")

    if not all([raw_endpoint, key, secret]):
        raise ValueError("❌ ข้อมูล Credentials ไม่ครบ กรุณาตรวจสอบไฟล์ .env")

    # 2. ทำความสะอาด Endpoint (ตัด https:// ออก)
    clean_endpoint = raw_endpoint.replace("https://", "").replace("http://", "").rstrip("/")

    # 3. สร้าง Connection และโหลด Extension
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")

    # 4. สร้าง Secret แบบ S3 (เพื่อให้ใช้ s3:// ได้)
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
def get_duck_con():
    con = duckdb.connect()
    
    # 2. ติดตั้งและโหลด Extension สำหรับจัดการไฟล์ผ่านเน็ต (ทำแค่ครั้งแรก)
    con.execute("INSTALL httpfs; LOAD httpfs;")
    
    # 3. สร้าง Secret บอก DuckDB ว่านี่คือ Google Cloud (TYPE GCS)
    con.execute(f"""
        CREATE OR REPLACE SECRET gcs_secret (
            TYPE GCS,
            KEY_ID '{key}',
            SECRET '{secret}'
        );
    """)
    return con

#con = fs()
