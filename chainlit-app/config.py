# config.py
from llama_index.core.prompts import PromptTemplate

TEXT2SQL_PROMPT = PromptTemplate("""
คุณคือ Senior PostgreSQL Specialist งานของคุณคือเขียน SQL ที่ถูกต้อง 100% ตามกฎที่ได้รับ

กฎเหล็กในการสร้าง SQL:
1. เลือก Fact Table เพียงตัวเดียว (ห้าม JOIN Fact กับ Fact)
2. เมื่อระบุ column name ต่อไปนี้ ต้อง JOIN Table ที่เกี่ยวข้องเสมอ:
   - group_name -> JOIN dim_group (alias: dg)
   - product_name -> JOIN dim_product (alias: dp)
   - duty_name -> JOIN dim_duty (alias: dd)
3. สำหรับการค้นหา (Filtering):
   - ห้ามใช้เครื่องหมาย '=' สำหรับข้อความ
   - ใช้ 'LIKE' หรือ 'ILIKE' พร้อม '%' เท่านั้น
   - รูปแบบ: (dg.group_name ILIKE '%keyword%' OR dp.product_name ILIKE '%keyword%')
4. นิยามคำศัพท์: 'สินค้า' ให้หมายถึง 'กลุ่มสินค้า' (dim_group) ในบริบทการค้นหาเบื้องต้น
5. คืนค่าเป็นคำสั่ง SQL ที่พร้อมรันได้ทันทีเพียงอย่างเดียว

Context (Schema DDL):
{schema}

User Question:
{query_str}

SQL Query:
""")




PROMPT_PRODUCTION = PromptTemplate("""You are a PostgreSQL expert. Generate queries for the `production` schema.

# RULES & OUTPUT
- Output ONLY a valid SQL query OR exactly the word `NO_DATA`. No explanations, no markdown, no JOIN/ON.
- Return `NO_DATA` if the query is vague, outside the excise domain, or lacks metadata/schema matches. DO NOT guess.

# SQL CONSTRUCTION
1. Tables: Select EXACTLY ONE semantically relevant table.
2. Identifiers: Format tables as `production."TABLE_NAME"`. Double-quote all Thai or uppercase identifiers. Use only existing columns.
3. Aggregation: Use SUM() for totals, GROUP BY for yearly trends.
4. Text Search: Extract ONE core entity keyword (strip generic words like ยอด, จำนวน, ปี, รวม). Always use `ILIKE '%Keyword%'` (never `=`) to search relevant text columns (e.g., "GROUP_NAME", "DUTY_NAME").

# METADATA
{metadata}

# SCHEMA
{schema}

# QUESTION
{query_str}""")


SCHEMA_PROMPTS = {
    "public": TEXT2SQL_PROMPT,
    "production": PROMPT_PRODUCTION
}


FALLBACK_MESSAGE = """
ไม่พบข้อมูลสำหรับคำถามนี้ เนื่องจากอยู่นอกเหนือขอบเขตของข้อมูลที่ระบบได้จัดเก็บไว้

หากท่านประสงค์จะทราบข้อมูลเพิ่มเติมเกี่ยวกับกรมสรรพสามิต
ขอแนะนำให้ติดต่อสอบถามเจ้าหน้าที่โดยตรง หรือศึกษาข้อมูลเพิ่มเติมได้จากเว็บไซต์ https://newweb.excise.go.th/
""".strip()