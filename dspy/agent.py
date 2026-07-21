# 1. IMPORT CONFIG FIRST SO THE LM IS LOADED ENTIRELY BEFORE BUILDING THE AGENT
import config
import dspy
from datetime import datetime
from types import SimpleNamespace
from typing import Literal
from zoneinfo import ZoneInfo

# 2. Now import your components safely
from signature import PEA_Assistant, PEA_Conversation_State, PEA_Heart_Model

from agent_tools import Check_Outage_Tool
from django_client import (
    fetch_session_context,
    record_closed_loop_response,
)
from session_state import (
    current_chat_history,
    current_closed_loop_recorded,
    current_conversation_state,
    current_heart_history,
    current_login_ca_number,
    current_pending_closed_loop_prompt,
    current_pdpa_consent,
    current_question,
    current_session_id,
    current_time_stamp,
    current_tool_errors,
    current_tool_results,
    current_tools_use,
    get_latest_outage,
    record_tool_call,
    record_tool_error,
    record_tool_result,
    reset_tool_execution,
    restore_latest_outage,
)
from time_utils import format_time_only, parse_iso_datetime

CALM_COMMANDER_STATES = {
    "waiting_for_intent",
    "checking_outage",
    "providing_eta_first",
    "fallback_to_human",
}
EMPATHETIC_ANALYST_STATES = {
    "existing_case_providing_eta",
    "mass_outage_providing_etr",
    "eta_timeout_waiting_etr",
    "etr_timeout_sla",
    "resolved",
}
SAFE_OUTAGE_CLARIFICATION = (
    "ขอยืนยันก่อนดำเนินการนะคะ ตอนนี้สถานที่ของคุณไฟดับหรือไม่มีไฟใช้ "
    "และต้องการให้การไฟฟ้าตรวจสอบใช่ไหมคะ"
)
HeartTimeRequest = Literal[
    "none",
    "current_clock",
    "technician_arrival",
    "power_restoration",
    "service_deadline",
]
HEART_TIME_REQUESTS = {
    "none",
    "current_clock",
    "technician_arrival",
    "power_restoration",
    "service_deadline",
}


def select_heart_persona(current_state) -> str:
    if isinstance(current_state, dict):
        flow_step = current_state.get("flow_step")
        current_persona = current_state.get("heart_persona")
    else:
        flow_step = getattr(current_state, "flow_step", None)
        current_persona = getattr(current_state, "heart_persona", None)

    if flow_step in CALM_COMMANDER_STATES:
        return "calm_commander"
    if flow_step in EMPATHETIC_ANALYST_STATES:
        return "empathetic_analyst"
    if flow_step == "heart_mode":
        if current_persona in {"calm_commander", "empathetic_analyst"}:
            return current_persona
        return "empathetic_analyst"
    return "empathetic_analyst"


base_heart_model = dspy.Predict(PEA_Heart_Model, temperature=0.6)


def _heart_response_goal(current_state, request_outage_confirmation: bool) -> str:
    if request_outage_confirmation:
        return "confirm_current_outage"

    if isinstance(current_state, dict):
        flow_step = current_state.get("flow_step")
    else:
        flow_step = getattr(current_state, "flow_step", None)
    if flow_step == "resolved":
        return "acknowledge_resolution"
    return "deescalate_and_acknowledge"


def _allowed_heart_time_context(time_request: HeartTimeRequest) -> str:
    latest_outage = get_latest_outage(current_session_id.get()) or {}
    source_value = None
    fact_name = None

    if time_request == "current_clock":
        source_value = current_time_stamp.get()
        fact_name = "เวลาปัจจุบัน"
    elif time_request == "technician_arrival":
        source_value = latest_outage.get("eta_target_time")
        fact_name = "เวลาที่ช่างคาดว่าจะถึงหน้างาน"
    elif time_request == "power_restoration":
        if latest_outage.get("event_type") == "etr_timeout_sla":
            source_value = latest_outage.get("sla_target_time")
        else:
            source_value = latest_outage.get("etr_target_time") or latest_outage.get(
                "oms_etr"
            )
        fact_name = "เวลาที่คาดว่าจะจ่ายไฟคืน"
    elif time_request == "service_deadline":
        source_value = latest_outage.get("sla_target_time")
        fact_name = "กำหนดเวลาสิ้นสุดล่าสุด"

    if not fact_name:
        return "ห้ามใช้หรือกล่าวถึงข้อมูลเวลาใด ๆ ในคำตอบนี้"

    time_label = format_time_only(source_value)
    if not time_label:
        return f"ยังไม่มีข้อมูล{fact_name}ที่ยืนยันได้"
    return f"{fact_name}: {time_label}"


def heart_tool(
    question: str,
    request_outage_confirmation: bool = False,
    time_request: HeartTimeRequest = "none",
) -> str:
    """Respond naturally without accessing OMS or performing operations.

    Set request_outage_confirmation=True on the first turn that may describe a
    current outage. This asks for confirmation without checking or opening a case.
    Set time_request to a non-none category only when the latest user message
    explicitly asks for that operational time. Emotional delay statements alone
    must use none. Decide from the meaning of the complete latest message.
    """
    record_tool_call("heart_tool")
    current_state = current_conversation_state.get()
    persona = select_heart_persona(current_state)
    authoritative_question = current_question.get() or str(question or "")
    normalized_time_request = str(time_request or "none").strip().lower()
    if normalized_time_request not in HEART_TIME_REQUESTS:
        normalized_time_request = "none"
    if request_outage_confirmation:
        normalized_time_request = "none"

    time_policy = (
        "explicit_request"
        if normalized_time_request != "none"
        else "forbidden"
    )
    response_goal = _heart_response_goal(current_state, request_outage_confirmation)
    allowed_time_context = _allowed_heart_time_context(normalized_time_request)
    if request_outage_confirmation:
        # Keep the safety-critical confirmation deterministic and avoid a second
        # model call on first-contact outage turns.
        answer = SAFE_OUTAGE_CLARIFICATION
    else:
        try:
            prediction = base_heart_model(
                chat_history=current_heart_history.get(),
                question=authoritative_question,
                persona=persona,
                response_goal=response_goal,
                time_policy=time_policy,
                allowed_time_context=allowed_time_context,
            )
            answer = str(getattr(prediction, "answer", prediction) or "").strip()
            if not answer:
                raise ValueError("Heart model returned an empty answer")
        except Exception:
            record_tool_error("heart_tool")
            answer = (
                "ขออภัยค่ะ ขณะนี้ระบบไม่สามารถช่วยตอบได้ครบถ้วน "
                "กรุณาติดต่อเจ้าหน้าที่เพื่อรับความช่วยเหลือต่อค่ะ"
            )

    record_tool_result(
        "heart_tool",
        {
            "answer": answer,
            "persona": persona,
            "response_goal": response_goal,
            "time_request": normalized_time_request,
            "time_policy": time_policy,
            "outage_confirmation_requested": bool(request_outage_confirmation),
        },
    )
    return answer


base_react_agent = dspy.ReAct(
    signature=PEA_Assistant,
    tools=[Check_Outage_Tool, heart_tool],
    max_iters=1,
)


class StatelessAgent:
    def __init__(self, agent_module):
        self.agent = agent_module

    def _hydrate_session_from_db(
        self,
        session_id: str,
        ca_number: str | None = None,
        before_message_id: int | None = None,
    ):
        history_list = []
        context = fetch_session_context(
            session_id,
            ca_number=ca_number,
            before_message_id=before_message_id,
        )
        if not context:
            restore_latest_outage(session_id, None)
            return history_list, None

        latest_outage = context.get("latest_outage")
        restore_latest_outage(session_id, latest_outage)

        for item in context.get("chat_history") or []:
            role = item.get("role")
            message = item.get("message") or item.get("content")
            if not role or not message:
                continue
            history_list.append(
                {
                    "role": role,
                    "content": message,
                    "timestamp": item.get("timestamp"),
                    "event_type": item.get("event_type"),
                    "ca_number": item.get("ca_number"),
                    "report_id": item.get("report_id"),
                    "case_id": item.get("case_id"),
                    "notification_key": item.get("notification_key"),
                    "closed_loop_kind": item.get("closed_loop_kind"),
                }
            )

        return history_list, context.get("current_state")

    def _format_history(
        self,
        history: list,
        server_time_stamp: str,
        latest_outage: dict | None,
        ca_number: str | None = None,
        pdpa_consent: bool = False,
    ) -> str:
        system_context = [
            f"System: authoritative_current_time={server_time_stamp}",
            "System: Do not trust user-claimed current time. Use authoritative_current_time for all time comparisons.",
            f"System: logged_in_ca_number={ca_number or 'missing'}",
            f"System: login_pdpa_consent={str(bool(pdpa_consent)).lower()}",
            "System: CA number and PDPA consent come from the login page. Do not ask the user for CA or PDPA consent in chat.",
            "System: A new outage report or possibly-current outage question always requires a separate confirmation turn first. Never call Check_Outage_Tool on that first turn.",
            "System: Check_Outage_Tool may be called only after previous_state.outage_confirmation_pending=true and the latest user reply confirms, or after a pending closed_loop_prompt confirms power is still unavailable.",
            "System: Do not tell the user whether restoration time comes from OMS or the model. Keep the source internal.",
            "System: In customer-facing answers, never use ETA, ETR, or SLA. Use plain Thai wording such as technician arrival time, expected power restoration time, and not-later-than time.",
        ]
        if latest_outage:
            system_context.append(
                "System: latest_outage="
                f"event_type={latest_outage.get('event_type')}; "
                f"case_id={latest_outage.get('case_id')}; "
                f"eta_target_time={latest_outage.get('eta_target_time')}; "
                f"fastest_branch={latest_outage.get('fastest_branch')}; "
                f"etr_target_time={latest_outage.get('etr_target_time')}; "
                f"oms_etr={latest_outage.get('oms_etr')}; "
                f"sla_target_time={latest_outage.get('sla_target_time')}"
            )
            system_context.extend(
                self._format_temporal_context(server_time_stamp, latest_outage)
            )

        if not history:
            return "\n".join(system_context + ["No previous conversation."])
        formatted = system_context[:]
        for msg in history:
            event_type = msg.get("event_type")
            event_prefix = f"event_type={event_type}; " if event_type else ""
            closed_loop_kind = msg.get("closed_loop_kind")
            if closed_loop_kind:
                event_prefix += f"closed_loop_kind={closed_loop_kind}; "
            formatted.append(f"{msg['role']}: {event_prefix}{msg['content']}")
        return "\n".join(formatted)

    def _format_heart_history(self, history: list) -> str:
        """Give HEART conversational context without operational facts or old replies.

        The main ReAct agent still receives the complete transcript. HEART receives
        only prior customer turns, so an old agent/OMS estimate cannot become an
        accidental source for a response that did not ask about time.
        """
        customer_turns = []
        for message in history[-12:]:
            if str(message.get("role") or "").strip().lower() != "user":
                continue
            content = str(message.get("content") or "").strip()
            if content:
                customer_turns.append(f"Customer: {content}")

        if not customer_turns:
            return "No previous customer turns."
        return "\n".join(customer_turns)

    def _format_thai_time(self, value: datetime | None) -> str | None:
        if not value:
            return None
        bangkok_time = value.astimezone(ZoneInfo("Asia/Bangkok"))
        return bangkok_time.strftime("%H:%M น.")

    def _format_user_time_label(
        self, target_time: datetime | None, now: datetime | None
    ) -> str | None:
        return self._format_thai_time(target_time)

    def _format_temporal_context(
        self, server_time_stamp: str, latest_outage: dict
    ) -> list[str]:
        now = datetime.fromisoformat(server_time_stamp)
        eta = parse_iso_datetime(latest_outage.get("eta_target_time"))
        etr = parse_iso_datetime(
            latest_outage.get("etr_target_time") or latest_outage.get("oms_etr")
        )
        sla = parse_iso_datetime(latest_outage.get("sla_target_time"))

        return [
            f"System: current_time_thai_label={self._format_thai_time(now)}",
            f"System: latest_eta_thai_label={self._format_thai_time(eta)}",
            f"System: latest_eta_user_label={self._format_thai_time(eta)}",
            f"System: latest_etr_thai_label={self._format_thai_time(etr)}",
            f"System: latest_etr_user_label={self._format_user_time_label(etr, now)}",
            f"System: latest_sla_thai_label={self._format_thai_time(sla)}",
            f"System: latest_sla_user_label={self._format_user_time_label(sla, now)}",
            "System: event_type=eta_timeout is an OMS/Celery event. Do not say the technician arrival estimate is past unless chat_history contains event_type=eta_timeout.",
            "System: If asked about current time, answer naturally using current_time_thai_label.",
            "System: If asked about technician arrival before eta_timeout event, answer naturally using latest_eta_user_label.",
            "System: If asked about restoration time, answer naturally using latest_etr_user_label only if it is available; do not mention the ETR source.",
            "System: If event_type=etr_timeout_sla, answer naturally using latest_sla_user_label when available.",
        ]

    def _latest_pending_closed_loop_prompt(self, history: list):
        for index in range(len(history) - 1, -1, -1):
            if history[index].get("event_type") == "closed_loop_prompt":
                if index == len(history) - 1:
                    return history[index]
                return None
        return None

    def _state_payload(self, state) -> dict:
        if state is None:
            return {}
        if hasattr(state, "model_dump"):
            return state.model_dump()
        if hasattr(state, "dict"):
            return state.dict()
        if isinstance(state, dict):
            return dict(state)
        return {}

    def _validated_previous_state(
        self,
        raw_state,
        ca_number: str | None,
        latest_outage: dict | None,
    ) -> PEA_Conversation_State:
        payload = self._state_payload(raw_state)
        if payload.get("flow_step") == "out_of_scope":
            payload["flow_step"] = "heart_mode"
            payload["heart_persona"] = payload.get("heart_persona")
        payload["ca_number"] = ca_number

        try:
            state = PEA_Conversation_State(**payload)
        except Exception:
            state = PEA_Conversation_State(ca_number=ca_number)

        event_flow_steps = {
            "mass_outage": "mass_outage_providing_etr",
            "repeated_event": "mass_outage_providing_etr",
            "eta_timeout": "eta_timeout_waiting_etr",
            "etr_timeout_sla": "etr_timeout_sla",
        }
        event_flow_step = event_flow_steps.get((latest_outage or {}).get("event_type"))
        if event_flow_step:
            payload = self._state_payload(state)
            payload["flow_step"] = event_flow_step
            payload["heart_persona"] = None
            payload["outage_confirmation_pending"] = False
            state = PEA_Conversation_State(**payload)
        return state

    def _response_from_check_outage_result(
        self, tool_result: str, ca_number: str | None
    ):
        result = str(tool_result or "").strip()
        flow_step = "checking_outage"
        is_mass_outage = None
        outage_confirmation_pending = False

        if result.startswith("[OUTAGE_CONFIRMATION_REQUIRED]"):
            answer = SAFE_OUTAGE_CLARIFICATION
            flow_step = "heart_mode"
            outage_confirmation_pending = True
        elif result.startswith("[เหตุแจ้งใหม่]"):
            detail = (
                result.split("แจ้งว่า", 1)[1].strip()
                if "แจ้งว่า" in result
                else result.split("]", 1)[-1].strip()
            )
            answer = f"รับทราบค่ะ เปิดใบงานใหม่ให้แล้วค่ะ {detail}"
            flow_step = "providing_eta_first"
            is_mass_outage = False
        elif result.startswith("[เคสเดิมของ CA]"):
            detail = result.split("]", 1)[-1].strip()
            if detail.startswith("แจ้งว่า"):
                detail = detail[len("แจ้งว่า") :].strip()
            elif detail.startswith("แจ้ง"):
                detail = detail[len("แจ้ง") :].strip()
            answer = f"พบเคสที่เปิดอยู่สำหรับ CA นี้ค่ะ {detail}"
            flow_step = "existing_case_providing_eta"
            case_type = (get_latest_outage(current_session_id.get()) or {}).get(
                "case_type"
            )
            if case_type:
                is_mass_outage = case_type == "mass_outage"
        elif result.startswith("[เหตุวงกว้าง]"):
            answer = result.split("]", 1)[-1].strip()
            flow_step = "mass_outage_providing_etr"
            is_mass_outage = True
        elif result.startswith("[อัปเดตการจ่ายไฟ]"):
            answer = result.split("]", 1)[-1].strip()
            if answer.startswith("แจ้งว่า"):
                answer = answer[len("แจ้งว่า") :].strip()
            flow_step = "etr_timeout_sla"
        elif result.startswith(
            ("[CA_INVALID]", "[CONSENT_REQUIRED]", "[CA_NOT_FOUND]")
        ):
            answer = result.split("]", 1)[-1].strip()
            flow_step = "fallback_to_human"
        elif result.startswith("[FallBack]") or result.startswith("ขัดข้อง"):
            answer = (
                "ขออภัยในความไม่สะดวกค่ะ ระบบขัดข้องไม่สามารถดำเนินการต่อได้ "
                "กำลังโอนสายให้เจ้าหน้าที่เพื่อช่วยเหลือต่อไปค่ะ"
            )
            flow_step = "fallback_to_human"
            outage_confirmation_pending = True
        else:
            answer = result

        return SimpleNamespace(
            answer=answer,
            current_state=PEA_Conversation_State(
                ca_number=ca_number,
                flow_step=flow_step,
                is_mass_outage=is_mass_outage,
                outage_confirmation_pending=outage_confirmation_pending,
            ),
        )

    def _response_flow_step(self, response) -> str | None:
        current_state = getattr(response, "current_state", None)
        if isinstance(current_state, dict):
            return current_state.get("flow_step")
        return getattr(current_state, "flow_step", None)

    def _response_tools_use(self, response) -> list[str]:
        current_state = getattr(response, "current_state", None)
        if isinstance(current_state, dict):
            return list(current_state.get("tools_use") or [])
        return list(getattr(current_state, "tools_use", None) or [])

    def _set_response(self, response, answer: str, state: PEA_Conversation_State):
        if hasattr(response, "answer"):
            response.answer = answer
            response.current_state = state
            return response
        return SimpleNamespace(answer=answer, current_state=state)

    def _fallback_response(self, ca_number: str | None):
        return SimpleNamespace(
            answer=(
                "ขออภัยในความไม่สะดวกค่ะ ระบบไม่สามารถดำเนินการต่อได้ "
                "กรุณาติดต่อเจ้าหน้าที่เพื่อรับความช่วยเหลือต่อค่ะ"
            ),
            current_state=PEA_Conversation_State(
                ca_number=ca_number,
                flow_step="fallback_to_human",
                tools_use=list(current_tools_use.get()),
            ),
        )

    def _normalize_tool_response(
        self,
        response,
        ca_number: str | None,
        pending_closed_loop_prompt: dict | None,
        allow_heart_fallback: bool,
    ):
        tools_use = list(current_tools_use.get())
        if not tools_use and allow_heart_fallback:
            direct_answer = str(getattr(response, "answer", "") or "").strip()
            if direct_answer and not pending_closed_loop_prompt:
                state = PEA_Conversation_State(
                    ca_number=ca_number,
                    flow_step="waiting_for_intent",
                    tools_use=[],
                    heart_persona=None,
                    outage_confirmation_pending=False,
                )
                return self._set_response(response, direct_answer, state)

            heart_tool(current_question.get())

        tools_use = list(current_tools_use.get())
        tool_results = dict(current_tool_results.get() or {})
        tool_errors = set(current_tool_errors.get())

        if "Check_Outage_Tool" in tools_use:
            checked = self._response_from_check_outage_result(
                tool_results.get("Check_Outage_Tool"),
                ca_number,
            )
            # The tool result is authoritative for case status and all times.
            # ReAct's final extraction must never paraphrase those values.
            answer = checked.answer
            state_payload = self._state_payload(checked.current_state)
            state_payload["tools_use"] = tools_use
            state_payload["heart_persona"] = None
            state = PEA_Conversation_State(**state_payload)
            return self._set_response(response, answer, state)

        if "heart_tool" in tools_use:
            heart_result = tool_results.get("heart_tool") or {}
            answer = str(heart_result.get("answer") or "").strip()
            if "heart_tool" in tool_errors:
                state = PEA_Conversation_State(
                    ca_number=ca_number,
                    flow_step="fallback_to_human",
                    tools_use=tools_use,
                    outage_confirmation_pending=False,
                )
                return self._set_response(response, answer, state)

            proposed_flow_step = self._response_flow_step(response)
            if pending_closed_loop_prompt and proposed_flow_step == "resolved":
                if not current_closed_loop_recorded.get():
                    record_closed_loop_response(
                        "resolved",
                        ca_number=ca_number,
                        report_id=pending_closed_loop_prompt.get("report_id"),
                    )
                    current_closed_loop_recorded.set(True)
                state = PEA_Conversation_State(
                    ca_number=ca_number,
                    flow_step="resolved",
                    tools_use=tools_use,
                    outage_confirmation_pending=False,
                )
            else:
                state = PEA_Conversation_State(
                    ca_number=ca_number,
                    flow_step="heart_mode",
                    tools_use=tools_use,
                    heart_persona=heart_result.get("persona"),
                    outage_confirmation_pending=bool(
                        heart_result.get("outage_confirmation_requested")
                    ),
                )
            return self._set_response(response, answer, state)

        return self._fallback_response(ca_number)

    def _ensure_feminine_ending(self, response):
        answer = str(getattr(response, "answer", response) or "").strip()
        if not answer:
            answer = "ขออภัยค่ะ ระบบไม่สามารถตอบกลับได้ในขณะนี้ค่ะ"

        if answer.endswith("ครับ"):
            answer = answer[: -len("ครับ")].rstrip()
            question_endings = ("ไหม", "หรือไม่", "หรือเปล่า", "ใช่ไหม")
            answer = f"{answer}{'คะ' if answer.endswith(question_endings) else 'ค่ะ'}"
        elif not answer.endswith(("ค่ะ", "คะ")):
            if answer.endswith("น."):
                answer = f"{answer} ค่ะ"
            else:
                answer = f"{answer.rstrip(' .!?…。！？')}ค่ะ"

        if hasattr(response, "answer"):
            response.answer = answer
            return response
        return SimpleNamespace(answer=answer, current_state=None)

    def _ensure_mass_outage_wording(self, response, session_id: str):
        if "Check_Outage_Tool" not in self._response_tools_use(response):
            return response

        latest_outage = get_latest_outage(session_id)
        if not latest_outage or latest_outage.get("event_type") != "mass_outage":
            return response

        answer = str(getattr(response, "answer", response) or "")
        if "ไฟดับวงกว้าง" in answer:
            return response

        etr = parse_iso_datetime(
            latest_outage.get("etr_target_time") or latest_outage.get("oms_etr")
        )
        etr_label = self._format_thai_time(etr)
        if etr_label:
            answer = (
                "ขณะนี้เกิดเหตุไฟดับวงกว้างในพื้นที่ค่ะ "
                f"คาดว่าจะจ่ายไฟคืนประมาณ {etr_label} ค่ะ"
            )
        else:
            answer = (
                "ขณะนี้เกิดเหตุไฟดับวงกว้างในพื้นที่ค่ะ "
                "ระบบกำลังประเมินเวลาไฟกลับล่าสุดค่ะ"
            )

        if hasattr(response, "answer"):
            response.answer = answer
            return response
        return SimpleNamespace(answer=answer, current_state=None)

    def _ensure_branch_wording(self, response, session_id: str):
        if "Check_Outage_Tool" not in self._response_tools_use(response):
            return response

        latest_outage = get_latest_outage(session_id)
        if not latest_outage:
            return response

        branch = latest_outage.get("fastest_branch")
        if not branch or latest_outage.get("event_type") not in {
            "new_event",
            "existing_ca_case",
            "active_case_exists",
        }:
            return response

        answer = str(getattr(response, "answer", response) or "").strip()
        if not answer:
            return response

        legacy_branch_prefix = "สาขาที่ประเมินว่าไปถึงเร็วที่สุด" + "คือ"
        legacy_branch_sentence = f"{legacy_branch_prefix} {branch}"
        if legacy_branch_sentence in answer:
            answer = answer.replace(
                legacy_branch_sentence,
                f"{branch} รับเรื่องแล้วค่ะ",
            )
            answer = answer.replace("ค่ะค่ะ", "ค่ะ")
        elif legacy_branch_prefix in answer:
            answer = answer.replace(legacy_branch_prefix, "")

        if branch in answer:
            if "รับเรื่องแล้ว" not in answer:
                answer = answer.replace(branch, f"{branch} รับเรื่องแล้วค่ะ", 1)
            if hasattr(response, "answer"):
                response.answer = answer
                return response
            return SimpleNamespace(answer=answer, current_state=None)

        current_state = getattr(response, "current_state", None)
        flow_step = getattr(current_state, "flow_step", None)
        if flow_step not in {
            "providing_eta_first",
            "existing_case_providing_eta",
            "checking_outage",
        }:
            return response

        branch_sentence = f"{branch} รับเรื่องแล้วค่ะ"

        answer = f"{branch_sentence} {answer}"
        if hasattr(response, "answer"):
            response.answer = answer
            return response
        return SimpleNamespace(answer=answer, current_state=None)

    def chat(
        self,
        user_input: str,
        session_id: str,
        time_stamp: str,
        ca_number: str | None = None,
        pdpa_consent: bool = False,
        user_message_id: int | None = None,
    ):
        # 4. ยัดข้อมูลใส่กระเป๋าทะลุมิติก่อนเริ่มคุย!
        current_session_id.set(session_id)
        current_time_stamp.set(time_stamp)
        current_login_ca_number.set(ca_number)
        current_pdpa_consent.set(bool(pdpa_consent))

        history_list, persisted_state = self._hydrate_session_from_db(
            session_id,
            ca_number=ca_number,
            before_message_id=user_message_id,
        )
        pending_closed_loop_prompt = self._latest_pending_closed_loop_prompt(
            history_list
        )
        latest_outage = get_latest_outage(session_id)
        previous_state = self._validated_previous_state(
            persisted_state,
            ca_number,
            latest_outage,
        )

        history_str = self._format_history(
            history_list,
            time_stamp,
            latest_outage,
            ca_number=ca_number,
            pdpa_consent=pdpa_consent,
        )
        heart_history_str = self._format_heart_history(history_list)

        reset_tool_execution()
        current_conversation_state.set(previous_state)
        current_chat_history.set(history_str)
        current_heart_history.set(heart_history_str)
        current_question.set(user_input)
        current_pending_closed_loop_prompt.set(pending_closed_loop_prompt)

        agent_failed = False
        try:
            response = self.agent(
                chat_history=history_str,
                question=user_input,
                time_stamp=time_stamp,
                previous_state=previous_state,
            )
        except Exception:
            agent_failed = True
            tool_results = dict(current_tool_results.get() or {})
            if "Check_Outage_Tool" in tool_results:
                response = self._response_from_check_outage_result(
                    tool_results["Check_Outage_Tool"],
                    ca_number,
                )
            elif "heart_tool" in tool_results:
                response = SimpleNamespace(
                    answer=tool_results["heart_tool"].get("answer", ""),
                    current_state=None,
                )
            else:
                response = self._fallback_response(ca_number)

        response = self._normalize_tool_response(
            response,
            ca_number,
            pending_closed_loop_prompt,
            allow_heart_fallback=not agent_failed,
        )

        response = self._ensure_mass_outage_wording(response, session_id)
        response = self._ensure_branch_wording(response, session_id)
        response = self._ensure_feminine_ending(response)

        return response


chatbot = StatelessAgent(base_react_agent)
