import os
import secrets
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from agent import chatbot


app = FastAPI(title="Stateless DSPy Agent")
INTERNAL_TOKEN = os.getenv(
    "AGENT_INTERNAL_TOKEN", "development-agent-internal-token"
)


@app.get("/health")
def health():
    return {"status": "ok"}


class QuestionRequest(BaseModel):
    job_id: str
    user_message_id: int
    question: str
    session_id: str
    ca_number: Optional[str] = None
    pdpa_consent: bool = False


def _state_payload(state, ca_number=None):
    if state is None:
        payload = {}
    elif hasattr(state, "model_dump"):
        payload = state.model_dump()
    elif isinstance(state, dict):
        payload = dict(state)
    else:
        payload = {"value": str(state)}

    if ca_number:
        payload["ca_number"] = ca_number
    return payload


@app.post("/ask")
def ask_agent(
    data: QuestionRequest,
    x_internal_token: Optional[str] = Header(default=None),
):
    if not x_internal_token or not secrets.compare_digest(
        x_internal_token, INTERNAL_TOKEN
    ):
        raise HTTPException(status_code=401, detail="invalid_internal_token")

    try:
        final_time_stamp = datetime.now(ZoneInfo("Asia/Bangkok")).isoformat()
        response = chatbot.chat(
            user_input=data.question,
            session_id=data.session_id,
            time_stamp=final_time_stamp,
            ca_number=data.ca_number,
            pdpa_consent=data.pdpa_consent,
            user_message_id=data.user_message_id,
        )
        return {
            "answer": getattr(response, "answer", str(response)),
            "state": _state_payload(
                getattr(response, "current_state", None),
                ca_number=data.ca_number,
            ),
            "used_timestamp": final_time_stamp,
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
