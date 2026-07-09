import os
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ["CA_CSV_PATH"] = str(Path(__file__).resolve().parents[1] / "ca_lat_lon_2.csv")
os.environ["DJANGO_OMS_EVENT_URL"] = ""

from fastapi.testclient import TestClient

import app as app_module
from app import app, load_customers


class OmsApiTests(unittest.TestCase):
    def setUp(self):
        app_module.OMS_API_TOKEN = ""
        app_module.DJANGO_OMS_EVENT_URL = ""
        load_customers()
        self.client = TestClient(app)

    def _django_ok(self):
        response = Mock()
        response.status_code = 200
        response.json.return_value = {
            "status": "success",
            "case_id": "11111111-1111-1111-1111-111111111111",
        }
        response.text = '{"status": "success"}'
        return response

    def test_customer_lookup_uses_csv(self):
        response = self.client.get("/customers/123456789012")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["ca_number"], "123456789012")

    def test_customer_list_and_nearby_radius_use_csv(self):
        listed = self.client.get("/customers", params={"limit": 10})
        nearby = self.client.get(
            "/customers/nearby",
            params={"ca_number": "123456789012", "radius_km": 0.5},
        )

        self.assertEqual(listed.status_code, 200)
        self.assertGreaterEqual(listed.json()["total"], 3)
        self.assertEqual(nearby.status_code, 200)
        self.assertEqual(
            {customer["ca_number"] for customer in nearby.json()["customers"]},
            {"123456789012", "123456789013", "123456789014"},
        )

    def test_cases_report_is_removed_from_merge_lifecycle(self):
        response = self.client.post("/cases/report", json={"ca_number": "123456789012"})

        self.assertEqual(response.status_code, 410)

    def test_open_case_proxies_group_event_to_django(self):
        app_module.DJANGO_OMS_EVENT_URL = "http://backend.test/api/oms/events/"
        with patch("app.requests.post", return_value=self._django_ok()) as mock_post:
            response = self.client.post(
                "/cases/open",
                json={
                    "case_id": "11111111-1111-1111-1111-111111111111",
                    "affected_ca_numbers": [
                        "123456789012",
                        "123456789013",
                        "123456789014",
                    ],
                    "oms_etr": "2026-07-09T10:30:00+00:00",
                },
            )

        self.assertEqual(response.status_code, 200)
        payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(payload["event_type"], "case_opened")
        self.assertEqual(payload["case"]["case_type"], "mass_outage")
        self.assertEqual(
            payload["case"]["case_id"], "11111111-1111-1111-1111-111111111111"
        )
        self.assertEqual(
            payload["case"]["affected_ca_numbers"],
            ["123456789012", "123456789013", "123456789014"],
        )

    def test_update_etr_and_close_case_proxy_to_django(self):
        app_module.DJANGO_OMS_EVENT_URL = "http://backend.test/api/oms/events/"
        with patch("app.requests.post", return_value=self._django_ok()) as mock_post:
            updated = self.client.patch(
                "/cases/11111111-1111-1111-1111-111111111111",
                json={"oms_etr": "2026-07-09T11:00:00+00:00"},
            )
            closed = self.client.post(
                "/cases/11111111-1111-1111-1111-111111111111/close"
            )

        self.assertEqual(updated.status_code, 200)
        self.assertEqual(closed.status_code, 200)
        self.assertEqual(mock_post.call_args_list[0].kwargs["json"]["event_type"], "etr_updated")
        self.assertEqual(mock_post.call_args_list[1].kwargs["json"]["event_type"], "case_closed")
        self.assertNotIn("oms_etr", mock_post.call_args_list[1].kwargs["json"]["case"])

    def test_spec_sync_open_sends_external_event_to_django(self):
        app_module.DJANGO_OMS_EVENT_URL = "http://backend.test/api/oms/events/"
        with patch("app.requests.post", return_value=self._django_ok()) as mock_post:
            response = self.client.post(
                "/api/v1/oms/outage/sync",
                json={
                    "eventId": "PEA-OUTAGE-20260709-001",
                    "caList": ["123456789012", "123456789013"],
                    "outageTime": "2026-07-09T08:30:00+07:00",
                    "etr": "2026-07-09T10:30:00+07:00",
                    "status": "OPEN",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["statusCode"], 200)
        self.assertEqual(
            response.json()["data"]["eventId"], "PEA-OUTAGE-20260709-001"
        )
        payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(payload["event_type"], "case_opened")
        self.assertEqual(
            payload["case"]["external_event_id"], "PEA-OUTAGE-20260709-001"
        )
        self.assertNotIn("case_id", payload["case"])
        self.assertEqual(payload["case"]["affected_ca_numbers"], ["123456789012", "123456789013"])

    def test_spec_sync_closed_sends_closed_event(self):
        app_module.DJANGO_OMS_EVENT_URL = "http://backend.test/api/oms/events/"
        with patch("app.requests.post", return_value=self._django_ok()) as mock_post:
            response = self.client.post(
                "/api/v1/oms/outage/sync",
                json={
                    "eventId": "PEA-OUTAGE-20260709-003",
                    "caList": ["123456789012"],
                    "outageTime": "2026-07-09T08:30:00+07:00",
                    "etr": "2026-07-09T10:30:00+07:00",
                    "status": "CLOSED",
                },
            )

        self.assertEqual(response.status_code, 200)
        payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(payload["event_type"], "case_closed")
        self.assertEqual(payload["case"]["status"], "restored")

    def test_spec_sync_enforces_bearer_auth_when_token_is_set(self):
        app_module.OMS_API_TOKEN = "secret-token"
        app_module.DJANGO_OMS_EVENT_URL = "http://backend.test/api/oms/events/"
        payload = {
            "eventId": "PEA-OUTAGE-20260709-004",
            "caList": ["123456789012"],
            "outageTime": "2026-07-09T08:30:00+07:00",
            "etr": None,
            "status": "OPEN",
        }

        missing = self.client.post("/api/v1/oms/outage/sync", json=payload)
        with patch("app.requests.post", return_value=self._django_ok()):
            accepted = self.client.post(
                "/api/v1/oms/outage/sync",
                json=payload,
                headers={"Authorization": "Bearer secret-token"},
            )

        self.assertEqual(missing.status_code, 401)
        self.assertEqual(accepted.status_code, 200)

    def test_spec_status_endpoint_is_not_added(self):
        response = self.client.get(
            "/api/v1/oms/outage/status", params={"ca": "123456789012"}
        )

        self.assertEqual(response.status_code, 404)

    def test_proxy_requires_django_event_url(self):
        response = self.client.post(
            "/cases/open",
            json={"affected_ca_numbers": ["123456789012"]},
        )

        self.assertEqual(response.status_code, 503)


if __name__ == "__main__":
    unittest.main()
