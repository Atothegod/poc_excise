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

    def _django_ok(self, case_id="11111111-1111-1111-1111-111111111111"):
        response = Mock()
        response.status_code = 200
        response.json.return_value = {
            "status": "success",
            "case_id": case_id,
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
        expected_count = len(app_module.CUSTOMERS)

        self.assertEqual(response.status_code, 200)
        self.assertGreater(expected_count, 0)
        self.assertEqual(response.json()["total"], expected_count)
        self.assertEqual(response.json()["returned"], expected_count)
        self.assertEqual(len(response.json()["customers"]), expected_count)

    def test_health_exposes_csv_debug_counts(self):
        response = self.client.get("/health")
        expected_count = len(app_module.CUSTOMERS)

        self.assertEqual(response.status_code, 200)
        self.assertGreater(expected_count, 0)
        self.assertEqual(response.json()["customers_loaded"], expected_count)
        self.assertEqual(
            response.json()["customers_with_coordinates"], expected_count
        )
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
        self.assertIn("caseId.value = canonicalCaseId", html)
        self.assertIn("requires_retry_with_canonical_case_id", html)
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
        self.assertEqual(
            response.json()["canonical_case_id"],
            "11111111-1111-1111-1111-111111111111",
        )
        self.assertFalse(response.json()["case_id_changed"])
        self.assertTrue(response.json()["action_applied"])

    def test_open_case_returns_canonical_id_when_django_merges_the_case(self):
        app_module.DJANGO_OMS_EVENT_URL = "http://backend.test/api/oms/events/"
        requested_case_id = "11111111-1111-1111-1111-111111111111"
        canonical_case_id = "22222222-2222-2222-2222-222222222222"
        with patch(
            "app.requests.post",
            return_value=self._django_ok(case_id=canonical_case_id),
        ) as mock_post:
            response = self.client.post(
                "/cases/open",
                json={
                    "case_id": requested_case_id,
                    "affected_ca_numbers": [FIRST_CA],
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(mock_post.call_count, 1)
        body = response.json()
        self.assertEqual(body["requested_case_id"], requested_case_id)
        self.assertEqual(body["canonical_case_id"], canonical_case_id)
        self.assertEqual(body["case_id"], canonical_case_id)
        self.assertTrue(body["case_id_changed"])
        self.assertTrue(body["action_applied"])
        self.assertFalse(body["requires_retry_with_canonical_case_id"])

    def test_update_to_merged_child_surfaces_canonical_id_without_auto_retry(self):
        app_module.DJANGO_OMS_EVENT_URL = "http://backend.test/api/oms/events/"
        merged_child_id = "11111111-1111-1111-1111-111111111111"
        canonical_case_id = "22222222-2222-2222-2222-222222222222"
        with patch(
            "app.requests.post",
            return_value=self._django_ok(case_id=canonical_case_id),
        ) as mock_post:
            response = self.client.patch(
                f"/cases/{merged_child_id}",
                json={"oms_etr": "2026-07-09T11:00:00+00:00"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(mock_post.call_count, 1)
        body = response.json()
        self.assertEqual(body["requested_case_id"], merged_child_id)
        self.assertEqual(body["canonical_case_id"], canonical_case_id)
        self.assertTrue(body["case_id_changed"])
        self.assertFalse(body["action_applied"])
        self.assertTrue(body["requires_retry_with_canonical_case_id"])

    def test_close_to_merged_child_surfaces_canonical_id_without_auto_retry(self):
        app_module.DJANGO_OMS_EVENT_URL = "http://backend.test/api/oms/events/"
        merged_child_id = "11111111-1111-1111-1111-111111111111"
        canonical_case_id = "22222222-2222-2222-2222-222222222222"
        with patch(
            "app.requests.post",
            return_value=self._django_ok(case_id=canonical_case_id),
        ) as mock_post:
            response = self.client.post(f"/cases/{merged_child_id}/close")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(mock_post.call_count, 1)
        body = response.json()
        self.assertEqual(body["canonical_case_id"], canonical_case_id)
        self.assertTrue(body["case_id_changed"])
        self.assertFalse(body["action_applied"])
        self.assertTrue(body["requires_retry_with_canonical_case_id"])

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
