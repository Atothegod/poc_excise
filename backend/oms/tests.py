from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils.dateparse import parse_datetime
from django.utils import timezone
from rest_framework.test import APIClient

from .models import CustomerReport, OutageCase
from .tasks import check_eta_timeout


class SyncAgentReportTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    @patch("oms.views_api.check_eta_timeout.apply_async")
    def test_new_event_returns_no_etr_until_admin_sets_it(self, mock_apply_async):
        mock_apply_async.return_value.id = "eta-task-id"
        base_time = timezone.now()

        response = self.client.post(
            "/api/reports/sync/",
            {
                "session_id": "session-new-event",
                "ca_number": "123456789",
                "latitude": 9.2917,
                "longitude": 100.926296,
                "time_stamp": base_time.isoformat(),
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["event_type"], "new_event")
        self.assertIsNotNone(response.data["eta_target_time"])
        self.assertIsNone(response.data["oms_etr"])

        eta = parse_datetime(response.data["eta_target_time"])
        self.assertEqual(eta, base_time + timedelta(minutes=45))

        case = OutageCase.objects.get(case_id=response.data["case_id"])
        self.assertEqual(case.eta_target_time, eta)
        self.assertIsNone(case.oms_etr)

        admin_etr = base_time + timedelta(hours=2)
        case.oms_etr = admin_etr
        case.save(update_fields=["oms_etr"])

        response = self.client.post(
            "/api/reports/sync/",
            {
                "session_id": "session-new-event",
                "ca_number": "123456789",
                "latitude": 9.2917,
                "longitude": 100.926296,
                "time_stamp": base_time.isoformat(),
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(parse_datetime(response.data["oms_etr"]), admin_etr)


class CheckEtaTimeoutTests(TestCase):
    @patch("oms.tasks.requests.post")
    def test_eta_timeout_sends_oms_etr_when_available(self, mock_post):
        etr = timezone.now() + timedelta(hours=1)
        case = OutageCase.objects.create(
            title="ETA timeout with ETR",
            latitude=9.2917,
            longitude=100.926296,
            eta_target_time=timezone.now() - timedelta(minutes=1),
            oms_etr=etr,
        )
        report = CustomerReport.objects.create(
            session_id="session-timeout-with-etr",
            ca_number="123456789",
            related_case=case,
        )

        check_eta_timeout(case.case_id, report.id)

        payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(payload["event_type"], "eta_timeout")
        self.assertIn(etr.isoformat(), payload["message"])
        self.assertNotIn("พี่ปลื้ม", payload["message"])

    @patch("oms.tasks.requests.post")
    def test_eta_timeout_falls_back_to_pluem_when_oms_etr_missing(self, mock_post):
        case = OutageCase.objects.create(
            title="ETA timeout without ETR",
            latitude=9.2917,
            longitude=100.926296,
            eta_target_time=timezone.now() - timedelta(minutes=1),
        )
        report = CustomerReport.objects.create(
            session_id="session-timeout-without-etr",
            ca_number="123456789",
            related_case=case,
        )

        check_eta_timeout(case.case_id, report.id)

        payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(payload["event_type"], "eta_timeout")
        self.assertIn("พี่ปลื้ม", payload["message"])
