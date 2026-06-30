# 1. IMPORT CONFIG FIRST SO THE LM IS LOADED ENTIRELY BEFORE BUILDING THE AGENT
import config
import dspy
from datetime import datetime
from math import ceil
from zoneinfo import ZoneInfo

# 2. Now import your components safely
from signature import PEA_Assistant

# นำเข้าตัวแปรทะลุมิติมาด้วยขอรับ!
from tools import (
    Check_Outage_Tool,
    Fast_Track_Tool,
    current_login_ca_number,
    current_pdpa_consent,
    current_session_id,
    current_time_stamp,
    fetch_session_context,
    get_latest_outage,
    parse_iso_datetime,
    restore_latest_outage,
    sync_chat_history_to_db,
)

# 3. Initialize your ReAct agent
base_react_agent = dspy.ReAct(
    signature=PEA_Assistant,
    tools=[Check_Outage_Tool, Fast_Track_Tool],
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
            "System: Do not tell the user whether ETR comes from OMS or the model. Keep the source internal.",
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
            formatted.append(f"{msg['role']}: {event_prefix}{msg['content']}")
        return "\n".join(formatted)

    def _format_thai_time(self, value: datetime | None) -> str | None:
        if not value:
            return None
        bangkok_time = value.astimezone(ZoneInfo("Asia/Bangkok"))
        return bangkok_time.strftime("%H:%M น.")

    def _format_remaining_label(
        self, target_time: datetime | None, now: datetime | None
    ) -> str | None:
        if not target_time or not now:
            return None

        remaining_minutes = ceil((target_time - now).total_seconds() / 60)
        if remaining_minutes <= 0:
            return "เลยกำหนดแล้ว"
        if remaining_minutes < 60:
            return f"ภายในประมาณ {remaining_minutes} นาที"

        hours = remaining_minutes // 60
        minutes = remaining_minutes % 60
        if minutes:
            return f"ภายในประมาณ {hours} ชั่วโมง {minutes} นาที"
        return f"ภายในประมาณ {hours} ชั่วโมง"

    def _format_user_time_label(
        self, target_time: datetime | None, now: datetime | None
    ) -> str | None:
        thai_time = self._format_thai_time(target_time)
        remaining_label = self._format_remaining_label(target_time, now)
        if thai_time and remaining_label:
            return f"{thai_time} ({remaining_label})"
        return thai_time

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
            f"System: latest_eta_remaining_label={self._format_remaining_label(eta, now)}",
            f"System: latest_eta_user_label={self._format_user_time_label(eta, now)}",
            f"System: latest_etr_thai_label={self._format_thai_time(etr)}",
            f"System: latest_etr_remaining_label={self._format_remaining_label(etr, now)}",
            f"System: latest_etr_user_label={self._format_user_time_label(etr, now)}",
            f"System: latest_sla_thai_label={self._format_thai_time(sla)}",
            f"System: latest_sla_remaining_label={self._format_remaining_label(sla, now)}",
            f"System: latest_sla_user_label={self._format_user_time_label(sla, now)}",
            "System: ETA timeout is an OMS/Celery event. Do not say ETA expired unless chat_history contains event_type=eta_timeout.",
            "System: If asked about current time, answer naturally using current_time_thai_label.",
            "System: If asked about technician arrival before eta_timeout event, answer naturally using latest_eta_user_label.",
            "System: If asked about restoration time, answer naturally using latest_etr_user_label only if it is available; do not mention the ETR source.",
            "System: If event_type=etr_timeout_sla or fast_track_created, answer naturally using latest_sla_user_label when available.",
        ]

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

        history_str = self._format_history(
            history_list,
            time_stamp,
            get_latest_outage(session_id),
            ca_number=ca_number,
            pdpa_consent=pdpa_consent,
        )

        response = self.agent(
            chat_history=history_str, question=user_input, time_stamp=time_stamp
        )

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
