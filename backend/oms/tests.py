import math
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils.dateparse import parse_datetime
from django.utils import timezone
from rest_framework.test import APIClient

from .models import CustomerLocation, CustomerReport, OutageCase, OutageRestorationLog
from .tasks import check_eta_timeout
from .views_api import calculate_distance


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
        self.assertEqual(response.data["lv_group_id"], 1)
        self.assertEqual(response.data["affected_ca_numbers"], ["123456789012"])
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
        self.assertEqual(case.affected_ca_numbers, ["123456789012"])
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

    def test_sync_chat_history_stores_dialog_on_latest_report(self):
        report = CustomerReport.objects.create(
            session_id="session-dialog",
            ca_number="123456789012",
        )

        response = self.client.post(
            "/api/reports/chat-history/",
            {
                "session_id": "session-dialog",
                "ca_number": "123456789012",
                "chat_history": [
                    {
                        "role": "user",
                        "message": "ไฟดับครับ",
                        "timestamp": "2026-06-26T12:00:00+07:00",
                    },
                    {
                        "role": "agent",
                        "message": "รบกวนแจ้ง CA ครับ",
                        "timestamp": "2026-06-26T12:00:01+07:00",
                    },
                ],
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        report.refresh_from_db()
        self.assertEqual(len(report.chat_history), 2)
        self.assertEqual(report.chat_history[0]["role"], "user")
        self.assertEqual(report.chat_history[1]["role"], "agent")

    def test_session_context_returns_chat_history_and_latest_outage(self):
        eta = timezone.now() + timedelta(minutes=15)
        case = OutageCase.objects.create(
            title="Session restore case",
            latitude=9.2917,
            longitude=100.926296,
            eta_target_time=eta,
        )
        report = CustomerReport.objects.create(
            session_id="session-restore",
            ca_number="123456789012",
            related_case=case,
            chat_history=[
                {
                    "role": "user",
                    "message": "ไฟดับครับ",
                    "timestamp": "2026-06-26T12:00:00+07:00",
                }
            ],
        )
        case.sync_affected_ca_numbers()

        response = self.client.get("/api/reports/session-context/session-restore/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "success")
        self.assertEqual(response.data["ca_number"], "123456789012")
        self.assertEqual(len(response.data["chat_history"]), 1)
        self.assertEqual(
            response.data["latest_outage"]["eta_target_time"], eta.isoformat()
        )
        self.assertEqual(
            response.data["latest_outage"]["affected_ca_numbers"], ["123456789012"]
        )

    def test_session_context_returns_pluem_etr_after_eta_timeout(self):
        eta = timezone.now() - timedelta(minutes=1)
        pluem_etr = timezone.now() + timedelta(minutes=71)
        case = OutageCase.objects.create(
            title="Session timeout Pluem ETR",
            latitude=9.2917,
            longitude=100.926296,
            eta_target_time=eta,
            pluem_etr_minutes=71.0,
            pluem_etr_target_time=pluem_etr,
        )
        CustomerReport.objects.create(
            session_id="session-timeout-context",
            ca_number="123456789012",
            related_case=case,
        )
        case.sync_affected_ca_numbers()

        response = self.client.get("/api/reports/session-context/session-timeout-context/")

        self.assertEqual(response.status_code, 200)
        outage = response.data["latest_outage"]
        self.assertEqual(outage["event_type"], "eta_timeout")
        self.assertEqual(parse_datetime(outage["etr_target_time"]), pluem_etr)
        self.assertEqual(parse_datetime(outage["pluem_etr_target_time"]), pluem_etr)
        self.assertEqual(outage["etr_source"], "pluem_model")

    def test_session_context_uses_oms_etr_over_pluem_etr(self):
        eta = timezone.now() - timedelta(minutes=1)
        pluem_etr = timezone.now() + timedelta(minutes=71)
        oms_etr = timezone.now() + timedelta(minutes=30)
        case = OutageCase.objects.create(
            title="Session timeout OMS ETR override",
            latitude=9.2917,
            longitude=100.926296,
            eta_target_time=eta,
            pluem_etr_minutes=71.0,
            pluem_etr_target_time=pluem_etr,
            oms_etr=oms_etr,
        )
        CustomerReport.objects.create(
            session_id="session-oms-context",
            ca_number="123456789012",
            related_case=case,
        )
        case.sync_affected_ca_numbers()

        response = self.client.get("/api/reports/session-context/session-oms-context/")

        self.assertEqual(response.status_code, 200)
        outage = response.data["latest_outage"]
        self.assertEqual(outage["event_type"], "eta_timeout")
        self.assertEqual(parse_datetime(outage["etr_target_time"]), oms_etr)
        self.assertEqual(parse_datetime(outage["oms_etr"]), oms_etr)
        self.assertIsNone(outage["pluem_etr_target_time"])
        self.assertEqual(outage["etr_source"], "oms")

    def test_sync_report_rejects_11_digit_ca_number(self):
        response = self.client.post(
            "/api/reports/sync/",
            {
                "session_id": "session-11-digit-ca",
                "ca_number": "20025009298",
                "pdpa_consent": True,
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
        self.assertIn("ภายในประมาณ", payload["message"])
        self.assertNotIn("พี่ปลื้ม", payload["message"])
        self.assertNotIn("OMS", payload["message"])

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
        self.assertIn("เวลาที่คาดว่าจะแก้ไขเสร็จ", payload["message"])
        self.assertIn("ภายในประมาณ", payload["message"])
        self.assertNotIn("พี่ปลื้ม", payload["message"])
        self.assertNotIn("OMS", payload["message"])

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
        self.assertIn("เวลาที่คาดว่าจะแก้ไขเสร็จ", payload["message"])
        self.assertIn("ภายในประมาณ", payload["message"])
        self.assertNotIn("พี่ปลื้ม", payload["message"])
        self.assertNotIn("OMS", payload["message"])
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
        self.assertIn("กำลังประเมินเวลาไฟกลับ", payload["message"])
        self.assertNotIn("พี่ปลื้ม", payload["message"])
        self.assertNotIn("OMS", payload["message"])

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

    @patch("oms.signals.send_proactive_alert.delay")
    def test_oms_etr_update_after_pluem_etr_sends_replacement_to_sessions(
        self, mock_send_alert
    ):
        case = OutageCase.objects.create(
            title="ETR update replaces Pluem",
            latitude=9.2917,
            longitude=100.926296,
            eta_target_time=timezone.now() - timedelta(minutes=5),
            pluem_etr_minutes=71.0,
            pluem_etr_target_time=timezone.now() + timedelta(minutes=71),
        )
        CustomerReport.objects.create(
            session_id="session-pluem-active",
            ca_number="123456789012",
            related_case=case,
        )

        case.oms_etr = timezone.now() + timedelta(minutes=30)
        case.save(update_fields=["oms_etr"])

        self.assertEqual(mock_send_alert.call_count, 1)
        call = mock_send_alert.call_args
        self.assertEqual(call.kwargs["event_type"], "etr_update")
        self.assertIn("ETR ล่าสุด", call.kwargs["message"])
        self.assertIn("ภายในประมาณ", call.kwargs["message"])
        self.assertNotIn("พี่ปลื้ม", call.kwargs["message"])
        self.assertNotIn("OMS", call.kwargs["message"])

    @patch("oms.signals.send_proactive_alert.delay")
    def test_restored_status_creates_restoration_log(self, mock_send_alert):
        case = OutageCase.objects.create(
            title="Restored analytics case",
            latitude=9.2917,
            longitude=100.926296,
            eta_target_time=timezone.now() - timedelta(minutes=30),
            oms_etr=timezone.now() + timedelta(minutes=10),
        )
        CustomerReport.objects.create(
            session_id="session-restored",
            ca_number="123456789012",
            related_case=case,
        )
        case.sync_affected_ca_numbers()

        case.status = "restored"
        case.save(update_fields=["status"])

        log = OutageRestorationLog.objects.get(case=case)
        self.assertEqual(log.lv_group_id, case.lv_group_id)
        self.assertEqual(log.affected_ca_numbers, ["123456789012"])
        self.assertEqual(log.etr_source, "oms")
        self.assertIsNotNone(log.restored_at)
        self.assertIsNotNone(log.etr_delta_minutes)


class OpsWebhookConsoleTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_ops_webhook_page_renders(self):
        response = self.client.get("/ops/webhook/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "OMS Webhook Console")

    @patch("oms.signals.send_proactive_alert.delay")
    def test_ops_action_sets_etr_for_selected_cases(self, mock_send_alert):
        selected_case = OutageCase.objects.create(
            title="Selected ETR case",
            latitude=9.2917,
            longitude=100.926296,
        )
        untouched_case = OutageCase.objects.create(
            title="Untouched ETR case",
            latitude=9.2918,
            longitude=100.926396,
        )
        CustomerReport.objects.create(
            session_id="session-ops-etr",
            ca_number="123456789012",
            related_case=selected_case,
        )

        response = self.client.post(
            "/ops/cases/action/",
            {
                "action": "set_etr",
                "target_mode": "selected",
                "case_ids": [str(selected_case.case_id)],
                "etr_minutes": 30,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["updated_count"], 1)
        selected_case.refresh_from_db()
        untouched_case.refresh_from_db()
        self.assertIsNotNone(selected_case.oms_etr)
        self.assertIsNone(untouched_case.oms_etr)
        mock_send_alert.assert_called_once()

    def test_ops_action_restores_all_active_cases(self):
        active_case = OutageCase.objects.create(
            title="Active restore case",
            latitude=9.2917,
            longitude=100.926296,
        )
        already_restored_case = OutageCase.objects.create(
            title="Already restored case",
            status="restored",
            latitude=9.2918,
            longitude=100.926396,
        )

        response = self.client.post(
            "/ops/cases/action/",
            {"action": "restore", "target_mode": "all_active"},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["updated_count"], 1)
        active_case.refresh_from_db()
        already_restored_case.refresh_from_db()
        self.assertEqual(active_case.status, "restored")
        self.assertEqual(already_restored_case.status, "restored")


class DistanceLinkingTests(TestCase):
    def test_calculate_distance_returns_infinite_outside_tiny_link_radius(self):
        distance = calculate_distance(14.0626077, 100.6109053, 14.0627077, 100.6109053)

        self.assertTrue(math.isinf(distance))

    def test_calculate_distance_allows_effectively_same_coordinates(self):
        distance = calculate_distance(14.0626077, 100.6109053, 14.0626077, 100.6109053)

        self.assertEqual(distance, 0)
