from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime
from zoneinfo import ZoneInfo
from collections import defaultdict

# 1. Import the stateful 'chatbot' instance you created in agent.py
from agent import chatbot  # (สมมติว่าไฟล์คลาส MemoryAgent ของคุณชื่อ core_agent)
from tools import fetch_session_context, restore_latest_outage, sync_chat_history_to_db

app = FastAPI(title="DSPy Agent Webhook Server")
pending_notifications = defaultdict(list)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class QuestionRequest(BaseModel):
    question: str
    session_id: str
    time_stamp: Optional[str] = None


# --- โมเดลสำหรับรับข้อมูลแจ้งเตือนเชิงรุกจาก Django OMS ---
class NotificationWebhook(BaseModel):
    session_id: str = Field(..., description="ID ผู้ใช้งาน (เช่น LINE UID)")
    ca_number: str = Field(..., description="หมายเลขผู้ใช้ไฟ")
    message: str = Field(..., description="ข้อความที่ต้องการให้ Agent ส่งถึง User")
    event_type: str = Field(
        ..., description="ประเภทเหตุการณ์ เช่น eta_timeout, etr_update, closed_loop"
    )


@app.post("/ask")
async def ask_agent(data: QuestionRequest):
    try:
        # Use server-side Thailand time as the single source of truth.
        # Client-provided timestamps can be spoofed or simply wrong.
        final_time_stamp = datetime.now(ZoneInfo("Asia/Bangkok")).isoformat()

        response = chatbot.chat(
            user_input=data.question,
            session_id=data.session_id,
            time_stamp=final_time_stamp,
        )

        return {
            "answer": getattr(response, "answer", str(response)),
            "state": getattr(response, "current_state", None),
            "used_timestamp": final_time_stamp,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# -----------------------------------------------------------------
# [เพิ่ม Endpoint ใหม่] สำหรับรับสัญญาณจากระบบจับเวลา (Celery)
# -----------------------------------------------------------------
@app.post("/webhook/notify")
async def receive_proactive_notification(data: NotificationWebhook):
    """
    รับข้อมูลจาก OMS เมื่อเกิดเหตุการณ์ที่ต้องแจ้งเตือนลูกค้าทันที (Asynchronous)
    เช่น หมดเวลา ETA, มีการเลื่อน ETR, หรือช่างแจ้งว่าจ่ายไฟเรียบร้อยแล้ว
    """
    try:
        # ในระบบจริง ตรงนี้คุณจะต้องนำ data.session_id (เช่น LINE UID)
        # ไปใช้ยิง LINE Messaging API: Push Message

        # จำลองการส่งข้อความ
        print("\n" + "=" * 50)
        print(f"🚨 [PROACTIVE ALERT] ส่งข้อความหา User: {data.session_id}")
        print(f"📝 สาเหตุ: {data.event_type}")
        print(f"💬 ข้อความ: {data.message}")
        print("=" * 50 + "\n")

        pending_notifications[data.session_id].append(
            {
                "message": data.message,
                "event_type": data.event_type,
                "ca_number": data.ca_number,
            }
        )

        # บันทึกประวัติลง Memory ของ Agent (State Cleansing & Updates)
        # เพื่อให้ AI รู้ว่ามีข้อความระบบส่งหาลูกค้าแล้ว จะได้คุยต่อถูกบริบท
        history_list = chatbot._get_or_create_session(data.session_id)
        history_list.append(
            {
                "role": "System Alert (OMS)",
                "message": data.message,
                "timestamp": datetime.now(ZoneInfo("Asia/Bangkok")).isoformat(),
                "event_type": data.event_type,
                "content": f"event_type={data.event_type}; ca_number={data.ca_number}; message={data.message}",
            }
        )
        sync_chat_history_to_db(data.session_id, history_list)
        context = fetch_session_context(data.session_id)
        if context:
            restore_latest_outage(data.session_id, context.get("latest_outage"))

        return {"status": "success", "message": "Notification pushed to user."}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/notifications/{session_id}")
async def get_notifications(session_id: str):
    notifications = pending_notifications.pop(session_id, [])
    return {"notifications": notifications}


@app.get("/health")
def health_check():
    return {"status": "healthy"}
