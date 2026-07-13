import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

fake_config = types.ModuleType("config")
fake_dspy = types.ModuleType("dspy")
fake_dspy.Signature = object
fake_dspy.InputField = lambda *args, **kwargs: None
fake_dspy.OutputField = lambda *args, **kwargs: None
fake_dspy.ReAct = lambda *args, **kwargs: object()
fake_dspy.Predict = lambda *args, **kwargs: object()
sys.modules["config"] = fake_config
sys.modules["dspy"] = fake_dspy

import agent as agent_module
from agent import StatelessAgent
from session_state import restore_latest_outage
from signature import PEA_Conversation_State, PEA_Intent_Router


class RouteStub:
    def __init__(self, route):
        self.route = route
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(routing=SimpleNamespace(route=self.route))


class ExplodingAgent:
    def __call__(self, **kwargs):
        raise AssertionError("ReAct agent should not be called")


class StatelessAgentIntentRouterTests(unittest.TestCase):
    def setUp(self):
        restore_latest_outage("test", None)

    def tearDown(self):
        restore_latest_outage("test", None)

    def _chat(self, memory_agent, message, session_id="session-router"):
        with patch.object(agent_module, "fetch_session_context", return_value=None):
            return memory_agent.chat(
                message,
                session_id=session_id,
                time_stamp="2026-07-09T12:00:00+07:00",
                ca_number="123456789012",
                pdpa_consent=True,
            )

    def test_out_of_scope_route_short_circuits_before_react_agent(self):
        router = RouteStub("out_of_scope")
        memory_agent = StatelessAgent(ExplodingAgent(), intent_router_module=router)

        response = self._chat(memory_agent, "ขอดูบิลค่าไฟ")

        self.assertEqual(response.current_state.flow_step, "out_of_scope")
        self.assertIn("รองรับการแจ้งและติดตามเหตุไฟดับเท่านั้น", response.answer)
        self.assertNotIn("ช่างจะถึงหน้างาน", response.answer)
        self.assertNotIn("จ่ายไฟคืน", response.answer)
        self.assertNotIn("ETA", response.answer)
        self.assertNotIn("ETR", response.answer)
        self.assertNotIn("SLA", response.answer)
        self.assertEqual(len(router.calls), 1)

    def test_generic_fire_route_short_circuits_as_out_of_scope(self):
        router = RouteStub("out_of_scope")
        memory_agent = StatelessAgent(ExplodingAgent(), intent_router_module=router)

        response = self._chat(memory_agent, "ไฟไหม้ครับ")

        self.assertEqual(response.current_state.flow_step, "out_of_scope")
        self.assertNotIn("ช่างจะถึงหน้างาน", response.answer)
        self.assertNotIn("จ่ายไฟคืน", response.answer)

    def test_out_of_scope_route_is_not_overridden_by_mass_outage_context(self):
        latest_outage = {
            "event_type": "mass_outage",
            "etr_target_time": "2026-07-09T13:00:00+07:00",
        }
        router = RouteStub("out_of_scope")
        memory_agent = StatelessAgent(ExplodingAgent(), intent_router_module=router)

        with patch.object(
            agent_module,
            "fetch_session_context",
            return_value={"chat_history": [], "latest_outage": latest_outage},
        ):
            response = memory_agent.chat(
                "ขอดูบิลค่าไฟ",
                session_id="session-mass-outage",
                time_stamp="2026-07-09T12:00:00+07:00",
                ca_number="123456789012",
                pdpa_consent=True,
            )

        self.assertEqual(response.current_state.flow_step, "out_of_scope")
        self.assertNotIn("ไฟดับวงกว้าง", response.answer)
        self.assertNotIn("จ่ายไฟคืน", response.answer)
        self.assertIn("latest_outage=present", router.calls[0]["latest_outage_context"])

    def test_outage_route_enters_react_agent(self):
        router = RouteStub("outage_report")
        react_agent = Mock(
            return_value=SimpleNamespace(
                answer="รับทราบค่ะ กำลังตรวจสอบเหตุไฟดับค่ะ",
                current_state=PEA_Conversation_State(
                    ca_number="123456789012",
                    flow_step="checking_outage",
                ),
            )
        )
        memory_agent = StatelessAgent(react_agent, intent_router_module=router)

        response = self._chat(memory_agent, "ไฟดับทั้งบ้าน")

        self.assertEqual(response.current_state.flow_step, "checking_outage")
        react_agent.assert_called_once()

    def test_closed_loop_resolved_route_records_resolution(self):
        router = RouteStub("closed_loop_resolved")
        memory_agent = StatelessAgent(ExplodingAgent(), intent_router_module=router)
        context = {
            "latest_outage": None,
            "chat_history": [{
                "role": "System Alert (OMS)",
                "message": "ระบบแจ้งว่าจ่ายไฟคืนแล้ว ไฟฟ้ากลับมาใช้งานได้หรือยังคะ",
                "event_type": "closed_loop_prompt",
                "report_id": 7,
            }],
        }

        with patch.object(agent_module, "fetch_session_context", return_value=context), patch.object(
            agent_module, "record_closed_loop_response"
        ) as mock_record:
            response = memory_agent.chat(
                "กลับมาใช้งานได้ตามปกติ",
                session_id="session-closed-loop-resolved",
                time_stamp="2026-07-09T12:00:00+07:00",
                ca_number="123456789012",
                pdpa_consent=True,
            )

        self.assertEqual(response.current_state.flow_step, "resolved")
        mock_record.assert_called_once_with(
            "resolved",
            ca_number="123456789012",
            report_id=7,
        )
        self.assertIn("pending_closed_loop=true", router.calls[0]["pending_closed_loop_context"])

    def test_closed_loop_still_out_route_calls_force_new_case_tool(self):
        router = RouteStub("closed_loop_still_out")
        memory_agent = StatelessAgent(Mock(), intent_router_module=router)
        context = {
            "latest_outage": None,
            "chat_history": [{
                "role": "System Alert (OMS)",
                "message": "ระบบแจ้งว่าจ่ายไฟคืนแล้ว ไฟฟ้ากลับมาใช้งานได้หรือยังคะ",
                "event_type": "closed_loop_prompt",
                "report_id": 9,
            }],
        }

        with patch.object(agent_module, "fetch_session_context", return_value=context), patch.object(
            agent_module, "record_closed_loop_response"
        ), patch.object(
            agent_module,
            "Check_Outage_Tool",
            return_value="[เหตุแจ้งใหม่] เปิดใบงานแล้ว แจ้งว่า ช่างจะถึงหน้างานประมาณ 12:30 น.",
        ) as mock_tool:
            response = memory_agent.chat(
                "ยังไม่กลับมาใช้งาน",
                session_id="session-closed-loop-still-out",
                time_stamp="2026-07-09T12:00:00+07:00",
                ca_number="123456789012",
                pdpa_consent=True,
            )

        self.assertEqual(response.current_state.flow_step, "providing_eta_first")
        mock_tool.assert_called_once_with(
            "123456789012",
            pdpa_consent=True,
            force_new_case=True,
        )

    def test_router_failure_fails_closed_as_out_of_scope(self):
        router = Mock(side_effect=RuntimeError("router unavailable"))
        memory_agent = StatelessAgent(ExplodingAgent(), intent_router_module=router)

        response = self._chat(memory_agent, "สอบถามเรื่องทั่วไป")

        self.assertEqual(response.current_state.flow_step, "out_of_scope")

    def test_router_prompt_keeps_generic_fire_out_of_scope(self):
        self.assertIn("ไฟไหม้ครับ", PEA_Intent_Router.__doc__)
        self.assertIn("Generic fire alone is out_of_scope", PEA_Intent_Router.__doc__)


if __name__ == "__main__":
    unittest.main()
