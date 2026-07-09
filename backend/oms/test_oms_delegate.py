from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APIClient

from .case_logic import CASE_TYPE_MASS_OUTAGE, CASE_TYPE_NORMAL
from .models import CustomerLocation, CustomerReport, OutageCase


class CaseOwnershipTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    @patch("oms.views_api.check_eta_timeout.apply_async")
    @patch("oms.views_api.get_pea_assessment")
    def test_sync_report_creates_local_single_ca_case_without_oms_delegate(
        self, mock_assessment, mock_apply_async
    ):
        mock_apply_async.return_value.id = "eta-task-id"
        mock_assessment.return_value = {
            "fastest_branch": "PEA Test Branch",
            "eta_formatted": "~ 8 min",
            "estimated_etr_minutes": 69,
        }
        CustomerLocation.objects.create(
            ca_number="123456789012",
            fullname="Local Customer",
            latitude=14.0,
            longitude=100.0,
        )

        response = self.client.post(
            "/api/reports/sync/",
            {
                "session_id": "session-local",
                "ca_number": "123456789012",
                "pdpa_consent": True,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["event_type"], "new_event")

        case = OutageCase.objects.get(case_id=response.data["case_id"])
        self.assertEqual(case.case_type, CASE_TYPE_NORMAL)
        self.assertEqual(case.affected_ca_numbers, ["123456789012"])
        self.assertEqual(case.latitude, 14.0)
        self.assertEqual(case.longitude, 100.0)

    @patch("oms.views_api.send_proactive_alert.delay")
    def test_oms_event_creates_authoritative_group_case_in_django(
        self, mock_send_alert
    ):
        CustomerReport.objects.create(
            session_id="session-a",
            ca_number="123456789012",
        )
        CustomerReport.objects.create(
            session_id="session-b",
            ca_number="123456789013",
        )

        response = self.client.post(
            "/api/oms/events/",
            {
                "event_type": "case_opened",
                "case": {
                    "case_id": "11111111-1111-1111-1111-111111111111",
                    "external_event_id": "PEA-OUTAGE-001",
                    "status": "reported",
                    "case_type": CASE_TYPE_MASS_OUTAGE,
                    "affected_ca_numbers": ["123456789012", "123456789013"],
                },
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        case = OutageCase.objects.get(case_id="11111111-1111-1111-1111-111111111111")
        self.assertEqual(case.external_event_id, "PEA-OUTAGE-001")
        self.assertEqual(case.case_type, CASE_TYPE_MASS_OUTAGE)
        self.assertEqual(
            set(case.affected_ca_numbers), {"123456789012", "123456789013"}
        )
        self.assertEqual(
            set(
                CustomerReport.objects.filter(related_case=case).values_list(
                    "session_id", flat=True
                )
            ),
            {"session-a", "session-b"},
        )
        self.assertEqual(mock_send_alert.call_count, 2)
