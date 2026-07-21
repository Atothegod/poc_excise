import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch


captured_react = {}
captured_predict = {}
fake_config = types.ModuleType("config")
fake_dspy = types.ModuleType("dspy")
fake_dspy.Signature = object
fake_dspy.InputField = lambda *args, **kwargs: None
fake_dspy.OutputField = lambda *args, **kwargs: None
fake_dspy.inspect_history = lambda *args, **kwargs: None


def fake_react(*args, **kwargs):
    captured_react.update(kwargs)
    return object()


fake_dspy.ReAct = fake_react


def fake_predict(*args, **kwargs):
    captured_predict.update(kwargs)
    return object()


fake_dspy.Predict = fake_predict
sys.modules["config"] = fake_config
sys.modules["dspy"] = fake_dspy

import agent as agent_module
import agent_tools
from agent import StatelessAgent, select_heart_persona
from session_state import reset_tool_execution, restore_latest_outage
from signature import PEA_Conversation_State


CA_NUMBER = "123456789012"
TIME_STAMP = "2026-07-09T12:00:00+07:00"


class HeartRoutingAgent:
    def __init__(
        self,
        flow_step="heart_mode",
        request_outage_confirmation=False,
        time_request="none",
    ):
        self.flow_step = flow_step
        self.request_outage_confirmation = request_outage_confirmation
        self.time_request = time_request
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        answer = agent_module.heart_tool(
            kwargs["question"],
            request_outage_confirmation=self.request_outage_confirmation,
            time_request=self.time_request,
        )
        return SimpleNamespace(
            answer=answer,
            current_state=PEA_Conversation_State(
                ca_number=CA_NUMBER,
                flow_step=self.flow_step,
            ),
        )


class CheckRoutingAgent:
    def __init__(self, force_new_case=False, final_answer=None):
        self.force_new_case = force_new_case
        self.final_answer = final_answer
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        answer = agent_module.Check_Outage_Tool(
            CA_NUMBER,
            pdpa_consent=True,
            force_new_case=self.force_new_case,
        )
        return SimpleNamespace(
            answer=self.final_answer if self.final_answer is not None else answer,
            current_state=PEA_Conversation_State(
                ca_number=CA_NUMBER,
                flow_step="checking_outage",
            ),
        )


class NoToolAgent:
    def __call__(self, **kwargs):
        return SimpleNamespace(
            answer="ตอบตรงโดยไม่ใช้เครื่องมือ",
            current_state=PEA_Conversation_State(ca_number=CA_NUMBER),
        )


class ExplodingAgent:
    def __call__(self, **kwargs):
        raise RuntimeError("agent unavailable")


class StatelessAgentReactRoutingTests(unittest.TestCase):
    def setUp(self):
        reset_tool_execution()
        restore_latest_outage("test", None)

    def tearDown(self):
        reset_tool_execution()
        restore_latest_outage("test", None)

    def _chat(self, stateless_agent, message, context=None, session_id="session-react"):
        with patch.object(agent_module, "fetch_session_context", return_value=context):
            return stateless_agent.chat(
                message,
                session_id=session_id,
                time_stamp=TIME_STAMP,
                ca_number=CA_NUMBER,
                pdpa_consent=True,
                user_message_id=42,
            )

    def _heart_prediction(self, answer="ยินดีช่วยเสมอค่ะ"):
        return patch.object(
            agent_module,
            "base_heart_model",
            Mock(return_value=SimpleNamespace(answer=answer)),
        )

    def _confirmation_context(self):
        return {
            "latest_outage": None,
            "current_state": {
                "flow_step": "heart_mode",
                "tools_use": ["heart_tool"],
                "heart_persona": "calm_commander",
                "outage_confirmation_pending": True,
            },
            "chat_history": [
                {
                    "role": "Assistant",
                    "message": agent_module.SAFE_OUTAGE_CLARIFICATION,
                }
            ],
        }

    def test_react_is_configured_with_check_outage_and_heart_tools(self):
        self.assertEqual(
            [tool.__name__ for tool in captured_react["tools"]],
            ["Check_Outage_Tool", "heart_tool"],
        )
        self.assertEqual(captured_react["max_iters"], 1)
        self.assertEqual(captured_predict["temperature"], 0.6)

    def test_persona_mapping_is_exact_for_every_non_heart_state(self):
        calm_states = {
            "waiting_for_intent",
            "checking_outage",
            "providing_eta_first",
            "fallback_to_human",
        }
        empathetic_states = {
            "existing_case_providing_eta",
            "mass_outage_providing_etr",
            "eta_timeout_waiting_etr",
            "etr_timeout_sla",
            "resolved",
        }

        for flow_step in calm_states:
            with self.subTest(flow_step=flow_step):
                self.assertEqual(
                    select_heart_persona({"flow_step": flow_step}),
                    "calm_commander",
                )
        for flow_step in empathetic_states:
            with self.subTest(flow_step=flow_step):
                self.assertEqual(
                    select_heart_persona({"flow_step": flow_step}),
                    "empathetic_analyst",
                )

    def test_heart_mode_continues_persona_and_defaults_to_empathetic(self):
        self.assertEqual(
            select_heart_persona(
                {"flow_step": "heart_mode", "heart_persona": "calm_commander"}
            ),
            "calm_commander",
        )
        self.assertEqual(
            select_heart_persona(
                {
                    "flow_step": "heart_mode",
                    "heart_persona": "empathetic_analyst",
                }
            ),
            "empathetic_analyst",
        )
        self.assertEqual(
            select_heart_persona({"flow_step": "heart_mode"}),
            "empathetic_analyst",
        )

    def test_emotional_followup_uses_heart_tool_and_persists_persona(self):
        router = HeartRoutingAgent()
        with self._heart_prediction("เข้าใจว่าตอนนี้กังวลมาก เล่าเพิ่มเติมได้ค่ะ") as heart_model:
            response = self._chat(StatelessAgent(router), "ตอนนี้กังวลมากครับ")

        self.assertEqual(response.answer, "เข้าใจว่าตอนนี้กังวลมาก เล่าเพิ่มเติมได้ค่ะ")
        self.assertEqual(response.current_state.flow_step, "heart_mode")
        self.assertEqual(response.current_state.tools_use, ["heart_tool"])
        self.assertEqual(response.current_state.heart_persona, "calm_commander")
        self.assertEqual(router.calls[0]["previous_state"].flow_step, "waiting_for_intent")
        self.assertEqual(
            heart_model.call_args.kwargs["question"],
            "ตอนนี้กังวลมากครับ",
        )
        self.assertEqual(
            heart_model.call_args.kwargs["time_policy"],
            "forbidden",
        )
        self.assertEqual(
            heart_model.call_args.kwargs["allowed_time_context"],
            "ห้ามใช้หรือกล่าวถึงข้อมูลเวลาใด ๆ ในคำตอบนี้",
        )

    def test_ambiguous_outage_uses_heart_without_calling_oms(self):
        router = HeartRoutingAgent(request_outage_confirmation=True)
        with patch.object(agent_tools, "save_report_to_db") as save_report:
            response = self._chat(StatelessAgent(router), "ไฟมีปัญหา")

        self.assertEqual(response.answer, agent_module.SAFE_OUTAGE_CLARIFICATION)
        self.assertEqual(response.current_state.tools_use, ["heart_tool"])
        self.assertEqual(response.current_state.flow_step, "heart_mode")
        self.assertTrue(response.current_state.outage_confirmation_pending)
        save_report.assert_not_called()

    def test_first_outage_turn_always_asks_confirmation_without_oms(self):
        messages = [
            "ไฟดับครับ",
            "ไฟไม่มาทั้งบ้าน",
            "ไฟมีปัญหาครับ",
            "ปกติไฟดับนานปะ",
        ]

        with patch.object(agent_tools, "save_report_to_db") as save_report:
            for index, message in enumerate(messages):
                with self.subTest(message=message):
                    router = HeartRoutingAgent(request_outage_confirmation=True)
                    response = self._chat(
                        StatelessAgent(router),
                        message,
                        session_id=f"first-outage-{index}",
                    )
                    self.assertEqual(
                        response.current_state.tools_use,
                        ["heart_tool"],
                    )
                    self.assertTrue(
                        response.current_state.outage_confirmation_pending
                    )
                    self.assertEqual(
                        response.answer,
                        agent_module.SAFE_OUTAGE_CLARIFICATION,
                    )
                    self.assertEqual(len(router.calls), 1)

        save_report.assert_not_called()

    def test_check_tool_guard_blocks_an_accidental_first_turn_call(self):
        with patch.object(agent_tools, "save_report_to_db") as save_report:
            response = self._chat(
                StatelessAgent(CheckRoutingAgent()),
                "ไฟดับครับ",
            )

        save_report.assert_not_called()
        self.assertEqual(response.answer, agent_module.SAFE_OUTAGE_CLARIFICATION)
        self.assertEqual(response.current_state.flow_step, "heart_mode")
        self.assertEqual(response.current_state.tools_use, ["Check_Outage_Tool"])
        self.assertTrue(response.current_state.outage_confirmation_pending)

    def test_confirmation_response_skips_writer_to_minimize_latency(self):
        with patch.object(agent_module, "base_heart_model") as heart_model:
            response = self._chat(
                StatelessAgent(
                    HeartRoutingAgent(request_outage_confirmation=True)
                ),
                "ปกติไฟดับนานปะ",
            )

        heart_model.assert_not_called()
        self.assertEqual(response.answer, agent_module.SAFE_OUTAGE_CLARIFICATION)

    def test_confirmed_outage_uses_check_tool_and_normalizes_state(self):
        router = CheckRoutingAgent(
            final_answer="เปิดใบงานแล้ว ช่างจะถึงหน้างานประมาณ 99:99 น."
        )
        db_response = {
            "event_type": "new_event",
            "eta_target_time": "2026-07-09T12:30:00+07:00",
            "eta_formatted": "12:30 น.",
            "fastest_branch": "PEA เมือง",
            "case_type": "normal",
        }
        with patch.object(
            agent_tools, "save_report_to_db", return_value=db_response
        ) as save_report:
            response = self._chat(
                StatelessAgent(router),
                "ใช่ครับ ช่วยตรวจสอบให้หน่อย",
                context=self._confirmation_context(),
            )

        save_report.assert_called_once_with(
            CA_NUMBER,
            True,
            force_new_case=False,
        )
        self.assertEqual(response.current_state.flow_step, "providing_eta_first")
        self.assertEqual(response.current_state.tools_use, ["Check_Outage_Tool"])
        self.assertFalse(response.current_state.is_mass_outage)
        self.assertIsNone(response.current_state.heart_persona)
        self.assertIn("12:30 น.", response.answer)
        self.assertNotIn("99:99", response.answer)

    def test_check_tool_failure_falls_back_to_human_without_heart(self):
        with patch.object(
            agent_tools,
            "save_report_to_db",
            side_effect=RuntimeError("OMS unavailable"),
        ):
            response = self._chat(
                StatelessAgent(CheckRoutingAgent()),
                "ใช่ครับ ช่วยตรวจสอบให้หน่อย",
                context=self._confirmation_context(),
            )

        self.assertEqual(response.current_state.flow_step, "fallback_to_human")
        self.assertEqual(response.current_state.tools_use, ["Check_Outage_Tool"])
        self.assertIsNone(response.current_state.heart_persona)

    def test_no_tool_completion_keeps_direct_react_answer(self):
        with patch.object(agent_module, "base_heart_model") as heart_model:
            response = self._chat(StatelessAgent(NoToolAgent()), "คุยด้วยหน่อย")

        self.assertEqual(response.answer, "ตอบตรงโดยไม่ใช้เครื่องมือค่ะ")
        self.assertEqual(response.current_state.tools_use, [])
        self.assertEqual(response.current_state.flow_step, "waiting_for_intent")
        self.assertIsNone(response.current_state.heart_persona)
        heart_model.assert_not_called()

    def test_agent_failure_without_tool_falls_back_to_human(self):
        response = self._chat(StatelessAgent(ExplodingAgent()), "ไฟมีปัญหา")

        self.assertEqual(response.current_state.flow_step, "fallback_to_human")
        self.assertEqual(response.current_state.tools_use, [])

    def test_legacy_out_of_scope_state_becomes_heart_mode_with_default_persona(self):
        context = {
            "chat_history": [],
            "latest_outage": None,
            "current_state": {"flow_step": "out_of_scope"},
        }
        with self._heart_prediction("คุยกันต่อได้เลยค่ะ") as heart_model:
            response = self._chat(
                StatelessAgent(HeartRoutingAgent()),
                "เล่าเรื่องทั่วไป",
                context=context,
            )

        self.assertEqual(response.current_state.flow_step, "heart_mode")
        self.assertEqual(response.current_state.heart_persona, "empathetic_analyst")
        self.assertEqual(
            heart_model.call_args.kwargs["persona"],
            "empathetic_analyst",
        )

    def test_latest_oms_event_overrides_persisted_state_for_persona(self):
        context = {
            "chat_history": [],
            "latest_outage": {"event_type": "eta_timeout"},
            "current_state": {"flow_step": "providing_eta_first"},
        }
        with self._heart_prediction("เข้าใจว่ารอนานกว่าที่คาดค่ะ") as heart_model:
            self._chat(
                StatelessAgent(HeartRoutingAgent()),
                "ยังต้องรออีกเหรอ",
                context=context,
            )

        self.assertEqual(
            heart_model.call_args.kwargs["persona"],
            "empathetic_analyst",
        )

    def test_heart_response_is_not_overridden_by_stale_mass_outage_context(self):
        context = {
            "chat_history": [],
            "latest_outage": {
                "event_type": "mass_outage",
                "etr_target_time": "2026-07-09T13:00:00+07:00",
            },
            "current_state": {"flow_step": "mass_outage_providing_etr"},
        }
        with self._heart_prediction("รับฟังอยู่นะคะ"):
            response = self._chat(
                StatelessAgent(HeartRoutingAgent()),
                "วันนี้เหนื่อยจัง",
                context=context,
            )

        self.assertEqual(response.answer, "รับฟังอยู่นะคะ")
        self.assertNotIn("ไฟดับวงกว้าง", response.answer)

    def test_heart_without_time_request_does_not_receive_old_agent_time(self):
        context = {
            "chat_history": [
                {
                    "role": "agent",
                    "message": "อัปเดตล่าสุด คาดว่าจะจ่ายไฟคืนประมาณ 01:35 น. ค่ะ",
                },
                {
                    "role": "user",
                    "message": "อ่านแล้วครับ",
                },
            ],
            "latest_outage": {
                "event_type": "mass_outage",
                "eta_target_time": "2026-07-09T12:20:00+07:00",
                "etr_target_time": "2026-07-09T13:35:00+07:00",
                "sla_target_time": "2026-07-09T16:00:00+07:00",
            },
            "current_state": {"flow_step": "mass_outage_providing_etr"},
        }

        with self._heart_prediction(
            "เสียใจด้วยจริง ๆ ค่ะที่เหตุไฟดับกระทบจนปลาตาย "
            "ต้องขออภัยต่อความเสียหายที่เกิดขึ้นค่ะ"
        ) as heart_model:
            response = self._chat(
                StatelessAgent(HeartRoutingAgent(time_request="none")),
                "นานแล้วครับ ปลาตายเลย",
                context=context,
            )

        heart_inputs = heart_model.call_args.kwargs
        self.assertEqual(heart_inputs["time_policy"], "forbidden")
        self.assertEqual(heart_inputs["chat_history"], "Customer: อ่านแล้วครับ")
        self.assertNotIn("01:35", heart_inputs["chat_history"])
        self.assertNotIn("13:35", heart_inputs["chat_history"])
        self.assertNotIn("01:35", response.answer)
        self.assertNotIn("จ่ายไฟคืน", response.answer)
        self.assertIn("ปลาตาย", response.answer)

    def test_explicit_restoration_question_receives_only_restoration_time(self):
        context = {
            "chat_history": [
                {
                    "role": "agent",
                    "message": "แจ้งกำหนดเดิมไว้ก่อนหน้านี้ค่ะ",
                }
            ],
            "latest_outage": {
                "event_type": "mass_outage",
                "eta_target_time": "2026-07-09T12:20:00+07:00",
                "etr_target_time": "2026-07-09T13:35:00+07:00",
                "sla_target_time": "2026-07-09T16:00:00+07:00",
            },
            "current_state": {"flow_step": "mass_outage_providing_etr"},
        }

        with self._heart_prediction(
            "เข้าใจว่าการรอทำให้กังวลค่ะ ข้อมูลล่าสุดคาดว่าจะจ่ายไฟคืนประมาณ 13:35 น. ค่ะ"
        ) as heart_model:
            response = self._chat(
                StatelessAgent(
                    HeartRoutingAgent(time_request="power_restoration")
                ),
                "รอนานมาก แล้วไฟจะมาเมื่อไหร่ครับ",
                context=context,
            )

        heart_inputs = heart_model.call_args.kwargs
        self.assertEqual(heart_inputs["time_policy"], "explicit_request")
        self.assertEqual(
            heart_inputs["allowed_time_context"],
            "เวลาที่คาดว่าจะจ่ายไฟคืน: 13:35 น.",
        )
        self.assertNotIn("12:20", heart_inputs["allowed_time_context"])
        self.assertNotIn("16:00", heart_inputs["allowed_time_context"])
        self.assertIn("13:35 น.", response.answer)

    def test_feminine_ending_preserves_questions_and_time_abbreviation(self):
        stateless_agent = StatelessAgent(NoToolAgent())

        question = stateless_agent._ensure_feminine_ending(
            SimpleNamespace(answer="ตอนนี้ไฟดับสนิทหรือไม่คะ")
        )
        time_answer = stateless_agent._ensure_feminine_ending(
            SimpleNamespace(answer="คาดว่าจะจ่ายไฟคืนประมาณ 23:51 น.")
        )

        self.assertEqual(question.answer, "ตอนนี้ไฟดับสนิทหรือไม่คะ")
        self.assertEqual(
            time_answer.answer,
            "คาดว่าจะจ่ายไฟคืนประมาณ 23:51 น. ค่ะ",
        )

    def test_closed_loop_resolved_uses_heart_and_records_once(self):
        context = {
            "latest_outage": None,
            "current_state": {"flow_step": "resolved"},
            "chat_history": [
                {
                    "role": "System Alert (OMS)",
                    "message": "ไฟฟ้ากลับมาใช้งานได้หรือยังคะ",
                    "event_type": "closed_loop_prompt",
                    "report_id": 7,
                }
            ],
        }
        with self._heart_prediction("ดีใจที่ไฟกลับมาใช้งานได้แล้วค่ะ"), patch.object(
            agent_module, "record_closed_loop_response"
        ) as record_response:
            response = self._chat(
                StatelessAgent(HeartRoutingAgent(flow_step="resolved")),
                "ไฟมาแล้ว",
                context=context,
            )

        self.assertEqual(response.current_state.flow_step, "resolved")
        self.assertEqual(response.current_state.tools_use, ["heart_tool"])
        self.assertIsNone(response.current_state.heart_persona)
        record_response.assert_called_once_with(
            "resolved",
            ca_number=CA_NUMBER,
            report_id=7,
        )

    def test_closed_loop_still_out_uses_force_new_case_and_records_once(self):
        context = {
            "latest_outage": None,
            "current_state": {"flow_step": "resolved"},
            "chat_history": [
                {
                    "role": "System Alert (OMS)",
                    "message": "ไฟฟ้ากลับมาใช้งานได้หรือยังคะ",
                    "event_type": "closed_loop_prompt",
                    "report_id": 9,
                }
            ],
        }
        db_response = {
            "event_type": "new_event",
            "eta_formatted": "12:30 น.",
            "case_type": "normal",
        }
        with patch.object(
            agent_tools, "save_report_to_db", return_value=db_response
        ) as save_report, patch.object(
            agent_tools, "record_closed_loop_response"
        ) as record_response:
            router = CheckRoutingAgent(force_new_case=True)
            response = self._chat(
                StatelessAgent(router),
                "ไฟยังไม่มา",
                context=context,
            )

        save_report.assert_called_once_with(
            CA_NUMBER,
            True,
            force_new_case=True,
        )
        record_response.assert_called_once_with(
            "still_out",
            ca_number=CA_NUMBER,
            report_id=9,
        )
        self.assertEqual(response.current_state.flow_step, "providing_eta_first")
        self.assertEqual(response.current_state.tools_use, ["Check_Outage_Tool"])
        self.assertEqual(len(router.calls), 1)


if __name__ == "__main__":
    unittest.main()
