import os
import re
import asyncio
import pandas as pd
import chainlit as cl
from typing import Tuple
from dotenv import load_dotenv
from chainlit.input_widget import Select
from sqlalchemy import create_engine, text, Engine

from llama_index.llms.litellm import LiteLLM
from llama_index.core import Settings, SQLDatabase
from llama_index.core.query_engine import NLSQLTableQueryEngine

from config import SCHEMA_PROMPTS

# =========================
# INIT
# =========================

load_dotenv()

import litellm
litellm.ssl_verify = False

llm = LiteLLM(
    model="openai/gpt-oss:20b",
    api_base=os.getenv("LLM_URL"),
    api_key=os.getenv("API_KEY"),
    temperature=0,
    request_timeout=60,
)

Settings.llm = llm
Settings.embed_model = None

def load_metadata() -> str:
    """
    โหลด Column Metadata จาก shared/*.json
    แล้วแปลงเป็นข้อความเพื่อ inject เข้า Prompt
    """

    import json

    meta_dir = "shared"

    if not os.path.exists(meta_dir):
        return "No metadata."

    metadata_blocks = []

    for f in os.listdir(meta_dir):
        if f.startswith("column_metadata_") and f.endswith(".json"):

            file_path = os.path.join(meta_dir, f)

            try:
                with open(file_path, "r", encoding="utf-8") as file:
                    data = json.load(file)

                block = f"\nFile: {data.get('file_name')}\n"

                for col in data.get("columns", []):
                    block += f"- {col.get('column_name')}: {col.get('description')}\n"

                metadata_blocks.append(block)

            except Exception as e:
                print(f"Metadata load error in {f}: {e}")

    if not metadata_blocks:
        return "No column metadata provided."

    return "\n".join(metadata_blocks)


# =========================
# DB ENGINE
# =========================

def get_engine(schema_name: str) -> Engine:
    return create_engine(
        f"postgresql+psycopg2://{os.getenv('POSTGRES_USER')}:{os.getenv('POSTGRES_PASSWORD')}@db:5432/{os.getenv('LLM_DB')}",
        connect_args={
            "options": f"-csearch_path={schema_name}",
            "connect_timeout": 10
        },
        pool_size=10,
        max_overflow=20,
        pool_pre_ping=True,
        future=True
    )


# =========================
# SECURITY
# =========================

def validate_sql(sql: str) -> Tuple[bool, str]:
    clean_sql = sql.lower().strip()

    danger_keywords = [
        "drop", "delete", "update", "insert",
        "truncate", "alter", "grant", "revoke", "create"
    ]

    for word in danger_keywords:
        if re.search(rf'\b{word}\b', clean_sql):
            return False, f"พบคำสั่งที่อันตราย: {word.upper()}"

    if "information_schema" in clean_sql or "pg_catalog" in clean_sql:
        return False, "ไม่อนุญาตให้เข้าถึง System Catalog"

    return True, ""


def clean_sql_output(sql: str) -> str:
    sql = re.sub(r"```sql\n?|```", "", sql, flags=re.IGNORECASE)
    return sql.strip().rstrip(";")


# =========================
# BUILD QUERY ENGINE (Dynamic Metadata Injection)
# =========================

def build_query_engine(schema_name: str):

    engine = get_engine(schema_name)

    with engine.connect() as conn:
        result = conn.execute(text("""
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = :schema
            AND table_type = 'BASE TABLE'
        """), {"schema": schema_name})

        tables = [row[0] for row in result]

    if not tables:
        raise ValueError(f"ไม่พบตารางใน Schema: {schema_name}")

    sql_database = SQLDatabase(
        engine=engine,
        schema=schema_name,
        include_tables=tables
    )

    selected_prompt = SCHEMA_PROMPTS.get(
        schema_name,
        SCHEMA_PROMPTS["public"]
    )

    metadata_text = load_metadata()
    print("\n===== METADATA LOADED =====")
    print(metadata_text)
    print("===========================\n")
    formatted_prompt = selected_prompt.partial_format(
        metadata=metadata_text
    )

    try:
        test_prompt = formatted_prompt.format(
            schema="DEBUG_SCHEMA",
            query_str="DEBUG_QUERY"
        )
        print("\n===== FINAL PROMPT SAMPLE =====")
        print(test_prompt[:1500])
        print("================================\n")
    except Exception as e:
        print("PROMPT DEBUG ERROR:", e)

    q_engine = NLSQLTableQueryEngine(
        sql_database=sql_database,
        text_to_sql_prompt=formatted_prompt,
        synthesize_response=False,
    )

    return q_engine, engine


# =========================
# CHAT START
# =========================

@cl.on_chat_start
async def on_chat_start():

    settings = await cl.ChatSettings(
        [
            Select(
                id="schema",
                label="📁 เลือกแหล่งข้อมูล (Schema)",
                values=["public", "production"],
                initial_index=1,
            )
        ]
    ).send()

    schema = settings["schema"] if settings else "public"

    cl.user_session.set("current_schema", schema)

# =========================
# SETTINGS UPDATE
# =========================

@cl.on_settings_update
async def on_settings_update(settings):
    schema = settings["schema"]
    cl.user_session.set("current_schema", schema)

    await cl.Message(
        content=f"✅ เปลี่ยน Schema เป็น `{schema}` สำเร็จ"
    ).send()


# =========================
# MESSAGE HANDLER (REBUILD ENGINE EVERY QUERY)
# =========================

@cl.on_message
async def on_message(message: cl.Message):

    schema = cl.user_session.get("current_schema", "public")

    processing_msg = cl.Message(content="🔍 กำลังวิเคราะห์...")
    await processing_msg.send()

    try:

        # 🔁 Rebuild Engine ทุกครั้ง → metadata dynamic 100%
        q_engine, db_engine = build_query_engine(schema)

        clean_query = re.sub(r'\s+', ' ', message.content).strip()

        response = await asyncio.wait_for(
            asyncio.to_thread(q_engine.query, clean_query),
            timeout=45
        )

        raw_sql = response.metadata.get("sql_query", "")
        sql = clean_sql_output(raw_sql)

        is_safe, err = validate_sql(sql)
        if not is_safe:
            processing_msg.content = f"🛡️ Security Alert: {err}\n```sql\n{sql}\n```"
            await processing_msg.update()
            return

        def fetch_data(engine, query):
            with engine.connect() as conn:
                return pd.read_sql(text(query), conn)

        def format_numeric_columns(df: pd.DataFrame) -> pd.DataFrame:
            df_display = df.copy()

            for col in df_display.columns:
                if pd.api.types.is_numeric_dtype(df_display[col]):
                    df_display[col] = df_display[col].apply(
                        lambda x: f"{x:,.0f}" if pd.notnull(x) else x
                    )

            return df_display

        df = await asyncio.to_thread(fetch_data, db_engine, sql)

        if not df.empty:
            df_display = format_numeric_columns(df)

            processing_msg.content = f"📊 ผลลัพธ์:\n```sql\n{sql}\n```"
            processing_msg.elements = [
                cl.Dataframe(data=df_display, name="Result", display="inline")
            ]
        else:
            processing_msg.content = f"✅ ไม่พบข้อมูล\n```sql\n{sql}\n```"

        await processing_msg.update()

    except asyncio.TimeoutError:
        processing_msg.content = "⚠️ ประมวลผลนานเกินไป"
        await processing_msg.update()

    except Exception as e:
        processing_msg.content = f"❌ Error: {str(e)}"
        await processing_msg.update()