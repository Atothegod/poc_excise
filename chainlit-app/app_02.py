import os
import io
import pandas as pd
import chainlit as cl
import matplotlib.pyplot as plt
import matplotlib
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()


import litellm
litellm.ssl_verify = False

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
คุณคือผู้เชี่ยวชาญการเขียน SQL สำหรับ PostgreSQL

ให้สร้าง SQL ที่ถูกต้องเท่านั้น
ห้ามอธิบาย
ห้ามสร้างคอลัมน์หรือชื่อตารางที่ไม่มีอยู่จริง

========================
โครงสร้างฐานข้อมูล (Schema)
========================
{schema_info}

========================
กฎตรวจสอบคอลัมน์ก่อนใช้ (สำคัญมาก)
========================

ก่อนใช้คอลัมน์ใด ๆ ต้องตรวจสอบก่อนว่า
คอลัมน์นั้นมีอยู่จริงใน Schema ด้านบน

ถ้าไม่มีอยู่ใน Schema:
- ห้ามใช้เด็ดขาด
- ห้ามสร้างขึ้นเอง
- ห้ามเดา

ถ้าผู้ใช้ถามถึงเงื่อนไขที่ไม่มีข้อมูลรองรับในฐานข้อมูล
ให้ละทิ้งเงื่อนไขนั้น และสร้าง SQL จากข้อมูลที่มีอยู่จริงเท่านั้น

========================
กฎการสกัดคำค้นและตัวเลข (สำคัญมาก)
========================

หากคำถามมีเงื่อนไขเชิงปริมาณ เช่น
- เกิน 200 กรัม/กม.
- มากกว่า 150 กรัม/กม.
- ต่ำกว่า 100

และไม่มีคอลัมน์ตัวเลขใน Schema รองรับ

ให้ปฏิบัติดังนี้:

1. แยก ออกมาเสมอ
   เช่น:
   "เกิน 200 กรัม/กม." → %> 200 กรัม% เสมอ หรือ
   "ต่ำกว่า 100" → %< 100%

2. ห้ามทิ้งตัวเลขเด็ดขาด

3. ให้ใช้ตัวเลขนั้นในเงื่อนไข ILIKE
   โดยค้นหาเฉพาะตัวเลข

ตัวอย่าง:

dp.product_name ILIKE '%เกิน 200 กรัม%'

4. หากมีทั้งคำอธิบายและตัวเลข
   ให้ใช้หลายเงื่อนไข AND ร่วมกัน

ตัวอย่าง:

dp.product_name ILIKE '%รถ%'
AND dp.product_name ILIKE '%เกิน 200 กรัม%'

========================
ขั้นตอนการคิด (ห้ามข้ามเด็ดขาด)
========================

ขั้นตอนที่ 1: เลือก Fact Table ให้ถูกต้อง (ใช้ได้เพียง 1 ตารางเท่านั้น)

- ถ้าคำถามเกี่ยวกับ "ภาษี" หรือ "รายได้" → ใช้ fact_tax
- ถ้าเกี่ยวกับ "ใบอนุญาต" → ใช้ fact_license
- ถ้าเกี่ยวกับ "ค่าปรับ หรือ ปรับ" → ใช้ fact_case
- ถ้าเกี่ยวกับ "การจดทะเบียน" → ใช้ fact_registration

ห้าม join ตาราง fact อื่นเพิ่มเติม
ใช้ได้เพียง fact table เดียวเท่านั้น

------------------------

ขั้นตอนที่ 2: การ Join ตารางมิติ (Dimension)

1) การเลือก Dimension ตาม Fact Table

กรณีใช้ fact_tax:
สามารถ JOIN ได้ดังนี้ แล้ว ใช้คำว่า ภาษีนำหน้า
เช่น 'อยากทราบภาษีรถยนต์' > 'ภาษีรถยนต์'
- JOIN dim_time ผ่าน time_id
- JOIN dim_group ผ่าน group_id
- JOIN dim_product ผ่าน product_id

และถ้ามี keyword ให้ค้นหาจากทั้ง 3 คอลัมน์นี้:

(
  dim_group.group_name ILIKE '%คำค้น%'
  OR dim_product.duty_name ILIKE '%คำค้น%'
  OR dim_product.product_name ILIKE '%คำค้น%'
)

-------------------------------------

กรณีใช้ fact_case, fact_license, fact_registration:

- JOIN ได้เฉพาะ dim_time ผ่าน time_id
- JOIN ได้เฉพาะ dim_group ผ่าน group_id

ห้าม JOIN dim_product เพราะไม่มี product_id

และหากมี keyword
ให้ค้นหาได้เฉพาะ:

dim_group.group_name ILIKE '%คำค้น%'


------------------------

ขั้นตอนที่ 3: การ SUM ให้ถูกต้อง

- fact_tax → SUM(tax_nettax_amt)
- fact_license → SUM(no_of_lic)
- fact_case → SUM(case_amt)
- fact_registration → SUM(no_of_reg)

------------------------

ขั้นตอนที่ 4: การใช้ keyword filter

ให้ใช้รูปแบบ:

ILIKE '%คำค้นหา%'

ห้ามใช้ EXISTS โดยไม่จำเป็น
ใช้ JOIN ตรงไปตรงมา

========================

ข้อห้ามสำคัญ:
- ห้ามใช้ dt.year และ dt.month
- ห้ามใช้ year และ month โดยไม่มีคำว่า budget_
- ให้ใช้ dt.budget_year , dt.budget_month เท่านั้น
ส่งออกเฉพาะ SQL ที่ถูกต้องสำหรับ PostgreSQL เท่านั้น
ห้ามมีข้อความอื่น
**fact_case, fact_license, fact_registration ไม่มี product
มีแค่ group_name**
- หากไม่มีข้อมูล ให้คืนค่า 'ไม่มีข้อมูล'
คำถามผู้ใช้:
{query_str}

SQL:
""")

query_engine = NLSQLTableQueryEngine(
    sql_database=sql_database,
    text_to_sql_prompt=custom_sql_prompt
)

def generate_chart(df):
    try:
        if df is None or df.shape[1] < 2:
            return None

        # ใช้ 2 คอลัมน์แรก
        x = df.iloc[:, 0].astype(str)
        y = pd.to_numeric(df.iloc[:, 1], errors="coerce")

        # ลบค่า NaN
        mask = y.notna()
        x = x[mask]
        y = y[mask]

        if len(y) == 0:
            return None

        plt.figure(figsize=(10, 5))
        plt.bar(x, y)
        plt.xticks(rotation=45)
        plt.tight_layout()

        buf = io.BytesIO()
        plt.savefig(buf, format="png")
        plt.close()
        buf.seek(0)

        return buf.getvalue()

    except Exception as e:
        print("Graph Error:", e)
        return None

@cl.action_callback("plot_graph")
async def plot_graph_callback(action):
    df = cl.user_session.get("dataframe")

    if df is None:
        await cl.Message(content="❌ ไม่มีข้อมูลให้สร้างกราฟ").send()
        return

    chart = generate_chart(df)

    if chart:
        await cl.Message(
            content="📊 กราฟจากข้อมูลล่าสุด",
            elements=[cl.Image(name="chart.png", content=chart)]
        ).send()

from chainlit.input_widget import Select

@cl.on_chat_start
async def start():
    settings = await cl.ChatSettings(
        inputs=[
            Select(
                id="mode",
                label="Mode",
                values=["Default", "Database"],
                initial_index=1,
            ),
            Select(
                id="tool",
                label="Tool",
                values=["None", "Canvas"],  
                initial_index=0,
            ),
        ]
    ).send()

    cl.user_session.set("mode", settings["mode"])
    cl.user_session.set("tool", settings["tool"])
    cl.user_session.set("dataframe", None)
    cl.user_session.set("history", [])

    await cl.Message(content="🚀 Ready").send()

@cl.on_settings_update
async def update_settings(settings):
    cl.user_session.set("mode", settings["mode"])
    cl.user_session.set("tool", settings["tool"])

@cl.on_message
async def main(message: cl.Message):
    mode = cl.user_session.get("mode")
    tool = cl.user_session.get("tool")
    history = cl.user_session.get("history")

    if mode == "Database":
        response = query_engine.query(message.content)

        sql_query = None
        if hasattr(response, "metadata"):
            sql_query = response.metadata.get("sql_query")

        print("Generated SQL:", sql_query)

        df = None
        if not sql_query or "SELECT" not in sql_query.upper():
            await cl.Message(content="❌ ไม่สามารถสร้างคำสั่ง SQL ได้").send()
            return
            
        if sql_query:
            df = pd.read_sql(text(sql_query), pg_engine)

        cl.user_session.set("dataframe", df)

        result_text = str(response)

        history.append(f"User: {message.content}")
        history.append(f"Assistant (Database Result): {result_text}")
        cl.user_session.set("history", history)

        await cl.Message(content=result_text).send()

        if df is not None and df.shape[1] >= 2:
            await cl.Message(
                content="เลือกการแสดงผลเพิ่มเติม:",
                actions=[
                    cl.Action(
                        name="plot_graph",
                        label="📊 Plot Graph",
                        payload={}
                    )
                ]
            ).send()

        # 3️⃣ Canvas Report
        if tool == "Canvas" and df is not None:
            prompt = f"""
คุณเป็นนักวิเคราะห์ข้อมูล
ใช้เฉพาะข้อมูลด้านล่างเท่านั้น
ห้ามสมมติข้อมูลเพิ่ม

{df.to_string()}

สร้างรายงานเชิงธุรกิจแบบกระชับ
"""
            report = llm.complete(prompt).text
            await cl.Message(content=report).send()

        return

    conversation = "\n".join(history)
    conversation += f"\nUser: {message.content}\nAssistant:"

    response = llm.complete(conversation).text

    history.append(f"User: {message.content}")
    history.append(f"Assistant: {response}")
    cl.user_session.set("history", history)

    await cl.Message(content=response).send()