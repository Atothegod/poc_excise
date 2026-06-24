from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
from datetime import datetime

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
    time_stamp: Optional[str] = None

@app.post("/ask")
async def ask_agent(data: QuestionRequest):
    try:
        response = chatbot.chat(user_input=data.question, session_id=data.session_id, time_stamp=data.time_stamp)
        
        return {
            "answer": getattr(response, 'answer', str(response)),
            "state": getattr(response, 'current_state', None)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/health")
def health_check():
    return {"status": "healthy"}