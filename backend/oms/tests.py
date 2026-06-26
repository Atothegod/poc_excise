from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils.dateparse import parse_datetime
from django.utils import timezone
from rest_framework.test import APIClient

from .models import CustomerLocation, CustomerReport, OutageCase
from .tasks import check_eta_timeout


class SyncAgentReportTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    @patch("oms.signals.send_proactive_alert.delay")
    @patch("oms.views_api.get_pea_assessment")
    @patch("oms.views_api.check_eta_timeout.apply_async")
    def test_new_event_uses_customer_location_and_assessment(
        self, mock_apply_async, mock_assessment, mock_send_alert
    ):
        mock_apply_async.return_value.id = "eta-task-id"
        mock_assessment.return_value = {
            "fastest_branch": "การไฟฟ้าส่วนภูมิภาค สาขา รังสิต",
            "eta_formatted": "~ 8 min",
            "estimated_etr_minutes": 69.0,
        }
        base_time = timezone.now()
        CustomerLocation.objects.create(
            ca_number="123456789012",
            fullname="Test User",
            latitude=9.2917,
            longitude=100.926296,
        )

        response = self.client.post(
            "/api/reports/sync/",
            {
                "session_id": "session-new-event",
                "ca_number": "123456789012",
                "time_stamp": base_time.isoformat(),
                "pdpa_consent": True,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["event_type"], "new_event")
        self.assertIsNotNone(response.data["eta_target_time"])
        self.assertEqual(response.data["eta_formatted"], "~ 8 min")
        self.assertEqual(
            response.data["fastest_branch"], "การไฟฟ้าส่วนภูมิภาค สาขา รังสิต"
        )
        self.assertIsNone(response.data["pluem_etr_minutes"])
        self.assertIsNone(response.data["pluem_etr_target_time"])
        self.assertIsNone(response.data["etr_target_time"])
        self.assertIsNone(response.data["etr_source"])

        eta = parse_datetime(response.data["eta_target_time"])
        self.assertEqual(eta, base_time + timedelta(minutes=8))

        case = OutageCase.objects.get(case_id=response.data["case_id"])
        self.assertEqual(case.eta_target_time, eta)
        self.assertEqual(case.assessment_eta_minutes, 8.0)
        self.assertEqual(case.pluem_etr_minutes, 69.0)
        self.assertEqual(case.pluem_etr_target_time, base_time + timedelta(minutes=69))
        self.assertIsNone(case.oms_etr)
        report = CustomerReport.objects.get(id=response.data["report_id"])
        self.assertEqual(report.latitude, 9.2917)
        self.assertEqual(report.longitude, 100.926296)
        self.assertTrue(report.pdpa_consent)
        self.assertIsNotNone(report.pdpa_consent_at)
        mock_assessment.assert_called_once_with(
            {
                "ca_number": "123456789012",
                "lat": 9.2917,
                "lon": 100.926296,
            }
        )

        admin_etr = base_time + timedelta(hours=2)
        case.oms_etr = admin_etr
        case.save(update_fields=["oms_etr"])

        response = self.client.post(
            "/api/reports/sync/",
            {
                "session_id": "session-new-event",
                "ca_number": "123456789012",
                "time_stamp": base_time.isoformat(),
                "pdpa_consent": True,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(parse_datetime(response.data["oms_etr"]), admin_etr)
        self.assertEqual(parse_datetime(response.data["etr_target_time"]), admin_etr)
        self.assertEqual(response.data["etr_source"], "oms")
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

    @patch("oms.views_api.get_pea_assessment")
    @patch("oms.views_api.check_eta_timeout.apply_async")
    def test_sync_report_temporarily_accepts_11_digit_ca_number(
        self, mock_apply_async, mock_assessment
    ):
        mock_apply_async.return_value.id = "eta-task-id"
        mock_assessment.return_value = {
            "fastest_branch": "การไฟฟ้าส่วนภูมิภาค สาขา รังสิต",
            "eta_formatted": "~ 8 min",
            "estimated_etr_minutes": 69.0,
        }
        CustomerLocation.objects.create(
            ca_number="20025009298",
            fullname="Eleven Digit User",
            latitude=14.0785,
            longitude=100.6140362,
        )

        response = self.client.post(
            "/api/reports/sync/",
            {
                "session_id": "session-11-digit-ca",
                "ca_number": "20025009298",
                "pdpa_consent": True,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["event_type"], "new_event")
        self.assertIsNotNone(response.data["case_id"])


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
    def test_eta_timeout_sends_pluem_etr_when_oms_etr_missing(self, mock_post):
        pluem_etr = timezone.now() + timedelta(minutes=69)
        case = OutageCase.objects.create(
            title="ETA timeout with Pluem ETR",
            latitude=9.2917,
            longitude=100.926296,
            eta_target_time=timezone.now() - timedelta(minutes=1),
            pluem_etr_minutes=69.0,
            pluem_etr_target_time=pluem_etr,
        )
        report = CustomerReport.objects.create(
            session_id="session-timeout-with-pluem-etr",
            ca_number="123456789012",
            related_case=case,
        )

        check_eta_timeout(case.case_id, report.id)

        payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(payload["event_type"], "eta_timeout")
        self.assertIn("โมเดล ETR พี่ปลื้ม", payload["message"])
        self.assertIn("เวลาที่คาดว่าจะแก้ไขเสร็จ", payload["message"])

    @patch("oms.tasks.get_pea_assessment")
    @patch("oms.tasks.requests.post")
    def test_eta_timeout_fetches_pluem_etr_when_oms_etr_missing(
        self, mock_post, mock_assessment
    ):
        mock_assessment.return_value = {
            "fastest_branch": "การไฟฟ้าส่วนภูมิภาค สาขา รังสิต",
            "eta_formatted": "~ 8 min",
            "estimated_etr_minutes": 69.0,
        }
        case = OutageCase.objects.create(
            title="ETA timeout fetches Pluem ETR",
            latitude=9.2917,
            longitude=100.926296,
            eta_target_time=timezone.now() - timedelta(minutes=1),
        )
        report = CustomerReport.objects.create(
            session_id="session-timeout-fetch-pluem-etr",
            ca_number="123456789012",
            related_case=case,
        )

        check_eta_timeout(case.case_id, report.id)

        payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(payload["event_type"], "eta_timeout")
        self.assertIn("โมเดล ETR พี่ปลื้ม", payload["message"])
        case.refresh_from_db()
        self.assertEqual(case.pluem_etr_minutes, 69.0)
        self.assertIsNotNone(case.pluem_etr_target_time)
        mock_assessment.assert_called_once_with(
            {
                "ca_number": "123456789012",
                "lat": 9.2917,
                "lon": 100.926296,
            }
        )

    @patch("oms.tasks.get_pea_assessment")
    @patch("oms.tasks.requests.post")
    def test_eta_timeout_falls_back_to_pluem_when_model_has_no_etr(
        self, mock_post, mock_assessment
    ):
        mock_assessment.return_value = {"error": "model unavailable"}
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
