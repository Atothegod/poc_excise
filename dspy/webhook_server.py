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
acked_notification_keys = defaultdict(set)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health():
    return {"status": "ok"}


class QuestionRequest(BaseModel):
    question: str
    session_id: str
    ca_number: Optional[str] = None
    pdpa_consent: bool = False
    time_stamp: Optional[str] = None


# --- โมเดลสำหรับรับข้อมูลแจ้งเตือนเชิงรุกจาก Django OMS ---
class NotificationWebhook(BaseModel):
    session_id: str = Field(..., description="ID ผู้ใช้งาน (เช่น LINE UID)")
    ca_number: str = Field(..., description="หมายเลขผู้ใช้ไฟ")
    message: str = Field(..., description="ข้อความที่ต้องการให้ Agent ส่งถึง User")
    event_type: str = Field(
        ...,
        description="ประเภทเหตุการณ์ เช่น eta_timeout, etr_update, etr_timeout_sla, closed_loop_prompt",
    )
    report_id: Optional[int] = Field(None, description="CustomerReport ID")
    case_id: Optional[str] = Field(None, description="OutageCase ID")
    notification_key: Optional[str] = Field(
        None, description="Stable identity for deduping proactive alerts"
    )
    closed_loop_kind: Optional[str] = Field(
        None, description="Subtype for closed-loop prompts, such as sla_expired"
    )


class NotificationAck(BaseModel):
    notification_key: Optional[str] = None
    notification_keys: list[str] = Field(default_factory=list)


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


def _notification_key(
    event_type,
    ca_number,
    message,
    report_id=None,
    case_id=None,
    notification_key=None,
):
    if notification_key:
        return str(notification_key)

    parts = [event_type, ca_number]
    if report_id or case_id:
        parts.extend([report_id, case_id])
    parts.append(message)
    return "|".join(str(part or "") for part in parts)


def _notification_key_for_data(data):
    return _notification_key(
        data.event_type,
        data.ca_number,
        data.message,
        report_id=data.report_id,
        case_id=data.case_id,
        notification_key=data.notification_key,
    )


def _notification_key_for_item(item, fallback_ca_number=None):
    item_message = item.get("message") or item.get("content") or ""
    item_message = str(item_message)
    if item_message.startswith("event_type=") and "; message=" in item_message:
        item_message = item_message.split("; message=", 1)[-1]

    return _notification_key(
        item.get("event_type"),
        item.get("ca_number") or fallback_ca_number,
        item_message,
        report_id=item.get("report_id"),
        case_id=item.get("case_id"),
        notification_key=item.get("notification_key"),
    )


def _has_pending_notification(session_id, data):
    incoming_key = _notification_key_for_data(data)
    return any(
        _notification_key_for_item(item, fallback_ca_number=data.ca_number)
        == incoming_key
        for item in pending_notifications[session_id]
    )


def _history_has_recent_notification(history_list, data):
    incoming_key = _notification_key_for_data(data)
    for item in reversed(history_list[-5:]):
        if (
            _notification_key_for_item(item, fallback_ca_number=data.ca_number)
            == incoming_key
        ):
            return True
    return False


def _notification_record(data):
    notification_key = _notification_key_for_data(data)
    return {
        "message": data.message,
        "event_type": data.event_type,
        "ca_number": data.ca_number,
        "report_id": data.report_id,
        "case_id": data.case_id,
        "notification_key": notification_key,
        "closed_loop_kind": data.closed_loop_kind,
    }


def _notification_key_for_record(notification):
    return notification.get("notification_key") or _notification_key(
        notification.get("event_type"),
        notification.get("ca_number"),
        notification.get("message"),
        report_id=notification.get("report_id"),
        case_id=notification.get("case_id"),
    )


def _pending_notifications_for_session(session_id):
    acked_keys = acked_notification_keys[session_id]
    return [
        notification
        for notification in pending_notifications[session_id]
        if _notification_key_for_record(notification) not in acked_keys
    ]


def _latest_closed_loop_notification_from_db(session_id):
    context = fetch_session_context(session_id)
    if not context:
        return None

    history = context.get("chat_history") or []
    if not history:
        return None

    latest_item = history[-1]
    if latest_item.get("event_type") != "closed_loop_prompt":
        return None

    message = latest_item.get("message") or latest_item.get("content") or ""
    if not message:
        return None

    ca_number = latest_item.get("ca_number") or context.get("ca_number")
    report_id = latest_item.get("report_id") or context.get("report_id")
    latest_outage = context.get("latest_outage") or {}
    case_id = latest_item.get("case_id") or latest_outage.get("case_id")
    notification_key = latest_item.get("notification_key") or _notification_key(
        latest_item.get("event_type"),
        ca_number,
        message,
        report_id=report_id,
        case_id=case_id,
    )

    return {
        "message": message,
        "event_type": latest_item.get("event_type"),
        "ca_number": ca_number,
        "report_id": report_id,
        "case_id": case_id,
        "notification_key": notification_key,
        "closed_loop_kind": latest_item.get("closed_loop_kind"),
        "source": "chat_history",
    }


def _merge_latest_closed_loop_fallback(session_id, notifications):
    fallback = _latest_closed_loop_notification_from_db(session_id)
    if not fallback:
        return notifications

    fallback_key = _notification_key_for_record(fallback)
    if any(_notification_key_for_record(item) == fallback_key for item in notifications):
        return notifications

    return notifications + [fallback]


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
            ca_number=data.ca_number,
            pdpa_consent=data.pdpa_consent,
        )

        return {
            "answer": getattr(response, "answer", str(response)),
            "state": _state_payload(
                getattr(response, "current_state", None),
                ca_number=data.ca_number,
            ),
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

        notification = _notification_record(data)
        if not _has_pending_notification(data.session_id, data):
            pending_notifications[data.session_id].append(notification)

        # บันทึกประวัติลง Memory ของ Agent (State Cleansing & Updates)
        # เพื่อให้ AI รู้ว่ามีข้อความระบบส่งหาลูกค้าแล้ว จะได้คุยต่อถูกบริบท
        history_list = chatbot._get_or_create_session(data.session_id)
        if not _history_has_recent_notification(history_list, data):
            history_list.append(
                {
                    "role": "System Alert (OMS)",
                    "message": data.message,
                    "timestamp": datetime.now(ZoneInfo("Asia/Bangkok")).isoformat(),
                    "event_type": data.event_type,
                    "ca_number": data.ca_number,
                    "report_id": data.report_id,
                    "case_id": data.case_id,
                    "notification_key": notification["notification_key"],
                    "closed_loop_kind": data.closed_loop_kind,
                    "content": (
                        f"event_type={data.event_type}; "
                        f"closed_loop_kind={data.closed_loop_kind}; "
                        f"ca_number={data.ca_number}; "
                        f"report_id={data.report_id}; "
                        f"case_id={data.case_id}; "
                        f"notification_key={notification['notification_key']}; "
                        f"message={data.message}"
                    ),
                }
            )
            sync_chat_history_to_db(data.session_id, history_list)
        context = fetch_session_context(data.session_id, ca_number=data.ca_number)
        if context:
            restore_latest_outage(data.session_id, context.get("latest_outage"))
        else:
            restore_latest_outage(
                data.session_id,
                {
                    "event_type": data.event_type,
                    "ca_number": data.ca_number,
                    "report_id": data.report_id,
                    "case_id": data.case_id,
                },
            )

        return {"status": "success", "message": "Notification pushed to user."}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/notifications/{session_id}")
async def get_notifications(session_id: str):
    notifications = _pending_notifications_for_session(session_id)
    return {"notifications": notifications}


@app.get("/notifications/{session_id}/latest-closed-loop")
async def get_latest_closed_loop_notification(session_id: str):
    notification = _latest_closed_loop_notification_from_db(session_id)
    if not notification:
        return {"notifications": []}
    return {"notifications": [notification]}


@app.post("/notifications/{session_id}/ack")
async def ack_notifications(session_id: str, data: NotificationAck):
    keys = set(data.notification_keys)
    if data.notification_key:
        keys.add(data.notification_key)
    keys = {str(key) for key in keys if key}

    if not keys:
        return {"status": "noop", "acked": 0}

    acked_notification_keys[session_id].update(keys)
    pending_notifications[session_id] = [
        notification
        for notification in pending_notifications[session_id]
        if _notification_key_for_record(notification) not in keys
    ]
    return {"status": "success", "acked": len(keys)}


@app.get("/health")
def health_check():
    return {"status": "healthy"}
