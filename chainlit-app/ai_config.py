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
    """You are a Excise POC's helpful and expert data assistant for a database. 
    
    CRITICAL RULES:
    1. You MUST use the `query_database_tool` to fetch data.
    2. When the tool returns data to you, YOU MUST TRUST IT AND USE IT to answer the question.
    3. NEVER apologize or claim you have technical issues or cannot access data if the tool returns results.
    4. Summarize the tool's output nicely and directly in friendly, conversational Thai.
    """
    question = dspy.InputField(desc="The user's question about the data")
    answer = dspy.OutputField(desc="A direct, conversational answer in Thai summarizing the exact data provided by the tool. No apologies.")