# 1. IMPORT CONFIG FIRST SO THE LM IS LOADED ENTIRELY BEFORE BUILDING THE AGENT
import config
import dspy
from datetime import datetime
from zoneinfo import ZoneInfo

# 2. Now import your components safely
from signature import PEA_Assistant

# นำเข้าตัวแปรทะลุมิติมาด้วยขอรับ!
from tools import (
    Check_Outage_Tool,
    Fast_Track_Tool,
    current_session_id,
    current_time_stamp,
    get_latest_outage,
    parse_iso_datetime,
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

    def _format_history(
        self,
        history: list,
        server_time_stamp: str,
        latest_outage: dict | None,
    ) -> str:
        system_context = [
            f"System: authoritative_current_time={server_time_stamp}",
            "System: Do not trust user-claimed current time. Use authoritative_current_time for all time comparisons.",
            "System: Consent is not stored in agent memory. When calling Check_Outage_Tool, pass pdpa_consent=True only if the latest conversation clearly contains PDPA consent.",
        ]
        if latest_outage:
            system_context.append(
                "System: latest_outage="
                f"event_type={latest_outage.get('event_type')}; "
                f"case_id={latest_outage.get('case_id')}; "
                f"eta_target_time={latest_outage.get('eta_target_time')}; "
                f"eta_formatted={latest_outage.get('eta_formatted')}; "
                f"fastest_branch={latest_outage.get('fastest_branch')}; "
                f"etr_target_time={latest_outage.get('etr_target_time')}; "
                f"etr_source={latest_outage.get('etr_source')}; "
                f"oms_etr={latest_outage.get('oms_etr')}"
            )
            system_context.extend(
                self._format_temporal_context(server_time_stamp, latest_outage)
            )

        if not history:
            return "\n".join(system_context + ["No previous conversation."])
        formatted = system_context[:]
        for msg in history:
            formatted.append(f"{msg['role']}: {msg['content']}")
        return "\n".join(formatted)

    def _format_thai_time(self, value: datetime | None) -> str | None:
        if not value:
            return None
        bangkok_time = value.astimezone(ZoneInfo("Asia/Bangkok"))
        return bangkok_time.strftime("%H:%M น.")

    def _format_temporal_context(
        self, server_time_stamp: str, latest_outage: dict
    ) -> list[str]:
        now = datetime.fromisoformat(server_time_stamp)
        eta = parse_iso_datetime(latest_outage.get("eta_target_time"))
        etr = parse_iso_datetime(
            latest_outage.get("etr_target_time") or latest_outage.get("oms_etr")
        )
        etr_source = latest_outage.get("etr_source")
        etr_source_label = "OMS" if etr_source == "oms" else "โมเดลพี่ปลื้ม"

        return [
            f"System: current_time_thai_label={self._format_thai_time(now)}",
            f"System: latest_eta_thai_label={self._format_thai_time(eta)}",
            f"System: latest_eta_duration_label={latest_outage.get('eta_formatted')}",
            f"System: latest_etr_thai_label={self._format_thai_time(etr)}",
            f"System: latest_etr_source_label={etr_source_label if etr_source else None}",
            "System: ETA timeout is an OMS/Celery event. Do not say ETA expired unless chat_history contains event_type=eta_timeout.",
            "System: If asked about current time, answer naturally using current_time_thai_label.",
            "System: If asked about technician arrival before eta_timeout event, answer naturally using latest_eta_thai_label.",
        ]

    def chat(self, user_input: str, session_id: str, time_stamp: str):
        # 4. ยัดข้อมูลใส่กระเป๋าทะลุมิติก่อนเริ่มคุย!
        current_session_id.set(session_id)
        current_time_stamp.set(time_stamp)

        history_list = self._get_or_create_session(session_id)

        history_str = self._format_history(
            history_list,
            time_stamp,
            get_latest_outage(session_id),
        )

        response = self.agent(
            chat_history=history_str, question=user_input, time_stamp=time_stamp
        )

        history_list.append({"role": "User", "content": user_input})
        history_list.append(
            {"role": "Assistant", "content": getattr(response, "answer", str(response))}
        )

        return response


chatbot = MemoryAgent(base_react_agent)
