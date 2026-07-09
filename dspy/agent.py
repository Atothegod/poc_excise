# 1. IMPORT CONFIG FIRST SO THE LM IS LOADED ENTIRELY BEFORE BUILDING THE AGENT
import config
import dspy
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

# 2. Now import your components safely
from signature import PEA_Assistant, PEA_Conversation_State

from agent_tools import Check_Outage_Tool
from django_client import (
    fetch_session_context,
    record_closed_loop_response,
    sync_chat_history_to_db,
)
from session_state import (
    current_login_ca_number,
    current_pdpa_consent,
    current_session_id,
    current_time_stamp,
    get_latest_outage,
    restore_latest_outage,
)
from time_utils import parse_iso_datetime

# 3. Initialize your ReAct agent
base_react_agent = dspy.ReAct(
    signature=PEA_Assistant,
    tools=[Check_Outage_Tool],
    max_iters=5,
)


class MemoryAgent:
    def __init__(self, agent_module):
        self.agent = agent_module
        self.sessions = {}

    def _get_or_create_session(self, session_id: str) -> list:
        if session_id not in self.sessions:
            self.sessions[session_id] = []
        return self.sessions[session_id]

    def _hydrate_session_from_db(self, session_id: str, ca_number: str | None = None):
        history_list = self._get_or_create_session(session_id)

        context = fetch_session_context(session_id, ca_number=ca_number)
        if not context:
            return history_list

        latest_outage = context.get("latest_outage")
        if latest_outage:
            restore_latest_outage(session_id, latest_outage)

        if not history_list:
            restored_history = []
            for item in context.get("chat_history") or []:
                role = item.get("role")
                message = item.get("message")
                if not role or not message:
                    continue
                restored_history.append(
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
            if restored_history:
                self.sessions[session_id] = restored_history
                history_list = restored_history

        return history_list

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
            "System: When outage intent or an outage status question is clear and login_pdpa_consent=true, call Check_Outage_Tool using logged_in_ca_number and pdpa_consent=True.",
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

    def _has_pending_closed_loop_prompt(self, history: list) -> bool:
        return self._latest_pending_closed_loop_prompt(history) is not None

    def _is_still_without_power(self, user_input: str) -> bool:
        normalized_input = user_input.strip().lower()
        still_out_keywords = [
            "ยังไม่มีไฟ",
            "ไฟยังไม่มา",
            "ยังใช้งานไม่ได้",
            "ไฟไม่มา",
            "ไฟยังดับ",
            "ยังดับ",
        ]
        return any(keyword in normalized_input for keyword in still_out_keywords)

    def _closed_loop_resolution_response(
        self, user_input: str, history: list, ca_number: str | None
    ):
        prompt = self._latest_pending_closed_loop_prompt(history)
        if not prompt:
            return None

        normalized_input = user_input.strip().lower()
        resolved_keywords = [
            "ใช้งานได้แล้ว",
            "ไฟมาแล้ว",
            "ใช้ได้แล้ว",
            "มาแล้ว",
            "เรียบร้อย",
        ]
        if not any(keyword in normalized_input for keyword in resolved_keywords):
            return None

        record_closed_loop_response(
            "resolved",
            ca_number=ca_number,
            report_id=prompt.get("report_id"),
        )
        return SimpleNamespace(
            answer=(
                "ขอบคุณที่แจ้งยืนยันค่ะ ดีใจที่ไฟกลับมาใช้งานได้ตามปกติแล้ว "
                "หากพบเหตุขัดข้องเพิ่มเติม สามารถแจ้งผ่านช่องทางนี้ได้เลยค่ะ"
            ),
            current_state=PEA_Conversation_State(
                ca_number=ca_number,
                flow_step="resolved",
            ),
        )

    def _response_from_check_outage_result(
        self, tool_result: str, ca_number: str | None
    ):
        result = str(tool_result or "").strip()
        flow_step = "checking_outage"

        if result.startswith("[เหตุแจ้งใหม่]"):
            detail = (
                result.split("แจ้งว่า", 1)[1].strip()
                if "แจ้งว่า" in result
                else result.split("]", 1)[-1].strip()
            )
            answer = f"รับทราบค่ะ เปิดใบงานใหม่ให้แล้วค่ะ {detail}"
            flow_step = "providing_eta_first"
        elif result.startswith("[เคสเดิมของ CA]"):
            detail = result.split("]", 1)[-1].strip()
            if detail.startswith("แจ้งว่า"):
                detail = detail[len("แจ้งว่า") :].strip()
            elif detail.startswith("แจ้ง"):
                detail = detail[len("แจ้ง") :].strip()
            answer = f"พบเคสที่เปิดอยู่สำหรับ CA นี้ค่ะ {detail}"
            flow_step = "existing_case_providing_eta"
        elif result.startswith("[เหตุวงกว้าง]"):
            answer = result.split("]", 1)[-1].strip()
            flow_step = "mass_outage_providing_etr"
        elif result.startswith("[CA_INVALID]") or result.startswith("[CONSENT_REQUIRED]"):
            answer = result.split("]", 1)[-1].strip()
            flow_step = "fallback_to_human"
        elif result.startswith("[FallBack]") or result.startswith("ขัดข้อง"):
            answer = (
                "ขออภัยในความไม่สะดวกค่ะ ระบบขัดข้องไม่สามารถดำเนินการต่อได้ "
                "กำลังโอนสายให้เจ้าหน้าที่เพื่อช่วยเหลือต่อไปค่ะ"
            )
            flow_step = "fallback_to_human"
        else:
            answer = result

        return SimpleNamespace(
            answer=answer,
            current_state=PEA_Conversation_State(
                ca_number=ca_number,
                flow_step=flow_step,
            ),
        )

    def _closed_loop_still_out_response(self, ca_number: str | None, prompt=None):
        record_closed_loop_response(
            "still_out",
            ca_number=ca_number,
            report_id=(prompt or {}).get("report_id"),
        )
        tool_result = Check_Outage_Tool(
            ca_number or "", pdpa_consent=True, force_new_case=True
        )
        return self._response_from_check_outage_result(tool_result, ca_number)

    def _ensure_feminine_ending(self, response):
        answer = str(getattr(response, "answer", response) or "").strip()
        if not answer:
            answer = "ขออภัยค่ะ ระบบไม่สามารถตอบกลับได้ในขณะนี้ค่ะ"

        answer = answer.rstrip(" .!?…。！？")
        for suffix in ["ครับ", "คะ"]:
            if answer.endswith(suffix):
                answer = answer[: -len(suffix)].rstrip()
                break
        if not answer.endswith("ค่ะ"):
            answer = f"{answer}ค่ะ"

        if hasattr(response, "answer"):
            response.answer = answer
            return response
        return SimpleNamespace(answer=answer, current_state=None)

    def _ensure_mass_outage_wording(self, response, session_id: str):
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
        } and not any(
            keyword in answer
            for keyword in ["ช่างจะถึงหน้างาน", "เปิดใบงาน", "เคสที่เปิดอยู่"]
        ):
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
    ):
        # 4. ยัดข้อมูลใส่กระเป๋าทะลุมิติก่อนเริ่มคุย!
        current_session_id.set(session_id)
        current_time_stamp.set(time_stamp)
        current_login_ca_number.set(ca_number)
        current_pdpa_consent.set(bool(pdpa_consent))

        history_list = self._hydrate_session_from_db(session_id, ca_number=ca_number)
        pending_closed_loop_prompt = self._latest_pending_closed_loop_prompt(
            history_list
        )
        pending_closed_loop = pending_closed_loop_prompt is not None
        still_without_power = self._is_still_without_power(user_input)

        history_str = self._format_history(
            history_list,
            time_stamp,
            get_latest_outage(session_id),
            ca_number=ca_number,
            pdpa_consent=pdpa_consent,
        )

        response = self._closed_loop_resolution_response(
            user_input, history_list, ca_number
        )
        if response is None:
            if pending_closed_loop and still_without_power:
                response = self._closed_loop_still_out_response(
                    ca_number, prompt=pending_closed_loop_prompt
                )
            else:
                response = self.agent(
                    chat_history=history_str, question=user_input, time_stamp=time_stamp
                )
        response = self._ensure_mass_outage_wording(response, session_id)
        response = self._ensure_branch_wording(response, session_id)
        response = self._ensure_feminine_ending(response)

        history_list.append(
            {"role": "user", "content": user_input, "timestamp": time_stamp}
        )
        history_list.append(
            {
                "role": "agent",
                "content": getattr(response, "answer", str(response)),
                "timestamp": time_stamp,
            }
        )
        sync_chat_history_to_db(session_id, history_list)

        return response


chatbot = MemoryAgent(base_react_agent)
