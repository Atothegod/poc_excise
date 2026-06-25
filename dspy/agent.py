# 1. IMPORT CONFIG FIRST SO THE LM IS LOADED ENTIRELY BEFORE BUILDING THE AGENT
import config
import dspy

# 2. Now import your components safely
from signature import PEA_Assistant

# นำเข้าตัวแปรทะลุมิติมาด้วยขอรับ!
from tools import (
    Check_Outage_Tool,
    Fast_Track_Tool,
    current_check_outage_consent,
    current_session_id,
    current_time_stamp,
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
        self.check_outage_consents = {}

    def _get_or_create_session(self, session_id: str) -> list:
        if session_id not in self.sessions:
            self.sessions[session_id] = []
        return self.sessions[session_id]

    def _format_history(self, history: list, has_check_outage_consent: bool) -> str:
        consent_status = (
            "System: check_outage_tool_consent=True"
            if has_check_outage_consent
            else "System: check_outage_tool_consent=False"
        )
        if not history:
            return f"{consent_status}\nNo previous conversation."
        formatted = [consent_status]
        for msg in history:
            formatted.append(f"{msg['role']}: {msg['content']}")
        return "\n".join(formatted)

    def _assistant_asked_check_consent(self, history: list) -> bool:
        for msg in reversed(history):
            if msg["role"] != "Assistant":
                continue
            content = msg["content"]
            return "ขออนุญาต" in content and "ตรวจสอบ" in content
        return False

    def _message_grants_consent(self, user_input: str, history: list) -> bool:
        text = user_input.strip().lower()
        explicit_terms = [
            "ยินยอม",
            "อนุญาต",
            "ให้ตรวจ",
            "ตรวจสอบได้",
            "เช็คได้",
            "consent",
        ]
        if any(term in text for term in explicit_terms):
            return True

        if not self._assistant_asked_check_consent(history):
            return False

        affirmative_replies = {
            "ได้",
            "ได้ครับ",
            "ได้ค่ะ",
            "ได้เลย",
            "ได้เลยครับ",
            "ได้เลยค่ะ",
            "ครับ",
            "ค่ะ",
            "คับ",
            "ตกลง",
            "โอเค",
            "โอเคครับ",
            "โอเคค่ะ",
            "ok",
            "okay",
            "yes",
            "ยืนยัน",
        }
        return text in affirmative_replies

    def chat(self, user_input: str, session_id: str, time_stamp: str):
        # 4. ยัดข้อมูลใส่กระเป๋าทะลุมิติก่อนเริ่มคุย!
        current_session_id.set(session_id)
        current_time_stamp.set(time_stamp)

        history_list = self._get_or_create_session(session_id)
        if self._message_grants_consent(user_input, history_list):
            self.check_outage_consents[session_id] = True

        has_check_outage_consent = self.check_outage_consents.get(session_id, False)
        current_check_outage_consent.set(has_check_outage_consent)
        history_str = self._format_history(history_list, has_check_outage_consent)

        response = self.agent(
            chat_history=history_str, question=user_input, time_stamp=time_stamp
        )

        history_list.append({"role": "User", "content": user_input})
        history_list.append(
            {"role": "Assistant", "content": getattr(response, "answer", str(response))}
        )

        return response


chatbot = MemoryAgent(base_react_agent)
