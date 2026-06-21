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
    1. If you don't know the table names or column structures, FIRST use `get_schema_tool` to explore the database.
    2. ONCE you know the structure, use `query_database_tool` to fetch the specific data needed.
    3. If the tool returns data, TRUST IT and use it to answer the question directly.
    4. NEVER apologize, never claim you have technical issues, and never say you cannot access data.
    5. Always answer in friendly, conversational Thai.
    6. If the data returned by the tool is a CSV/table, summarize the key findings instead of just dumping the raw text.
    """
    question = dspy.InputField(desc="The user's question about the data")
    answer = dspy.OutputField(desc="A direct, conversational answer in Thai summarizing the data. No apologies.")
