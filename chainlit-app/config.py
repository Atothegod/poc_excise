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


PROMPT_PRODUCTION = PromptTemplate("""
You are a PostgreSQL Production Expert.

Generate a correct SQL query for schema `production`.

# OUTPUT RULES
- Return ONLY one of:
  1. A valid SQL query (starting with SELECT or WITH)
  2. EXACTLY this word: NO_DATA
- No explanation
- No markdown
- No JOIN, no ON

# WHEN TO RETURN NO_DATA

Return NO_DATA if:
- The question refers to a product, group, or entity that is NOT related to excise domain
- The keyword does not match any BUSINESS METADATA or SCHEMA
- The request cannot be mapped to any table or column
- The keyword is too vague or unrelated (e.g., ข้าวสาร, รถยนต์ไฟฟ้า ถ้าไม่มีใน domain)

DO NOT guess.

# TABLE SELECTION

1. Choose exactly ONE table based on semantic meaning.
   - Fine → fine table
   - Tax → tax table
   - Never choose unrelated category.

2. Table must contain relevant columns.
   Do not guess column names.

3. Always prefix table with:
   production."TABLE_NAME"

4. Thai or uppercase identifiers must use double quotes.

# KEYWORD EXTRACTION

If question refers to product/item:

1. Extract ONE keyword entity only.
2. Remove generic words:
   ยอด, จำนวน, รายงาน, ปี, รวม, ทั้งหมด, เท่าไหร่ ฯลฯ
3. Use format:
   '%Keyword%'

Text search must use ILIKE only:

(
 "GROUP_NAME" ILIKE '%Keyword%'
 OR "DUTY_NAME" ILIKE '%Keyword%'
 OR "PRODUCT_NAME" ILIKE '%Keyword%'
)

No '=' for text.

# AGGREGATION

If asking total → use SUM().
If yearly → GROUP BY year column.

# BUSINESS METADATA
{metadata}

# SCHEMA
{schema}

# QUESTION
{query_str}
""")

SCHEMA_PROMPTS = {
    "public": TEXT2SQL_PROMPT,
    "production": PROMPT_PRODUCTION
}


FALLBACK_MESSAGE = """
ไม่พบข้อมูลสำหรับคำถามนี้ เนื่องจากอยู่นอกเหนือขอบเขตของข้อมูลที่ระบบได้จัดเก็บไว้

หากท่านประสงค์จะทราบข้อมูลเพิ่มเติมเกี่ยวกับกรมสรรพสามิต
ขอแนะนำให้ติดต่อสอบถามเจ้าหน้าที่โดยตรง หรือศึกษาข้อมูลเพิ่มเติมได้จากเว็บไซต์ https://newweb.excise.go.th/
""".strip()