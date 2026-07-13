import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

import webhook_server


class StatelessWebhookTests(unittest.TestCase):
    def _request(self):
        return webhook_server.QuestionRequest(
            job_id="job-1",
            user_message_id=42,
            question="ไฟดับครับ",
            session_id="session-1",
            ca_number="123456789012",
            pdpa_consent=True,
        )

    def test_ask_requires_internal_token(self):
        with self.assertRaises(HTTPException) as context:
            webhook_server.ask_agent(self._request(), x_internal_token=None)
        self.assertEqual(context.exception.status_code, 401)

    @patch.object(webhook_server.chatbot, "chat")
    def test_ask_passes_message_cutoff_to_stateless_agent(self, mock_chat):
        mock_chat.return_value = SimpleNamespace(
            answer="รับทราบค่ะ",
            current_state={"flow_step": "checking_outage"},
        )

        response = webhook_server.ask_agent(
            self._request(),
            x_internal_token=webhook_server.INTERNAL_TOKEN,
        )

        self.assertEqual(response["answer"], "รับทราบค่ะ")
        self.assertEqual(mock_chat.call_args.kwargs["user_message_id"], 42)
        self.assertEqual(mock_chat.call_args.kwargs["session_id"], "session-1")


if __name__ == "__main__":
    unittest.main()
