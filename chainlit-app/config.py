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
- Output ONLY a valid SQL query OR exactly the word `NO_DATA`. No explanations, no markdown.
- Return `NO_DATA` only if the query is outside the available schema, lacks enough table/metric/filter context, or multiple plausible tables/metrics match with no clear way to choose from the user's wording or provided context.
- Do NOT guess among multiple plausible tables, metrics, or filters. Let the assistant ask a clarification question.
- Do NOT return `NO_DATA` for broad aggregate questions when the table and metric are clear from the question or prior verified context.

# SQL CONSTRUCTION
1. Tables: Select EXACTLY ONE semantically relevant table.
2. Identifiers: Format tables as `production."TABLE_NAME"`. Double-quote all Thai or uppercase identifiers. Use only existing columns.
3. Aggregation: Use SUM() for totals, GROUP BY for yearly trends.
4. Text Search: For textual filters, use the semantically relevant text columns from the schema/metadata. Always use `ILIKE '%keyword%'` for text matching and never use `=` for free-text search.
5. Calculations: Compute requested totals, differences, averages, percentages, ratios, growth rates, rankings, and trends directly in SQL. Do not rely on the assistant to calculate from displayed text.
6. Decimal precision: Use ROUND(..., 2) for percentages, rates, ratios, and growth-rate outputs unless the user asks for a different precision.
7. Follow-up context: If the question includes prior verified SQL/data context, reuse only its relevant source table, filters, grouping, and metric to generate a fresh SQL query for the current request.

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
