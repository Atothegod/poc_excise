import os
import re
import duckdb, s3fs
from dotenv import load_dotenv

load_dotenv()

endpoint_url = os.environ.get("GCS_ENDPOINT_URL")
key = os.environ.get("GCS_ACCESS_KEY")
secret = os.environ.get("GCS_SECRET_KEY")

def get_fs():
    endpoint = re.sub(r"https?://", "", endpoint_url).rstrip("/")
    
    return s3fs.S3FileSystem(
        key=key,
        secret=secret,
        client_kwargs={
            "endpoint_url": f"https://{endpoint}"
        },
        use_ssl=False
    )

def get_duck_con():
    fs = get_fs()
    
    con = duckdb.connect()
    con.register_filesystem(fs)
    return con
