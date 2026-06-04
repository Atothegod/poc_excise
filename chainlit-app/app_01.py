import os
import chainlit as cl
from dotenv import load_dotenv
load_dotenv()
import litellm
litellm.ssl_verify = False
import pandas as pd
# ---------- LiteLLM ----------
from llama_index.llms.litellm import LiteLLM
from llama_index.core import Settings

llm = LiteLLM(
    model="openai/gpt-oss:20b",
    api_base=os.getenv("LLM_URL"),
    api_key=os.getenv("API_KEY"),
    temperature=0
)

Settings.llm = llm
Settings.embed_model = None
# ---------- PostgreSQL ----------
from sqlalchemy import create_engine
from llama_index.core import SQLDatabase
from llama_index.core.query_engine import NLSQLTableQueryEngine
from llama_index.core.prompts import PromptTemplate

pg_engine = create_engine(
    f"postgresql+psycopg2://{os.getenv('POSTGRES_USER')}:"
    f"{os.getenv('POSTGRES_PASSWORD')}@db:5432/{os.getenv('LLM_DB')}"
)

sql_database = SQLDatabase(engine=pg_engine)


def get_schema_info(engine):
    query = """
    SELECT table_name, column_name
    FROM information_schema.columns
    WHERE table_schema = 'public'
    ORDER BY table_name, ordinal_position;
    """
    df = pd.read_sql(query, engine)

    schema_text = ""
    for table in df["table_name"].unique():
        cols = df[df["table_name"] == table]["column_name"].tolist()
        col_str = ", ".join(cols)
        schema_text += f"{table}: {col_str}\n"

    return schema_text

schema_info = get_schema_info(pg_engine)

custom_sql_prompt = PromptTemplate("""
คุณเป็นผู้เชี่ยวชาญ PostgreSQL

โครงสร้างตารางคือ:
{schema_info}

กฎ:
- ห้าม join ระหว่าง fact กับ fact
- ขั่นตอนคือเลือก fact table แล้ว JOIN แค่ dim ภายใน fact นั้น
- คำว่า 'สินค้า' คือ group_name, duty_name และ product_name
- ใช้เฉพาะชื่อตารางใน schema
- หากคำถามถามผลรวมทั้งหมด ไม่ต้อง JOIN ตารางอื่น
- ใช้ SUM() สำหรับค่าจำนวน
- JOIN เฉพาะเมื่อจำเป็น
- ตอบเป็น SQL เท่านั้น
- ห้ามมีคำอธิบาย

คำถาม:
{query_str}

SQL:
""")

# 👇 inject schema ตรงนี้
custom_sql_prompt = custom_sql_prompt.partial_format(
    schema_info=schema_info
)

query_engine = NLSQLTableQueryEngine(
    sql_database=sql_database,
    text_to_sql_prompt=custom_sql_prompt,
)
# ---------- Chainlit ----------
@cl.on_chat_start
async def start():
    await cl.Message(
        content="""
## Welcome to Tax Assistant

คุณสามารถพิมพ์คำถามเช่น:
- ภาษีทั้งหมดปี 2566
- ภาษีรถยนต์ รายเดือน ปี 2568
- ยอดการจัดเก็บภาษีในแต่ละปี
"""
    ).send()

print(schema_info)

@cl.on_message
async def main(message: cl.Message):
    response = query_engine.query(message.content)

    # ✅ print SQL ที่ LLM generate
    if hasattr(response, "metadata"):
        print("\n========== GENERATED SQL ==========")
        print(response.metadata.get("sql_query"))
        print("===================================\n")

    # แสดงผลลัพธ์
    output = response.response if hasattr(response, "response") else str(response)
    await cl.Message(content=output).send()
