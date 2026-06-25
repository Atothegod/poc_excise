# 1. IMPORT CONFIG FIRST SO THE LM IS LOADED ENTIRELY BEFORE BUILDING THE AGENT
import config
import dspy

# 2. Now import your components safely
from signature import PEA_Assistant

# นำเข้าตัวแปรทะลุมิติมาด้วยขอรับ!
from tools import (
    Check_Outage_Tool,
    Fast_Track_Tool,
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

    def _get_or_create_session(self, session_id: str) -> list:
        if session_id not in self.sessions:
            self.sessions[session_id] = []
        return self.sessions[session_id]

    def _format_history(self, history: list) -> str:
        if not history:
            return "No previous conversation."
        formatted = []
        for msg in history:
            formatted.append(f"{msg['role']}: {msg['content']}")
        return "\n".join(formatted)

    def chat(self, user_input: str, session_id: str, time_stamp: str):
        # 4. ยัดข้อมูลใส่กระเป๋าทะลุมิติก่อนเริ่มคุย!
        current_session_id.set(session_id)
        current_time_stamp.set(time_stamp)

        history_list = self._get_or_create_session(session_id)
        history_str = self._format_history(history_list)

        response = self.agent(
            chat_history=history_str, question=user_input, time_stamp=time_stamp
        )

        history_list.append({"role": "User", "content": user_input})
        history_list.append(
            {"role": "Assistant", "content": getattr(response, "answer", str(response))}
        )

        return response


chatbot = MemoryAgent(base_react_agent)
