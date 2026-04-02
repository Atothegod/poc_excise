import logging

from pathlib import Path

from fb_scraper_core import fetch_latest_10_posts, save_new_posts_to_parquet, upload_to_cloud



logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)-8s | %(message)s')

logger = logging.getLogger("PipelineRunner")



if __name__ == "__main__":

    TARGET_PAGES = {

        "PatumTourist": "100068138844126",

        "rangsitcitypathumthani": "100064659283170"

    }

    

    BASE_DIR = Path(__file__).parent / "data_lake_staging"

    

    logger.info("===== STARTING DATA PIPELINE =====")

    

    for page_name, page_id in TARGET_PAGES.items():

        logger.info(f"\n▶️ Processing: {page_name}")

        save_dir = BASE_DIR / page_name

        

        raw_data = fetch_latest_10_posts(page_id)

        final_df = save_new_posts_to_parquet(raw_data, save_dir)

        

        if not final_df.empty:

            bucket_data = upload_to_cloud(final_df, page_name)

            

            if not bucket_data.empty:

                logger.info(f"📊 Sample data retrieved from S3 Bucket for {page_name}:\n{bucket_data.head(3).to_string()}")

        else:

            logger.info(f"⏭️ Skip Cloud: ไม่มีข้อมูลใหม่สำหรับ {page_name}")



    logger.info("===== PIPELINE COMPLETED =====")
