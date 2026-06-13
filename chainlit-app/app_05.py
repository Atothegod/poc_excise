# app_05.py
import os
import re
import asyncio
import pandas as pd
import chainlit as cl
from typing import Tuple
from chainlit.input_widget import Select
from sqlalchemy import create_engine, text, Engine

# --- AI & LlamaIndex Imports ---
import dspy
from llama_index.core import SQLDatabase
from llama_index.core.query_engine import NLSQLTableQueryEngine

# --- Local Configs ---
from config import SCHEMA_PROMPTS, FALLBACK_MESSAGE
from ai_config import init_ai_models, DataAssistantSignature

# =========================
# ENABLE CHAT HISTORY (UI)
# =========================
import chainlit.data as cl_data
from chainlit.data.sql_alchemy import SQLAlchemyDataLayer

# เปิดใช้งาน SQLite
cl_data._data_layer = SQLAlchemyDataLayer(conninfo="sqlite+aiosqlite:///chainlit_history.db")

# =========================
# 🔐 MOCK AUTHENTICATION (เปิดหน้า Login)
# =========================
@cl.password_auth_callback
async def auth_callback(username: str, password: str):
    # สมมติให้ทุกคนล็อกอินผ่านหมด (แค่กรอกชื่อก็พอ)
    # เพื่อให้ Chainlit นำชื่อ Username ไปสร้างประวัติแชทของคนๆ นั้น
    return cl.User(identifier=username)

# =========================
# INIT AI MODELS
# =========================
init_ai_models()

# =========================
# METADATA & DB ENGINE
# =========================

def load_metadata() -> str:
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
# SECURITY & HELPERS
# =========================

def validate_sql(sql: str) -> Tuple[bool, str]:
    clean_sql = sql.lower().strip()
    danger_keywords = ["drop", "delete", "update", "insert", "truncate", "alter", "grant", "revoke", "create"]
    for word in danger_keywords:
        if re.search(rf'\b{word}\b', clean_sql):
            return False, f"พบคำสั่งที่อันตราย: {word.upper()}"
    if "information_schema" in clean_sql or "pg_catalog" in clean_sql:
        return False, "ไม่อนุญาตให้เข้าถึง System Catalog"
    return True, ""

def clean_sql_output(sql: str) -> str:
    return re.sub(r"```sql\n?|```", "", sql, flags=re.IGNORECASE).strip().rstrip(";")

def is_no_data(df: pd.DataFrame) -> bool:
    if df.empty: return True
    if df.isna().all().all(): return True
    numeric_df = df.select_dtypes(include='number')
    if not numeric_df.empty and (numeric_df == 0).all().all(): return True
    return False

def is_metadata_question(query: str) -> bool:
    q = query.lower()
    strong_keywords = ["table", "ตาราง", "schema", "column", "field", "โครงสร้าง"]
    return any(k in q for k in strong_keywords)

def normalize_sql(sql: str, schema: str) -> str:
    sql = re.sub(rf'{schema}\.{schema}\.', '', sql, flags=re.IGNORECASE)
    sql = re.sub(rf'\b{schema}\.', '', sql, flags=re.IGNORECASE)
    return sql

def get_table_columns(engine, schema: str, table: str):
    query = text("""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = :schema
        AND table_name = :table
    """)
    with engine.connect() as conn:
        result = conn.execute(query, {"schema": schema, "table": table})
        return [row[0] for row in result]

def auto_quote_columns(sql: str, engine, schema: str) -> str:
    match = re.search(r'from\s+"?([\wก-๙]+)"?', sql, re.IGNORECASE)
    if not match: return sql
    table = match.group(1)
    columns = get_table_columns(engine, schema, table)
    for col in columns:
        sql = re.sub(rf'(?<!")\b{col}\b(?!")', f'"{col}"', sql, flags=re.IGNORECASE)
    return sql

def cleanup_double_quotes(sql: str) -> str:
    return re.sub(r'""(\w+)""', r'"\1"', sql)

def fetch_data(engine, query):
    with engine.connect() as conn:
        return pd.read_sql(text(query), conn)

def format_numeric_columns(df: pd.DataFrame) -> pd.DataFrame:
    df_display = df.copy()
    for col in df_display.columns:
        if pd.api.types.is_numeric_dtype(df_display[col]):
            df_display[col] = df_display[col].apply(lambda x: f"{x:,.0f}" if pd.notnull(x) else x)
    return df_display

# =========================
# BUILD QUERY ENGINE
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

    sql_database = SQLDatabase(engine=engine, schema=schema_name, include_tables=tables)
    selected_prompt = SCHEMA_PROMPTS.get(schema_name, SCHEMA_PROMPTS["public"])
    metadata_text = load_metadata()
    
    formatted_prompt = selected_prompt.partial_format(metadata=metadata_text)

    q_engine = NLSQLTableQueryEngine(
        sql_database=sql_database,
        text_to_sql_prompt=formatted_prompt,
        synthesize_response=False,
    )
    return q_engine, engine

# =========================
# CHAT STARTERS (Welcome Screen)
# =========================

@cl.set_starters
async def set_starters():
    return [
        cl.Starter(
            label="📊 ผลรวมภาษีแต่ละปีงบประมาณ",
            message="ขอดูผลรวมภาษีแต่ละปีงบประมาณหน่อย",
            icon="/public/logo_dark.png",
        ),
        cl.Starter(
            label="📋 จำนวนใบอนุญาตแยกตามประเภท",
            message="จำนวนใบอนุญาตแยกตามประเภทสินค้ามีเท่าไหร่บ้าง?",
            icon="/public/logo_dark.png",
        ),
        cl.Starter(
            label="🗂️ โครงสร้างตารางข้อมูล",
            message="ขอดูโครงสร้างตารางข้อมูลหน่อย",
            icon="/public/logo_dark.png",
        ),
        cl.Starter(
            label="📈 เปรียบเทียบรายได้ภาษี",
            message="เปรียบเทียบรายได้ภาษีระหว่างปีงบประมาณล่าสุดให้หน่อย",
            icon="/public/logo_dark.png",
        ),
    ]

# =========================
# CHAT START & SETTINGS
# =========================

@cl.on_chat_start
async def on_chat_start():

    settings = await cl.ChatSettings([
        Select(
            id="schema",
            label="📁 เลือกแหล่งข้อมูล (Schema)",
            values=["public", "production"],
            initial_index=1,
        )
    ]).send()
    schema = settings["schema"] if settings else "public"
    cl.user_session.set("current_schema", schema)

@cl.on_settings_update
async def on_settings_update(settings):
    schema = settings["schema"]
    cl.user_session.set("current_schema", schema)
    await cl.Message(content=f"✅ เปลี่ยน Schema เป็น `{schema}` สำเร็จ").send()

# =========================
# MESSAGE HANDLER (DSPy ReAct Agent)
# =========================

@cl.on_message
async def on_message(message: cl.Message):
    schema = cl.user_session.get("current_schema", "public")
    clean_query = re.sub(r'\s+', ' ', message.content).strip()

    processing_msg = cl.Message(content="กำลังวิเคราะห์และดึงข้อมูล...")
    await processing_msg.send()

    # 1. Intercept Metadata questions first
    if is_metadata_question(clean_query):
        _, db_engine = build_query_engine(schema)
        def get_tables(engine, schema):
            query = text("""
                SELECT table_name FROM information_schema.tables 
                WHERE table_schema = :schema AND table_type = 'BASE TABLE'
            """)
            with engine.connect() as conn:
                return [row[0] for row in conn.execute(query, {"schema": schema})]
        
        tables = get_tables(db_engine, schema)
        if tables:
            table_list = "\n".join([f"- {t}" for t in tables])
            processing_msg.content = f"📁 ตารางใน schema `{schema}`:\n{table_list}"
        else:
            processing_msg.content = "ไม่พบตารางในระบบ"
        await processing_msg.update()
        return

    # Variables to hold tool execution results
    final_df = None
    final_sql = ""

    # 2. Define the dynamic tool for DSPy
    def query_database_tool(database_question: str) -> str:
        """
        Translates a natural language question into an SQL query, executes it on the PostgreSQL database, and returns the data table.
        Use this tool to find factual data to answer the user's questions.
        """
        nonlocal final_df, final_sql
        try:
            q_engine, db_engine = build_query_engine(schema)
            response = q_engine.query(database_question)
            print(q_engine)
            print(response)
            raw_sql = response.metadata.get("sql_query", "")
            sql = clean_sql_output(raw_sql)

            if sql.strip().upper() == "NO_DATA":
                return "The system could not generate a valid SQL query for this question."

            sql = normalize_sql(sql, schema)
            sql = auto_quote_columns(sql, db_engine, schema)
            sql = cleanup_double_quotes(sql)

            final_sql = sql  
            is_safe, err = validate_sql(sql)
            if not is_safe:
                return f"Security Alert: {err}"

            df = fetch_data(db_engine, sql)
            if is_no_data(df):
                return "The query executed successfully, but returned NO DATA."

            df_display = format_numeric_columns(df)
            final_df = df_display 

            # ใช้ .to_csv() แทน .to_markdown() เพื่อป้องกันปัญหา Library tabulate หาย
            csv_data = df_display.head(30).to_csv(index=False)
            return f"✅ SUCCESS: Database query completed. Here is the factual data you must use to answer the user:\n\n{csv_data}"
            
        except Exception as e:
            return f"Error executing query: {str(e)}"

    # 3. Initialize the ReAct Agent using the imported Signature
    agent = dspy.ReAct(DataAssistantSignature, tools=[query_database_tool])

    # 4. Run the Agent
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(agent, question=clean_query),
            timeout=60
        )

        print("\n" + "="*50)
        print("🤖 DSPY INTERNAL LOGS (PROMPT & THOUGHTS)")
        print("="*50)
        dspy.inspect_history(n=1)
        print("="*50 + "\n")

        response_text = result.answer
        if final_sql:
            response_text += f"\n\n**🔍 Query Used:**\n```sql\n{final_sql}\n```"
        
        processing_msg.content = response_text
        if final_df is not None:
            processing_msg.elements = [cl.Dataframe(data=final_df, name="Result", display="inline")]
        
        await processing_msg.update()

    except asyncio.TimeoutError:
        processing_msg.content = "⚠️ ประมวลผลนานเกินไป (Agent Timeout)"
        await processing_msg.update()
    except Exception as e:
        processing_msg.content = f"❌ Error: {str(e)}"
        await processing_msg.update()