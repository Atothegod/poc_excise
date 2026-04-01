# run_pipeline.py
from pathlib import Path
from fb_scraper_core import fetch_latest_10_posts, save_new_posts_to_parquet

if __name__ == "__main__":
    # 1. นิยามเพจที่ต้องการดึงทั้งหมด (เป็น Dictionary)
    TARGET_PAGES = {
        "PatumTourist": "100068138844126",
        "rangsitcitypathumthani": "100064659283170"
    }
    
    # 2. วนลูปสั่งทำงานทีละเพจ
    for page_name, page_id in TARGET_PAGES.items():
        # สร้าง/ระบุโฟลเดอร์ของเพจนั้นๆ
        SAVE_DIR = Path.home() / "Desktop" / page_name
        SAVE_DIR.mkdir(parents=True, exist_ok=True)
        
        # เรียกใช้ฟังก์ชันจากคลังเครื่องมือ
        raw_data = fetch_latest_10_posts(page_id)
        final_df = save_new_posts_to_parquet(raw_data, SAVE_DIR)
        
        # เช็คผลลัพธ์
        if not final_df.empty:
            print(final_df.head(2)) # โชว์ตัวอย่าง 2 แถวใน Terminal