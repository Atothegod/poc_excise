import requests

import pandas as pd

import os

import sys

import logging

from pathlib import Path

from typing import List, Dict, Any, Set



# --- 🌟 โมดูลเชื่อมต่อ Bucket ---

sys.path.append('..')

try:

    from utils.bucket_utils import get_fs 

except ImportError:

    logging.getLogger("ScraperCore").warning("ไม่พบโมดูล utils.bucket_utils (ทำงานแบบ Local/Offline Mode)")



# --- LOGGING CONFIG ---

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)-8s | %(message)s')

logger = logging.getLogger("ScraperCore")



# --- CONFIGURATION ---

API_KEY = os.getenv("RAPIDAPI_KEY", "5a74f09b58mshcc53e8477cbeb6dp1c0595jsncee572e27ef4")

API_HOST = "facebook-scraper3.p.rapidapi.com"

BASE_URL = f"https://{API_HOST}/page/posts"

HEADERS = {"x-rapidapi-key": API_KEY, "x-rapidapi-host": API_HOST, "Content-Type": "application/json"}



SCHEMA_COLS = ['id', 'post_date', 'post_content']



def fetch_latest_10_posts(page_id: str) -> List[Dict[str, Any]]:

    """[Extract] ดึงข้อมูลจาก API"""

    try:

        response = requests.get(BASE_URL, headers=HEADERS, params={"page_id": page_id}, timeout=20)

        response.raise_for_status()

        data = response.json()

        all_posts = data.get('results') or data.get('data') or (data if isinstance(data, list) else [])

        return all_posts[:10]

    except Exception as e:

        logger.error(f"API Error ({page_id}): {e}")

        return []



def get_existing_post_ids(folder_path: Path) -> Set[str]:

    """[Helper] ดึง ID จาก Local เพื่อเช็คซ้ำ"""

    existing_ids = set()

    if not folder_path.exists(): 

        return existing_ids

    

    for file in folder_path.glob("*.parquet"):

        try:

            temp_df = pd.read_parquet(file, columns=['id'])

            existing_ids.update(temp_df['id'].astype(str).tolist())

        except Exception: 

            pass

    return existing_ids



def save_new_posts_to_parquet(new_posts: List[Dict[str, Any]], folder_path: Path) -> pd.DataFrame:

    """[Transform & Load Local] แปลงข้อมูล, ปรับเวลาไทย, และเซฟเป็น Staging"""

    if not new_posts: 

        return pd.DataFrame(columns=SCHEMA_COLS)



    # 1. Extraction

    processed_data = [{'id': str(p.get('id') or p.get('post_id') or ""), 

                       'post_date': p.get('created_time') or p.get('time') or p.get('timestamp'), 

                       'post_content': p.get('message') or p.get('text') or ""} 

                      for p in new_posts if (p.get('id') or p.get('post_id'))]

            

    df_new = pd.DataFrame(processed_data)

    

    # 2. Deduplication

    df_filtered = df_new[~df_new['id'].isin(get_existing_post_ids(folder_path))].copy()



    if df_filtered.empty: 

        return pd.DataFrame(columns=SCHEMA_COLS)



    # 3. 🌟 Time Standardization (แปลงเป็นเวลาไทยอย่างปลอดภัย)

    if not df_filtered['post_date'].isna().all():

        # สร้าง Series เป็น UTC ก่อน

        dt_series = pd.to_datetime(df_filtered['post_date'], errors='coerce', utc=True)

        is_numeric = df_filtered['post_date'].astype(str).str.replace('.', '', 1).str.isdigit()

        

        if is_numeric.any():

            # แปลงเฉพาะตัวเลข แล้วใช้ .update() เพื่อความปลอดภัยจาก Warning

            unix_dates = pd.to_datetime(df_filtered.loc[is_numeric, 'post_date'].astype(float), unit='s', utc=True)

            dt_series.update(unix_dates)

            

        # แปลงเป็นเวลาไทย (Asia/Bangkok)

        df_filtered['post_date'] = dt_series.dt.tz_convert('Asia/Bangkok')



    final_df = df_filtered.sort_values(by='post_date', ascending=False)



    # 4. Local Save (🌟 บังคับใช้เวลาประเทศไทยในการตั้งชื่อไฟล์เสมอ)

    folder_path.mkdir(parents=True, exist_ok=True)

    bkk_now = pd.Timestamp.now('Asia/Bangkok')

    filename = folder_path / f"fb_posts_{bkk_now.strftime('%Y-%m-%d_%H%M%S')}.parquet"

    

    final_df.to_parquet(filename, index=False, engine='pyarrow')

    logger.info(f"💾 Saved Local: {len(final_df)} posts -> {filename.name}")

    

    return final_df



def upload_to_cloud(df: pd.DataFrame, page_name: str) -> pd.DataFrame:

    """[Load Cloud] โยนขึ้น Bucket ผ่าน DuckDB พร้อมระบบ Clear Memory"""

    if df.empty: 

        return pd.DataFrame(columns=SCHEMA_COLS)

        

    con = None

    try:

        con = get_fs() 

        # 🌟 บังคับใช้เวลาประเทศไทยในการสร้าง Partition (ปี/เดือน)

        bkk_now = pd.Timestamp.now('Asia/Bangkok')

        bucket_path = f"s3://pea-oms/facebook_data/page={page_name}/year={bkk_now.year}/month={bkk_now.month:02d}/fb_{bkk_now.strftime('%Y%m%d_%H%M%S')}.parquet"

        

        con.register('df_upload_view', df)

        logger.info(f"☁️ Uploading {len(df)} records to Bucket...")

        

        con.execute(f"""

            COPY (SELECT * FROM df_upload_view) 

            TO '{bucket_path}' (FORMAT parquet)

        """)

        logger.info(f"✅ [SUCCESS] Uploaded {len(df)} records to {bucket_path}")

        

        # Verification Check

        verify_df = con.execute(f"SELECT * FROM read_parquet('{bucket_path}') LIMIT 3").df()

        if not verify_df.empty:

            logger.info("✅ Cloud Verification Success! (Tested reading from Bucket)")

            return verify_df 

        else:

            logger.warning("⚠️ Upload succeeded but read verification returned empty data.")

            return pd.DataFrame(columns=SCHEMA_COLS)

            

    except Exception as e:

        logger.error(f"❌ [CRITICAL] Cloud Upload Failed for {page_name}: {e}")

        return pd.DataFrame(columns=SCHEMA_COLS)

        

    finally:

        if con:

            try:

                con.unregister('df_upload_view')

                con.close()

            except Exception:

                pass