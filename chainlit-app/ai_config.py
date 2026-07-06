# ai_config.py
import os
import dspy
import litellm
from dotenv import load_dotenv

from llama_index.llms.litellm import LiteLLM
from llama_index.core import Settings

def init_ai_models():
    """
    ฟังก์ชันสำหรับตั้งค่า LLM ทั้งหมด (LlamaIndex และ DSPy)
    """
    load_dotenv()
    litellm.ssl_verify = False

    # 1. Setup LlamaIndex LLM
    llm = LiteLLM(
        model="gemini/gemini-2.5-flash",
        api_key=os.getenv("API_KEY_4"),
        temperature=0,
        request_timeout=60,
    )
    Settings.llm = llm
    Settings.embed_model = None

    # 2. Setup DSPy LLM globally
    dspy_lm = dspy.LM(
        "gemini/gemini-2.5-flash", 
        api_key=os.getenv("API_KEY_4"), 
        temperature=0
    )
    dspy.settings.configure(lm=dspy_lm)


# 3. Define the DSPy Signature
class DataAssistantSignature(dspy.Signature):
    """
    You are an expert Data Assistant for an Excise Department database.
    
    GUIDELINES:
    1. Use `chat_history` to understand follow-up questions and unresolved clarifications in the current thread.
    2. Use `data_context` only to resolve follow-up references such as the prior source table, filters, grouping, and metric.
    3. For every data-derived answer, numeric result, trend, comparison, percentage, ratio, ranking, aggregate, or calculation, you MUST call `query_database_tool` in the current turn. Do not calculate from chat history or `data_context` alone.
    4. When calling `query_database_tool` for a follow-up, rewrite the request as a standalone database question using the verified `data_context`.
    5. If you don't know the table names or column structures, FIRST use `get_schema_tool` to explore the database.
    6. If multiple tables, metrics, or filters could satisfy the request, ask one concise clarification question in Thai using dynamic candidates found from schema/context. Do not invent fixed choices.
    7. If a tool returns CLARIFICATION_NEEDED, ask the user a concise clarification question instead of apologizing.
    8. If the tool returns data, TRUST IT and use it to answer the question directly. Mention the source/metric when useful; the app will show the table and SQL separately.
    9. NEVER apologize, never claim you have technical issues, and never say you cannot access data.
    10. Always answer in friendly, conversational Thai and end every final answer with "ครับ".
    11. If the data returned by the tool is a CSV/table, summarize the key findings instead of just dumping the raw text.
    """
    chat_history = dspy.InputField(desc="Recent conversation turns in this thread. Use for follow-up questions and clarifications.")
    data_context = dspy.InputField(desc="Verified SQL/data context from recent database tool calls. Use it to resolve follow-ups, but run a fresh query for new data-derived answers.")
    question = dspy.InputField(desc="The user's question about the data")
    answer = dspy.OutputField(desc="A direct, conversational answer in Thai summarizing the data. No apologies. Must end with ครับ.")
