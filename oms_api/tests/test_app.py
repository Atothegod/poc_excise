import os
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

REAL_CSV_PATH = Path(__file__).resolve().parents[2] / "backend" / "ca_lat_lon_2.csv"
if not REAL_CSV_PATH.exists():
    REAL_CSV_PATH = Path(__file__).resolve().parents[1] / "ca_lat_lon_2.csv"

os.environ["CA_CSV_PATH"] = str(REAL_CSV_PATH)
os.environ["DJANGO_OMS_EVENT_URL"] = ""

from fastapi.testclient import TestClient

import app as app_module
from app import app, load_customers

FIRST_CA = "020025009298"
SECOND_CA = "020017181205"


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
        response = self.client.get(f"/customers/{FIRST_CA}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["ca_number"], FIRST_CA)
        self.assertEqual(response.json()["lat"], 14.062607725177923)
        self.assertEqual(response.json()["lon"], 100.61090536911522)
        self.assertIn("address", response.json())

    def test_customer_list_returns_all_loaded_customers_by_default(self):
        response = self.client.get("/customers")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["total"], 160)
        self.assertEqual(response.json()["returned"], 160)
        self.assertEqual(len(response.json()["customers"]), 160)

    def test_health_exposes_csv_debug_counts(self):
        response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["customers_loaded"], 160)
        self.assertEqual(response.json()["customers_with_coordinates"], 157)
        self.assertTrue(response.json()["csv_exists"])

    def test_nearby_radius_endpoint_is_removed(self):
        response = self.client.get(
            "/customers/nearby",
            params={"ca_number": FIRST_CA, "radius_km": 0.5},
        )

        self.assertEqual(response.status_code, 404)

    def test_ui_uses_tunable_radius_and_status_dropdown(self):
        response = self.client.get("/ui")
        html = response.text

        self.assertEqual(response.status_code, 200)
        self.assertIn('id="radiusKm"', html)
        self.assertIn("selectWithinRadius", html)
        self.assertIn("clearSelection", html)
        self.assertIn('id="statusAction"', html)
        self.assertIn('<option value="open">', html)
        self.assertIn('<option value="update">', html)
        self.assertIn('<option value="close">', html)
        self.assertIn('fetch("/customers")', html)
        self.assertNotIn("/customers/nearby", html)

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
                        FIRST_CA,
                        SECOND_CA,
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
            [FIRST_CA, SECOND_CA],
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
                    "caList": [FIRST_CA, SECOND_CA],
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
        self.assertEqual(payload["case"]["case_type"], "mass_outage")
        self.assertEqual(payload["case"]["affected_ca_numbers"], [FIRST_CA, SECOND_CA])

    def test_spec_sync_closed_sends_closed_event(self):
        app_module.DJANGO_OMS_EVENT_URL = "http://backend.test/api/oms/events/"
        with patch("app.requests.post", return_value=self._django_ok()) as mock_post:
            response = self.client.post(
                "/api/v1/oms/outage/sync",
                json={
                    "eventId": "PEA-OUTAGE-20260709-003",
                    "caList": [FIRST_CA],
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
            "caList": [FIRST_CA],
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
            json={"affected_ca_numbers": [FIRST_CA]},
        )

        self.assertEqual(response.status_code, 503)


if __name__ == "__main__":
    unittest.main()
