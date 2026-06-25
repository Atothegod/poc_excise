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

    @patch("oms.signals.send_proactive_alert.delay")
    @patch("oms.views_api.check_eta_timeout.apply_async")
    def test_new_event_returns_no_etr_until_admin_sets_it(
        self, mock_apply_async, mock_send_alert
    ):
        mock_apply_async.return_value.id = "eta-task-id"
        base_time = timezone.now()

        response = self.client.post(
            "/api/reports/sync/",
            {
                "session_id": "session-new-event",
                "ca_number": "123456789012",
                "latitude": 9.2917,
                "longitude": 100.926296,
                "time_stamp": base_time.isoformat(),
                "pdpa_consent": True,
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
        report = CustomerReport.objects.get(id=response.data["report_id"])
        self.assertTrue(report.pdpa_consent)
        self.assertIsNotNone(report.pdpa_consent_at)

        admin_etr = base_time + timedelta(hours=2)
        case.oms_etr = admin_etr
        case.save(update_fields=["oms_etr"])

        response = self.client.post(
            "/api/reports/sync/",
            {
                "session_id": "session-new-event",
                "ca_number": "123456789012",
                "latitude": 9.2917,
                "longitude": 100.926296,
                "time_stamp": base_time.isoformat(),
                "pdpa_consent": True,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(parse_datetime(response.data["oms_etr"]), admin_etr)
        mock_send_alert.assert_called_once()

    def test_sync_report_rejects_invalid_ca_number(self):
        response = self.client.post(
            "/api/reports/sync/",
            {
                "session_id": "session-invalid-ca",
                "ca_number": "12345abc9012",
                "latitude": 9.2917,
                "longitude": 100.926296,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 400)


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
            ca_number="123456789012",
            related_case=case,
        )

        check_eta_timeout(case.case_id, report.id)

        payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(payload["event_type"], "eta_timeout")
        self.assertIn("เวลาที่คาดว่าจะแก้ไขเสร็จ", payload["message"])
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
            ca_number="123456789012",
            related_case=case,
        )

        check_eta_timeout(case.case_id, report.id)

        payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(payload["event_type"], "eta_timeout")
        self.assertIn("พี่ปลื้ม", payload["message"])

    @patch("oms.tasks.requests.post")
    def test_eta_timeout_notifies_all_active_sessions_for_case(self, mock_post):
        case = OutageCase.objects.create(
            title="ETA timeout broadcast",
            latitude=9.2917,
            longitude=100.926296,
            eta_target_time=timezone.now() - timedelta(minutes=1),
        )
        first_report = CustomerReport.objects.create(
            session_id="session-a",
            ca_number="123456789012",
            related_case=case,
        )
        CustomerReport.objects.create(
            session_id="session-b",
            ca_number="123456789013",
            related_case=case,
        )
        CustomerReport.objects.create(
            session_id="session-a",
            ca_number="123456789014",
            related_case=case,
        )
        CustomerReport.objects.create(
            session_id="session-resolved",
            ca_number="123456789015",
            related_case=case,
            is_resolved=True,
        )

        check_eta_timeout(case.case_id, first_report.id)

        payloads = [call.kwargs["json"] for call in mock_post.call_args_list]
        self.assertEqual(
            {payload["session_id"] for payload in payloads},
            {"session-a", "session-b"},
        )
        self.assertEqual(mock_post.call_count, 2)


class OutageCaseSignalTests(TestCase):
    @patch("oms.signals.send_proactive_alert.delay")
    def test_oms_etr_update_sends_etr_update_to_active_sessions(self, mock_send_alert):
        case = OutageCase.objects.create(
            title="ETR update case",
            latitude=9.2917,
            longitude=100.926296,
            eta_target_time=timezone.now() - timedelta(minutes=1),
        )
        CustomerReport.objects.create(
            session_id="session-a",
            ca_number="123456789012",
            related_case=case,
        )
        CustomerReport.objects.create(
            session_id="session-b",
            ca_number="123456789013",
            related_case=case,
        )
        CustomerReport.objects.create(
            session_id="session-a",
            ca_number="123456789014",
            related_case=case,
        )
        CustomerReport.objects.create(
            session_id="session-resolved",
            ca_number="123456789015",
            related_case=case,
            is_resolved=True,
        )

        case.oms_etr = timezone.now() + timedelta(hours=1)
        case.save(update_fields=["oms_etr"])

        self.assertEqual(mock_send_alert.call_count, 2)
        event_types = {
            call.kwargs["event_type"] for call in mock_send_alert.call_args_list
        }
        self.assertEqual(event_types, {"etr_update"})
