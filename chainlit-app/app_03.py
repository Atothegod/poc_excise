import os
import re
import asyncio
import pandas as pd
import chainlit as cl
from typing import Tuple, Optional
from dotenv import load_dotenv
from chainlit.input_widget import Select

load_dotenv()

import litellm
litellm.ssl_verify = False

from llama_index.llms.litellm import LiteLLM
from llama_index.core import Settings, SQLDatabase
from llama_index.core.query_engine import NLSQLTableQueryEngine
from llama_index.core.prompts import PromptTemplate
from sqlalchemy import create_engine, text, Engine

llm = LiteLLM(
    model="openai/gpt-oss:20b",
    api_base=os.getenv("LLM_URL"),
    api_key=os.getenv("API_KEY"),
    temperature=0, 
    request_timeout=60,
)

Settings.llm = llm
Settings.embed_model = None 







def get_engine(schema_name: str) -> Engine:
    """สร้าง SQLAlchemy Engine พร้อมการตั้งค่า Connection Pool ที่มีประสิทธิภาพ"""
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

from config import TEXT2SQL_PROMPT

def validate_sql(sql: str) -> Tuple[bool, str]:
    """ตรวจสอบความปลอดภัยของคำสั่ง SQL ก่อนประมวลผล"""
    clean_sql = sql.lower().strip()
    
    danger_keywords = ["drop", "delete", "update", "insert", "truncate", "alter", "grant", "revoke", "create"]
    for word in danger_keywords:
        if re.search(rf'\b{word}\b', clean_sql):
            return False, f"พบคำสั่งที่อันตราย: {word.upper()}"
            
    if "information_schema" in clean_sql or "pg_catalog" in clean_sql:
        return False, "ไม่อนุญาตให้เข้าถึง System Catalog"
            
    return True, ""

def clean_sql_output(sql: str) -> str:
    """ทำความสะอาด SQL String จาก Markdown หรือส่วนเกินที่ LLM อาจแถมมา"""
    sql = re.sub(r"```sql\n?|```", "", sql, flags=re.IGNORECASE)
    return sql.strip().rstrip(";")

def build_query_engine(schema_name: str):
    engine = get_engine(schema_name)
    
    get_tables_query = text("""
        SELECT table_name FROM information_schema.tables 
        WHERE table_schema = :schema AND table_type = 'BASE TABLE'
    """)
    
    with engine.connect() as conn:
        db_name = conn.execute(text("SELECT current_database()")).fetchone()
        print(f"\n[DEBUG] Connected DB = {db_name}")

        sp = conn.execute(text("SHOW search_path")).fetchone()
        print(f"\n[DEBUG] search_path = {sp}\n")

        result = conn.execute(get_tables_query, {"schema": schema_name})
        tables = [row[0] for row in result]
        print(f"\n[DEBUG] Tables in schema '{schema_name}': {tables}\n")

    if not tables:
        raise ValueError(f"ไม่พบตารางใน Schema: {schema_name}")

    sql_database = SQLDatabase(engine=engine, schema=schema_name, include_tables=tables)
    
    return NLSQLTableQueryEngine(
        sql_database=sql_database,
        text_to_sql_prompt=TEXT2SQL_PROMPT,
        synthesize_response=False,
    )







@cl.on_chat_start
async def on_chat_start():
    settings = await cl.ChatSettings(
        [
            Select(
                id="schema",
                label="📁 เลือกแหล่งข้อมูล (Schema)",
                values=["public", "production"],
                initial_index=0,
            )
        ]
    ).send()

    schema = settings["schema"] if settings else "public"
    
    try:
        q_engine = build_query_engine(schema)
        cl.user_session.set("q_engine", q_engine)
        cl.user_session.set("current_schema", schema)

        await cl.Message(
            content=f"🚀 **ระบบพร้อมทำงานแล้ว!**\n\nเชื่อมต่อกับ Schema: `{schema}`\n\nสามารถพิมพ์ถามข้อมูลเป็นภาษาไทยได้เลยครับ เช่น 'ขอยอดขายแยกตามกลุ่มสินค้าเดือนที่แล้ว'"
        ).send()
    except Exception as e:
        await cl.Message(content=f"❌ การเชื่อมต่อผิดพลาด: {str(e)}").send()



@cl.on_settings_update
async def on_settings_update(settings):
    schema = settings["schema"]
    print(f"[DEBUG] Switching to schema: {schema}")
    msg = cl.Message(content=f"🔄 กำลังย้ายไปยัง Schema: `{schema}`...")
    await msg.send()

    try:
        q_engine = build_query_engine(schema)
        cl.user_session.set("q_engine", q_engine)
        cl.user_session.set("current_schema", schema)
        msg.content = f"✅ ย้ายไปยัง Schema: `{schema}` เรียบร้อยแล้ว!"
        await msg.update()
    except Exception as e:
        msg.content = f"❌ เปลี่ยน Schema ไม่สำเร็จ: {str(e)}"
        await msg.update()



@cl.on_message
async def on_message(message: cl.Message):
    q_engine = cl.user_session.get("q_engine")
    
    if not q_engine:
        await cl.Message(content="⚠️ กรุณารอสักครู่ ระบบกำลังเริ่มต้น...").send()
        return

    processing_msg = cl.Message(content="🔍 กำลังประมวลผลคำถามของคุณ...")
    await processing_msg.send()

    try:
        response = await asyncio.wait_for(
            asyncio.to_thread(q_engine.query, message.content),
            timeout=45
        )

        raw_sql = response.metadata.get("sql_query", "")
        sql = clean_sql_output(raw_sql)

        print(f"\n--- [LOG] Generated SQL ---\n{sql}\n-------------------------\n")

        is_safe, err = validate_sql(sql)
        if not is_safe:
            processing_msg.content = f"🛡️ **Security Alert:** {err}\n```sql\n{sql}\n```"
            await processing_msg.update()
            return

        data = response.metadata.get("result", [])
        columns = response.metadata.get("col_keys", [])

        if data and columns:
            df = pd.DataFrame(data, columns=columns)

            for col in df.select_dtypes(include=["int64", "float64"]).columns:
                df[col] = df[col].apply(lambda x: f"{x:,.0f}" if pd.notnull(x) else x)

            display_df = df.head(100)
            
            processing_msg.content = f"📊 **ผลลัพธ์จากการวิเคราะห์:**\n```sql\n{sql}\n```"

            processing_msg.elements = [
                cl.Dataframe(
                    data=display_df, 
                    name="Query Result", 
                    display="inline"
                )
            ]
            await processing_msg.update()
        else:
            processing_msg.content = f"✅ รันคำสั่งสำเร็จ แต่ไม่พบข้อมูลที่ตรงเงื่อนไข\n```sql\n{sql}\n```"
            await processing_msg.update()

    except asyncio.TimeoutError:
        processing_msg.content = "⚠️ ประมวลผลนานเกินไป กรุณาลองใช้คำถามที่เฉพาะเจาะจงมากขึ้น"
        await processing_msg.update()
    except Exception as e:
        err_msg = str(e)
        processing_msg.content = f"❌ **เกิดข้อผิดพลาด:**\n`{err_msg}`"
        await processing_msg.update()