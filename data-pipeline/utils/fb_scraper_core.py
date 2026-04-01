# fb_scraper_core.py
import requests
import pandas as pd
import os
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Dict, Any

# --- 1. CONFIGURATION ---
API_KEY = os.getenv("RAPIDAPI_KEY", "5a74f09b58mshcc53e8477cbeb6dp1c0595jsncee572e27ef4")
API_HOST = "facebook-scraper3.p.rapidapi.com"
BASE_URL = f"https://{API_HOST}/page/posts"

HEADERS = {
    "x-rapidapi-key": API_KEY,
    "x-rapidapi-host": API_HOST,
    "Content-Type": "application/json"
}

# --- 2. EXTRACT FUNCTION ---
def fetch_latest_10_posts(page_id: str) -> List[Dict[str, Any]]:
    querystring = {"page_id": page_id}
    try:
        print(f"⏳ กำลังดึง 10 โพสต์ล่าสุดจาก ID: {page_id}...")
        response = requests.get(BASE_URL, headers=HEADERS, params=querystring, timeout=20)
        response.raise_for_status()
        
        data = response.json()
        all_posts = data.get('results') or data.get('data') or (data if isinstance(data, list) else [])
        return all_posts[:10]
    except Exception as e:
        print(f"❌ Error: {e}")
        return []

# --- 3. TRANSFORM & LOAD FUNCTIONS ---
def get_existing_post_ids(folder_path: Path) -> set:
    existing_ids = set()
    for file in folder_path.glob("*.parquet"):
        try:
            temp_df = pd.read_parquet(file)
            if 'id' in temp_df.columns:
                existing_ids.update(temp_df['id'].astype(str).tolist())
        except Exception:
            pass
    return existing_ids

def save_new_posts_to_parquet(new_posts: List[Dict[str, Any]], folder_path: Path) -> pd.DataFrame:
    if not new_posts:
        print("ℹ️ ไม่มีข้อมูลให้ประมวลผล")
        return pd.DataFrame()

    processed_data = []
    for post in new_posts:
        content = post.get('message') or post.get('text') or post.get('description') or ""
        raw_date = post.get('created_time') or post.get('time') or post.get('timestamp')
        post_id = str(post.get('id') or post.get('post_id') or "")
        
        if post_id:
            processed_data.append({'id': post_id, 'post_date': raw_date, 'post_content': content})
            
    df_new = pd.DataFrame(processed_data)
    
    existing_ids = get_existing_post_ids(folder_path)
    df_filtered = df_new[~df_new['id'].isin(existing_ids)].copy()

    if df_filtered.empty:
        print("✨ ทุกโพสต์ที่ดึงมา มีอยู่ในเครื่องแล้ว (ไม่มีข้อมูลใหม่)")
        return pd.DataFrame()

    if 'post_date' in df_filtered.columns:
        is_timestamp = str(df_filtered['post_date'].dropna().iloc[0]).replace('.', '', 1).isdigit()
        if is_timestamp:
            df_filtered['post_date'] = pd.to_datetime(df_filtered['post_date'].astype(float), unit='s', errors='coerce')
        else:
            df_filtered['post_date'] = pd.to_datetime(df_filtered['post_date'], format='mixed', errors='coerce')

    final_df = df_filtered[['id', 'post_date', 'post_content']].sort_values(by='post_date', ascending=False)

    today_str = datetime.now().strftime("%Y-%m-%d")
    filename = folder_path / f"fb_posts_{today_str}.parquet"
    final_df.to_parquet(filename, index=False, engine='pyarrow')
    
    print(f"🎉 บันทึกข้อมูลใหม่ {len(final_df)} โพสต์ ลงในไฟล์: {filename.name}")
    return final_df