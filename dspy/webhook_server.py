from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
from datetime import datetime

# 1. Import the stateful 'chatbot' instance you created in agent.py
from agent import chatbot

app = FastAPI(title="DSPy Agent Webhook Server")

# --- CORS configuration ---

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -----------------------------------


class QuestionRequest(BaseModel):
    question: str
    session_id: str
    time_stamp: Optional[str] = None  # Allows the field to be missing or null


@app.post("/ask")
async def ask_agent(data: QuestionRequest):
    try:
        # 2. Check if time_stamp is null. If so, generate the real current system time.
        if data.time_stamp is None:
            # .astimezone() ensures the timezone offset (like +07:00) is included
            final_time_stamp = datetime.now().astimezone().isoformat()
        else:
            final_time_stamp = data.time_stamp

        # 3. Pass the resolved timestamp into your stateful MemoryAgent wrapper
        response = chatbot.chat(
            user_input=data.question,
            session_id=data.session_id,
            time_stamp=final_time_stamp,
        )

        # 4. Return the natural language answer AND the Pydantic state
        return {
            "answer": getattr(response, "answer", str(response)),
            "state": getattr(response, "current_state", None),
            "used_timestamp": final_time_stamp,  # Optional: helpful for debugging what time was actually used
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
def health_check():
    return {"status": "healthy"}
