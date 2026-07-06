# app_05.py
import os
import re
import asyncio
import uuid
import pandas as pd
import chainlit as cl
from typing import Any, Tuple
from chainlit.input_widget import Select
from sqlalchemy import create_engine, text, Engine
import google.generativeai as genai
from gtts import gTTS
# --- AI & LlamaIndex Imports ---
import dspy
from llama_index.core import SQLDatabase
from llama_index.core.indices.struct_store.sql_retriever import NLSQLRetriever

# --- Local Configs ---
from config import SCHEMA_PROMPTS, FALLBACK_MESSAGE
from ai_config import init_ai_models, DataAssistantSignature

# =========================
# ENABLE CHAT HISTORY (UI)
# =========================
import chainlit.data as cl_data
from chainlit.data.sql_alchemy import SQLAlchemyDataLayer
from chainlit.context import context # อย่าลืม import ตัวนี้ไว้ด้านบนๆ ของไฟล์ด้วยนะครับ
# เปิดใช้งาน SQLite
cl_data._data_layer = SQLAlchemyDataLayer(conninfo="sqlite+aiosqlite:///chainlit_history.db")

CHAT_HISTORY_KEY = "chat_history"
DATA_CONTEXT_KEY = "data_context"
MAX_CHAT_HISTORY_MESSAGES = 5
MAX_DATA_CONTEXT_ITEMS = 3
MAX_DATA_CONTEXT_ROWS = 20
MAX_MEMORY_CHARS = 1200

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
    if not re.match(r"^(select|with)\b", clean_sql):
        return False, "อนุญาตเฉพาะคำสั่ง SELECT หรือ WITH เท่านั้น"
    if ";" in clean_sql.rstrip(";"):
        return False, "ไม่อนุญาตให้รัน SQL หลายคำสั่งพร้อมกัน"
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
    return False

# def is_metadata_question(query: str) -> bool:
#     q = query.lower()
#     strong_keywords = ["table", "ตาราง", "schema", "column", "field", "โครงสร้าง"]
#     return any(k in q for k in strong_keywords)

def normalize_sql(sql: str, schema: str) -> str:
    sql = re.sub(rf'\b{schema}\.{schema}\.', f'{schema}.', sql, flags=re.IGNORECASE)
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
    match = re.search(
        r'from\s+(?:(?:"?[\wก-๙_]+"?)\.)?(?:"([^"]+)"|([\wก-๙_]+))',
        sql,
        re.IGNORECASE,
    )
    if not match: return sql
    table = match.group(1) or match.group(2)
    columns = get_table_columns(engine, schema, table)
    for col in columns:
        sql = re.sub(rf'(?<!")\b{col}\b(?!")', f'"{col}"', sql, flags=re.IGNORECASE)
    return sql

def cleanup_double_quotes(sql: str) -> str:
    return re.sub(r'""(\w+)""', r'"\1"', sql)

def fetch_data(engine, query):
    with engine.connect() as conn:
        return pd.read_sql(text(query), conn)

def is_year_identifier_column(column_name: str) -> bool:
    name = column_name.strip()
    lower_name = name.lower()
    if any(marker in name for marker in ["%", "อัตรา", "เติบโต"]):
        return False
    if any(marker in lower_name for marker in ["rate", "ratio", "growth", "percent"]):
        return False

    normalized = re.sub(r"[\s_-]+", "", lower_name)
    year_names = {
        "ปี",
        "ปีงบประมาณ",
        "ปีเริ่มต้น",
        "ปีปลายทาง",
        "year",
        "budgetyear",
        "startyear",
        "endyear",
    }
    return normalized in year_names or lower_name.endswith("_year")

def format_numeric_columns(df: pd.DataFrame) -> pd.DataFrame:
    df_display = df.copy()
    for col in df_display.columns:
        col_name = str(col)
        col_name_lower = col_name.lower()
        if is_year_identifier_column(col_name):
            continue

        numeric_series = pd.to_numeric(df_display[col], errors="coerce")
        has_numeric_values = numeric_series.notna().any()
        is_numeric_column = pd.api.types.is_numeric_dtype(df_display[col])
        mostly_numeric_text = has_numeric_values and numeric_series.notna().mean() >= 0.8
        if is_numeric_column or mostly_numeric_text:
            if "%" in col_name or "rate" in col_name_lower or "ratio" in col_name_lower or "อัตรา" in col_name:
                df_display[col] = numeric_series.apply(lambda x: f"{x:,.2f}" if pd.notnull(x) else x)
            elif (numeric_series.dropna() % 1 == 0).all():
                df_display[col] = numeric_series.apply(lambda x: f"{x:,.0f}" if pd.notnull(x) else x)
            else:
                df_display[col] = numeric_series.apply(lambda x: f"{x:,.2f}" if pd.notnull(x) else x)
    return df_display

def ensure_khrap_ending(text: str) -> str:
    clean_text = text.strip()
    if not clean_text:
        return clean_text

    clean_text = re.sub(r"(ค่ะ|คะ)\s*([.!?。！？…]*)$", r"ครับ\2", clean_text)
    if re.search(r"ครับ\s*[.!?。！？…]*$", clean_text):
        return clean_text

    trailing_match = re.search(r"([.!?。！？…]*)$", clean_text)
    trailing = trailing_match.group(1) if trailing_match else ""
    body = clean_text[:-len(trailing)].rstrip() if trailing else clean_text.rstrip()
    separator = "" if re.search(r"[\u0E00-\u0E7F]$", body) else " "
    return f"{body}{separator}ครับ{trailing}"

def strip_query_used(text: str) -> str:
    return re.sub(r"\n\n\*\*🔍 Query Used:\*\*[\s\S]*$", "", text).strip()

def compact_memory_text(text: str, limit: int = MAX_MEMORY_CHARS) -> str:
    clean_text = re.sub(r"\s+", " ", strip_query_used(text)).strip()
    if len(clean_text) <= limit:
        return clean_text
    return clean_text[:limit].rstrip() + "..."

def get_chat_history() -> list[dict[str, str]]:
    return cl.user_session.get(CHAT_HISTORY_KEY, []) or []

def set_chat_history(history: list[dict[str, str]]) -> None:
    cl.user_session.set(CHAT_HISTORY_KEY, history[-MAX_CHAT_HISTORY_MESSAGES:])

def append_chat_history(role: str, content: str) -> None:
    if not content:
        return
    history = get_chat_history()
    history.append({"role": role, "content": compact_memory_text(content)})
    set_chat_history(history)

def format_chat_history() -> str:
    history = get_chat_history()
    if not history:
        return "No previous conversation in this thread."

    lines = []
    for item in history[-MAX_CHAT_HISTORY_MESSAGES:]:
        label = "User" if item.get("role") == "user" else "Assistant"
        lines.append(f"{label}: {item.get('content', '')}")
    return "\n".join(lines)

def get_data_context() -> list[dict[str, str]]:
    return cl.user_session.get(DATA_CONTEXT_KEY, []) or []

def set_data_context(contexts: list[dict[str, str]]) -> None:
    cl.user_session.set(DATA_CONTEXT_KEY, contexts[-MAX_DATA_CONTEXT_ITEMS:])

def extract_query_used(text: str) -> str:
    match = re.search(r"\*\*🔍 Query Used:\*\*\s*```sql\s*([\s\S]*?)```", text, re.IGNORECASE)
    return clean_sql_output(match.group(1)) if match else ""

def append_data_context(database_question: str, sql: str, df: pd.DataFrame | None = None, answer: str = "") -> None:
    if not sql:
        return

    context = {
        "question": compact_memory_text(database_question, limit=500),
        "sql": clean_sql_output(sql),
        "answer": compact_memory_text(answer, limit=500) if answer else "",
        "rows_csv": "",
    }
    if df is not None and not df.empty:
        context["rows_csv"] = df.head(MAX_DATA_CONTEXT_ROWS).to_csv(index=False)

    contexts = get_data_context()
    contexts.append(context)
    set_data_context(contexts)

def format_data_context() -> str:
    contexts = get_data_context()
    if not contexts:
        return "No verified database query context yet."

    blocks = ["Recent verified database query context. Use this only to resolve follow-up references; run a fresh query for new calculations:"]
    for index, item in enumerate(contexts[-MAX_DATA_CONTEXT_ITEMS:], start=1):
        block = [
            f"Context {index}:",
            f"User/database question: {item.get('question', '')}",
            "SQL:",
            item.get("sql", ""),
        ]
        if item.get("rows_csv"):
            block.extend(["Result preview CSV:", item["rows_csv"]])
        if item.get("answer"):
            block.extend(["Assistant summary:", item["answer"]])
        blocks.append("\n".join(block))
    return "\n\n".join(blocks)

def build_retriever_question(database_question: str) -> str:
    data_context = format_data_context()
    if data_context.startswith("No verified"):
        return database_question

    return f"""
Use the verified context below only to resolve follow-up references such as source table, filters, grouping, and metric.
Generate a fresh SQL query for the current request. Do not answer from the context text alone.

{data_context}

Current request:
{database_question}
""".strip()

def seed_chat_history_from_thread(thread: Any) -> None:
    steps = thread.get("steps", []) if isinstance(thread, dict) else []
    history: list[dict[str, str]] = []
    contexts: list[dict[str, str]] = []
    last_user_message = ""

    for step in steps:
        if not isinstance(step, dict):
            continue
        step_type = step.get("type")
        content = step.get("output") or step.get("input") or ""
        if not content:
            continue
        if step_type == "user_message":
            last_user_message = content
            history.append({"role": "user", "content": compact_memory_text(content)})
        elif step_type == "assistant_message":
            history.append({"role": "assistant", "content": compact_memory_text(content)})
            sql = extract_query_used(content)
            if sql:
                contexts.append({
                    "question": compact_memory_text(last_user_message or "Resumed thread question", limit=500),
                    "sql": sql,
                    "answer": compact_memory_text(content, limit=500),
                    "rows_csv": "",
                })

    set_chat_history(history)
    set_data_context(contexts)

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

    sql_retriever = NLSQLRetriever(
        sql_database=sql_database,
        text_to_sql_prompt=formatted_prompt,
        sql_only=True,
        handle_sql_errors=False,
    )
    return sql_retriever, engine

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
    cl.user_session.set(CHAT_HISTORY_KEY, [])
    cl.user_session.set(DATA_CONTEXT_KEY, [])

@cl.on_chat_resume
async def on_chat_resume(thread):
    cl.user_session.set("current_schema", cl.user_session.get("current_schema", "production"))
    seed_chat_history_from_thread(thread)

@cl.on_settings_update
async def on_settings_update(settings):
    schema = settings["schema"]
    cl.user_session.set("current_schema", schema)
    await cl.Message(content=f"✅ เปลี่ยน Schema เป็น `{schema}` สำเร็จ").send()

async def update_ai_message(
    processing_msg: cl.Message,
    answer: str,
    final_sql: str = "",
    final_df: pd.DataFrame | None = None,
) -> None:
    answer_text = ensure_khrap_ending(answer)
    response_text = answer_text
    if final_sql:
        response_text += f"\n\n**🔍 Query Used:**\n```sql\n{final_sql}\n```"

    processing_msg.content = response_text
    processing_msg.elements = []
    if final_df is not None:
        processing_msg.elements.append(cl.Dataframe(data=final_df, name="Result", display="inline"))

    try:
        os.makedirs(".files", exist_ok=True)
        speech_path = os.path.join(".files", f"ai_response_{uuid.uuid4().hex}.mp3")

        communicate = edge_tts.Communicate(
            text=answer_text,
            voice="th-TH-NiwatNeural",
        )
        await communicate.save(speech_path)

        processing_msg.elements.append(
            cl.Audio(name="🔊 ฟังเสียงตอบกลับ", path=speech_path, display="inline")
        )
    except Exception as e:
        print(f"TTS Error: {e}")

    await processing_msg.update()
    append_chat_history("assistant", answer_text)

# =========================
# MESSAGE HANDLER (DSPy ReAct Agent)
# =========================

@cl.on_message
async def on_message(message: cl.Message):
    schema = cl.user_session.get("current_schema", "public")
    clean_query = re.sub(r'\s+', ' ', message.content).strip()

    processing_msg = cl.Message(content="กำลังวิเคราะห์และดึงข้อมูล...")
    await processing_msg.send()
    append_chat_history("user", clean_query)

    # 1. Intercept Metadata questions first
    # if is_metadata_question(clean_query):
    #     _, db_engine = build_query_engine(schema)
    #     def get_tables(engine, schema):
    #         query = text("""
    #             SELECT table_name FROM information_schema.tables 
    #             WHERE table_schema = :schema AND table_type = 'BASE TABLE'
    #         """)
    #         with engine.connect() as conn:
    #             return [row[0] for row in conn.execute(query, {"schema": schema})]
        
    #     tables = get_tables(db_engine, schema)
    #     if tables:
    #         table_list = "\n".join([f"- {t}" for t in tables])
    #         processing_msg.content = f"📁 ตารางใน schema `{schema}`:\n{table_list}"
    #     else:
    #         processing_msg.content = "ไม่พบตารางในระบบ"
    #     await processing_msg.update()
    #     return

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
            sql_retriever, db_engine = build_query_engine(schema)
            retriever_question = build_retriever_question(database_question)
            nodes, metadata = sql_retriever.retrieve_with_metadata(retriever_question)
            raw_sql = metadata.get("sql_query") or metadata.get("result") or ""
            if not raw_sql and nodes:
                raw_sql = nodes[0].node.get_content()
            print(sql_retriever)
            print(f"Generated SQL: {raw_sql}")
            sql = clean_sql_output(raw_sql)

            if not sql:
                return "CLARIFICATION_NEEDED: No SQL was generated. Ask a concise clarification question in Thai."

            if sql.strip().upper() == "NO_DATA":
                return (
                    "CLARIFICATION_NEEDED: The generated SQL was NO_DATA. "
                    "Ask the user a concise clarification question in Thai instead of executing SQL."
                )

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
            append_data_context(database_question, sql, df)

            # ใช้ .to_csv() แทน .to_markdown() เพื่อป้องกันปัญหา Library tabulate หาย
            csv_data = df.head(30).to_csv(index=False)
            return f"✅ SUCCESS: Database query completed. Here is the factual data you must use to answer the user:\n\n{csv_data}"
            
        except Exception as e:
            return f"Error executing query: {str(e)}"

    def get_schema_tool(query_unused: str = "") -> str:
            """
            Retrieves the full database schema, including all table names and their column names.
            Use this tool when you need to understand the database structure, table names, or column names to write a query.
            """
            _, db_engine = build_query_engine(schema)
            
            # ดึงข้อมูลจาก database
            query = text("""
                SELECT t.table_name, c.column_name, c.data_type
                FROM information_schema.tables t
                JOIN information_schema.columns c
                  ON t.table_schema = c.table_schema
                 AND t.table_name = c.table_name
                WHERE t.table_schema = :schema
                ORDER BY t.table_name, c.ordinal_position;
            """)
            
            with db_engine.connect() as conn:
                result = conn.execute(query, {"schema": schema})
                schema_dict = {}
                for row in result:
                    table, column, dtype = row
                    if table not in schema_dict:
                        schema_dict[table] = []
                    schema_dict[table].append(f"{column} ({dtype})")
            
            # แปลงเป็นสตริงเพื่อส่งให้ Agent
            output = "Database Schema:\n"
            for table, cols in schema_dict.items():
                output += f"\nTable: {table}\n  Columns: " + ", ".join(cols) + "\n"
            
            return output

    # 3. Initialize the ReAct Agent using the imported Signature
    agent = dspy.ReAct(DataAssistantSignature, tools=[query_database_tool, get_schema_tool])
    # 4. Run the Agent
    try:
        chat_history = format_chat_history()
        data_context = format_data_context()
        result = await asyncio.wait_for(
            asyncio.to_thread(agent, question=clean_query, chat_history=chat_history, data_context=data_context),
            timeout=60
        )

        print("\n" + "="*50)
        print("🤖 DSPY INTERNAL LOGS (PROMPT & THOUGHTS)")
        print("="*50)
        dspy.inspect_history(n=1)
        print("="*50 + "\n")

        await update_ai_message(processing_msg, result.answer, final_sql, final_df)

    except asyncio.TimeoutError:
        processing_msg.content = "⚠️ ประมวลผลนานเกินไป (Agent Timeout)"
        await processing_msg.update()
    except Exception as e:
        processing_msg.content = f"❌ Error: {str(e)}"
        await processing_msg.update()


import wave
from gtts import gTTS
import edge_tts  # 👈 เพิ่มบรรทัดนี้

# ตั้งค่า API Key สำหรับ Gemini
genai.configure(api_key=os.getenv("API_KEY_4"))

# =========================
# 🔘 ACTION BUTTONS (ปุ่มยืนยัน/ยกเลิกเสียง)
# =========================

@cl.action_callback("confirm_audio")
async def on_confirm_audio(action: cl.Action):
    # 1. พอกดปุ่มยืนยัน ให้ลบชุดปุ่มออกไป
    await action.remove()
    
    # 2. ดึงข้อความจาก payload 
    user_text = action.payload.get("value")
    
    # --- 💡 เพิ่มโค้ดบังคับเปลี่ยนชื่อ Tab (Thread) ตรงนี้ ---
    try:
        if cl_data._data_layer:
            thread_id = context.session.thread_id
            # ย่อข้อความให้เหลือแค่ 30 ตัวอักษรแรก เพื่อให้ชื่อ Tab ไม่ยาวเกินไป
            new_title = user_text[:30] + ("..." if len(user_text) > 30 else "")
            await cl_data._data_layer.update_thread(thread_id=thread_id, name=new_title)
    except Exception as e:
        print(f"ไม่สามารถเปลี่ยนชื่อ Tab ได้: {e}")
    # ------------------------------------------------------
    
    # โยนข้อความเข้า Agent ให้ประมวลผลต่อ
    mock_message = cl.Message(content=user_text, author="User")
    await on_message(mock_message)

@cl.action_callback("cancel_audio")
async def on_cancel_audio(action: cl.Action):
    # ถ้ากดยกเลิก ลบปุ่มออก
    await action.remove()
    await cl.Message(content="❌ *ยกเลิกแล้วครับ คุณสามารถก๊อปปี้ข้อความด้านบนเพื่อนำไปแก้ไขในกล่องแชทและพิมพ์ส่งใหม่ได้เลย*").send()



# =========================
# 🎤 AUDIO INPUT HANDLER (STT - Speech to Text)
# =========================

@cl.on_audio_start
async def on_audio_start():
    cl.user_session.set("audio_buffer", bytearray())
    return True

@cl.on_audio_chunk
async def on_audio_chunk(chunk):
    buffer = cl.user_session.get("audio_buffer")
    if buffer is not None:
        data = chunk.data if hasattr(chunk, 'data') else chunk
        buffer.extend(data)

# 👇 แก้ตรงนี้ครับ: เอา elements ออกจากวงเล็บ
@cl.on_audio_end
async def on_audio_end(**kwargs): 
    msg = cl.Message(content="⏳ กำลังฟังและวิเคราะห์เสียงด้วย Gemini...")
    await msg.send()

    buffer = cl.user_session.get("audio_buffer")
    if not buffer or len(buffer) == 0:
        msg.content = "❌ ไม่ได้รับข้อมูลเสียง กรุณาลองใหม่อีกครั้งครับ"
        await msg.update()
        return

    # ประกอบร่างเสียงดิบ
    audio_path = "user_audio.wav"
    with wave.open(audio_path, "wb") as wav_file:
        wav_file.setnchannels(1)      
        wav_file.setsampwidth(2)     
        wav_file.setframerate(24000)  
        wav_file.writeframes(buffer)

    try:
        # อัปโหลดและแปลเสียงด้วย Gemini
        uploaded_audio = await asyncio.to_thread(genai.upload_file, path=audio_path)
        model = genai.GenerativeModel('gemini-2.5-flash')
        response = await model.generate_content_async([
            "พิมพ์ข้อความที่คุณได้ยินจากเสียงนี้ออกมาให้ถูกต้อง เป็นภาษาไทย โดยไม่ต้องอธิบายหรือเพิ่มคำพูดอื่นๆ",
            uploaded_audio
        ])
        
        user_text = response.text.strip()
        await asyncio.to_thread(genai.delete_file, uploaded_audio.name)

        # สร้างปุ่มให้ User เลือกว่าจะเอายังไง
        actions = [
            cl.Action(name="confirm_audio", payload={"value": user_text}, label="✅ ยืนยันและส่งให้ AI"),
            cl.Action(name="cancel_audio", payload={"value": "cancel"}, label="❌ ยกเลิก (เพื่อแก้ไขเอง)")
        ]
        
        msg.content = f"🗣️ **ระบบได้ยินว่า:**\n\n> {user_text}\n\nคุณต้องการส่งข้อความนี้เลยหรือไม่?"
        msg.actions = actions
        await msg.update()

    except Exception as e:
        msg.content = f"❌ เกิดข้อผิดพลาดในการแปลเสียง: {str(e)}"
        await msg.update()
