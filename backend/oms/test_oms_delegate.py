from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APIClient

from .models import OutageCase


class OmsDelegationTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    @patch("oms.views_api.check_eta_timeout.apply_async")
    @patch("oms.views_api.get_pea_assessment")
    @patch("oms.views_api.report_outage")
    def test_sync_report_mirrors_oms_case_without_coordinates(
        self, mock_report_outage, mock_assessment, mock_apply_async
    ):
        mock_apply_async.return_value.id = "eta-task-id"
        mock_assessment.return_value = {
            "fastest_branch": "PEA Test Branch",
            "eta_formatted": "~ 8 min",
            "estimated_etr_minutes": 69,
        }
        mock_report_outage.return_value = {
            "status": "success",
            "event_type": "new_event",
            "newly_promoted": False,
            "case": {
                "case_id": "11111111-1111-1111-1111-111111111111",
                "status": "reported",
                "case_type": "normal",
                "affected_ca_numbers": ["123456789012"],
                "oms_etr": None,
            },
        }

        response = self.client.post(
            "/api/reports/sync/",
            {
                "session_id": "session-oms",
                "ca_number": "123456789012",
                "pdpa_consent": True,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["event_type"], "new_event")
        mock_report_outage.assert_called_once_with("123456789012")

        case = OutageCase.objects.get(case_id=response.data["case_id"])
        self.assertIsNone(case.latitude)
        self.assertIsNone(case.longitude)
        self.assertEqual(case.affected_ca_numbers, ["123456789012"])

    @patch("oms.views_api.get_customer")
    def test_validate_ca_uses_oms_customer_lookup(self, mock_get_customer):
        mock_get_customer.return_value = {
            "ca_number": "123456789012",
            "fullname": "OMS Customer",
        }

        response = self.client.get(
            "/api/reports/validate-ca/", {"ca_number": "123456789012"}
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["customer_name"], "OMS Customer")
        mock_get_customer.assert_called_once_with("123456789012")
