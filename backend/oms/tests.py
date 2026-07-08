import csv
import io
import math
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.contrib.admin.sites import AdminSite
from django.test import TestCase
from django.test.client import RequestFactory
from django.utils.dateparse import parse_datetime
from django.utils import timezone
from rest_framework.test import APIClient

from .admin import (
    CustomerLocationAdmin,
    CustomerReportAdmin,
    OutageCaseAdmin,
    OutageRestorationLogAdmin,
)
from .case_logic import (
    CASE_LINK_RADIUS_KM,
    CASE_TYPE_MASS_OUTAGE,
    CASE_TYPE_NORMAL,
    MASS_OUTAGE_CONFIRMATION_COUNT,
    STATUS_MERGED,
)
from .models import CustomerLocation, CustomerReport, OutageCase, OutageRestorationLog
from .tasks import check_eta_timeout, check_etr_timeout
from .views_api import calculate_distance


class SyncAgentReportTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def _assessment_payload(self, eta_minutes=8, etr_minutes=69):
        return {
            "fastest_branch": "การไฟฟ้าส่วนภูมิภาค สาขา รังสิต",
            "eta_formatted": f"~ {eta_minutes} min",
            "estimated_etr_minutes": float(etr_minutes),
        }

    def _post_sync(self, session_id, ca_number):
        return self.client.post(
            "/api/reports/sync/",
            {
                "session_id": session_id,
                "ca_number": ca_number,
                "pdpa_consent": True,
            },
            format="json",
        )

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
        self.assertEqual(case.sla_reference_time, base_time)
        self.assertEqual(case.sla_target_time, base_time + timedelta(hours=4))
        self.assertEqual(case.sla_reason, "case_created")
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

    def test_session_login_links_same_ca_to_existing_active_case(self):
        eta = timezone.now() + timedelta(minutes=20)
        CustomerLocation.objects.create(
            ca_number="123456789012",
            fullname="Existing CA User",
            latitude=9.2917,
            longitude=100.926296,
        )
        case = OutageCase.objects.create(
            title="Existing CA case",
            latitude=9.2917,
            longitude=100.926296,
            eta_target_time=eta,
        )
        CustomerReport.objects.create(
            session_id="session-one",
            ca_number="123456789012",
            related_case=case,
        )
        case.sync_affected_ca_numbers()

        response = self.client.post(
            "/api/reports/session-login/",
            {
                "session_id": "session-two",
                "ca_number": "123456789012",
                "pdpa_consent": True,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        report = CustomerReport.objects.get(
            session_id="session-two", ca_number="123456789012"
        )
        self.assertEqual(report.related_case, case)
        self.assertTrue(report.pdpa_consent)
        self.assertIsNotNone(report.pdpa_consent_at)
        case.refresh_from_db()
        self.assertEqual(case.affected_ca_numbers, ["123456789012"])
        self.assertEqual(response.data["latest_outage"]["case_id"], str(case.case_id))
        self.assertEqual(response.data["customer_name"], "Existing CA User")

    def test_validate_ca_login_requires_customer_location(self):
        response = self.client.get(
            "/api/reports/validate-ca/", {"ca_number": "123456789012"}
        )

        self.assertEqual(response.status_code, 404)

        CustomerLocation.objects.create(
            ca_number="123456789012",
            fullname="Valid CA User",
            latitude=9.2917,
            longitude=100.926296,
        )

        response = self.client.get(
            "/api/reports/validate-ca/", {"ca_number": "123456789012"}
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "success")
        self.assertEqual(response.data["ca_number"], "123456789012")
        self.assertEqual(response.data["customer_name"], "Valid CA User")

    def test_session_login_rejects_ca_without_customer_location(self):
        response = self.client.post(
            "/api/reports/session-login/",
            {
                "session_id": "session-missing-ca",
                "ca_number": "123456789012",
                "pdpa_consent": True,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 404)
        self.assertFalse(
            CustomerReport.objects.filter(session_id="session-missing-ca").exists()
        )

    def test_session_login_works_without_csrf_for_authenticated_browser_session(self):
        CustomerLocation.objects.create(
            ca_number="123456789012",
            fullname="CSRF User",
            latitude=9.2917,
            longitude=100.926296,
        )
        User.objects.create_user(username="admin-user", password="test-password")
        csrf_client = APIClient(enforce_csrf_checks=True)
        self.assertTrue(csrf_client.login(username="admin-user", password="test-password"))

        response = csrf_client.post(
            "/api/reports/session-login/",
            {
                "session_id": "session-csrf",
                "ca_number": "123456789012",
                "pdpa_consent": True,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            CustomerReport.objects.filter(session_id="session-csrf").exists()
        )

    @patch("oms.views_api.get_pea_assessment")
    def test_sync_report_reuses_active_case_for_same_ca_across_sessions(
        self, mock_assessment
    ):
        case = OutageCase.objects.create(
            title="Same CA active case",
            latitude=9.2917,
            longitude=100.926296,
            eta_target_time=timezone.now() + timedelta(minutes=15),
        )
        CustomerReport.objects.create(
            session_id="session-one",
            ca_number="123456789012",
            related_case=case,
        )

        response = self.client.post(
            "/api/reports/sync/",
            {
                "session_id": "session-two",
                "ca_number": "123456789012",
                "pdpa_consent": True,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["event_type"], "existing_ca_case")
        self.assertEqual(str(response.data["case_id"]), str(case.case_id))
        self.assertEqual(OutageCase.objects.count(), 1)
        report = CustomerReport.objects.get(
            session_id="session-two", ca_number="123456789012"
        )
        self.assertEqual(report.related_case, case)
        mock_assessment.assert_not_called()

    @patch("oms.views_api.get_pea_assessment")
    @patch("oms.views_api.check_eta_timeout.apply_async")
    def test_new_case_attaches_waiting_same_ca_sessions(
        self, mock_apply_async, mock_assessment
    ):
        mock_apply_async.return_value.id = "eta-task-id"
        mock_assessment.return_value = {
            "fastest_branch": "การไฟฟ้าส่วนภูมิภาค สาขา รังสิต",
            "eta_formatted": "~ 8 min",
            "estimated_etr_minutes": 69.0,
        }
        CustomerLocation.objects.create(
            ca_number="123456789012",
            fullname="Waiting Same CA User",
            latitude=9.2917,
            longitude=100.926296,
        )
        waiting_report = CustomerReport.objects.create(
            session_id="session-two",
            ca_number="123456789012",
            pdpa_consent=True,
        )

        response = self.client.post(
            "/api/reports/sync/",
            {
                "session_id": "session-one",
                "ca_number": "123456789012",
                "pdpa_consent": True,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["event_type"], "new_event")
        waiting_report.refresh_from_db()
        self.assertIsNotNone(waiting_report.related_case)
        self.assertEqual(
            str(waiting_report.related_case_id), str(response.data["case_id"])
        )
        case = OutageCase.objects.get(case_id=response.data["case_id"])
        self.assertEqual(case.affected_ca_numbers, ["123456789012"])

    @patch("oms.views_api.send_proactive_alert.delay")
    @patch("oms.views_api.get_pea_assessment")
    @patch("oms.views_api.check_eta_timeout.apply_async")
    def test_second_nearby_ca_stays_new_event_until_threshold(
        self, mock_apply_async, mock_assessment, mock_send_alert
    ):
        mock_apply_async.return_value.id = "eta-task-id"
        mock_assessment.return_value = self._assessment_payload()
        CustomerLocation.objects.create(
            ca_number="123456789012",
            fullname="Anchor CA User",
            latitude=14.0000,
            longitude=100.0000,
        )
        CustomerLocation.objects.create(
            ca_number="123456789013",
            fullname="Second Nearby CA User",
            latitude=14.0030,
            longitude=100.0000,
        )

        first_response = self._post_sync("session-anchor", "123456789012")
        second_response = self._post_sync("session-second", "123456789013")

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(second_response.status_code, 200)
        self.assertEqual(first_response.data["event_type"], "new_event")
        self.assertEqual(second_response.data["event_type"], "new_event")
        self.assertNotEqual(first_response.data["case_id"], second_response.data["case_id"])
        self.assertEqual(OutageCase.objects.count(), 2)
        self.assertEqual(
            set(OutageCase.objects.values_list("case_type", flat=True)),
            {CASE_TYPE_NORMAL},
        )
        mock_send_alert.assert_not_called()

    @patch("oms.views_api.celery_app.control.revoke")
    @patch("oms.views_api.send_proactive_alert.delay")
    @patch("oms.views_api.get_pea_assessment")
    @patch("oms.views_api.check_eta_timeout.apply_async")
    def test_third_nearby_ca_promotes_anchor_to_mass_outage(
        self, mock_apply_async, mock_assessment, mock_send_alert, mock_revoke
    ):
        mock_apply_async.return_value.id = "eta-task-id"
        mock_assessment.return_value = self._assessment_payload()
        ca_numbers = ["123456789012", "123456789013", "123456789014"]
        latitudes = [14.0000, 14.0020, 14.0030]
        for ca_number, latitude in zip(ca_numbers, latitudes):
            CustomerLocation.objects.create(
                ca_number=ca_number,
                fullname=f"Mass CA {ca_number}",
                latitude=latitude,
                longitude=100.0000,
            )

        first_response = self._post_sync("session-a", ca_numbers[0])
        second_response = self._post_sync("session-b", ca_numbers[1])
        third_response = self._post_sync("session-c", ca_numbers[2])

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(second_response.status_code, 200)
        self.assertEqual(third_response.status_code, 200)
        self.assertEqual(first_response.data["event_type"], "new_event")
        self.assertEqual(second_response.data["event_type"], "new_event")
        self.assertEqual(third_response.data["event_type"], "mass_outage")
        self.assertEqual(third_response.data["case_type"], CASE_TYPE_MASS_OUTAGE)
        self.assertEqual(
            set(third_response.data["affected_ca_numbers"]),
            set(ca_numbers),
        )
        self.assertIsNotNone(third_response.data["etr_target_time"])
        self.assertIsNotNone(parse_datetime(third_response.data["etr_target_time"]))
        self.assertEqual(third_response.data["etr_source"], "pluem_model")
        self.assertEqual(third_response.data["pluem_etr_minutes"], 69.0)

        anchor = OutageCase.objects.get(case_id=first_response.data["case_id"])
        anchor.refresh_from_db()
        self.assertEqual(anchor.case_type, CASE_TYPE_MASS_OUTAGE)
        self.assertEqual(anchor.affected_ca_numbers, sorted(ca_numbers))

        merged_cases = OutageCase.objects.filter(status=STATUS_MERGED)
        self.assertEqual(merged_cases.count(), 2)
        for merged_case in merged_cases:
            self.assertEqual(merged_case.merged_into, anchor)
            self.assertIsNotNone(merged_case.merged_at)
            self.assertIsNone(merged_case.celery_eta_task_id)
            self.assertIsNone(merged_case.celery_etr_task_id)

        for ca_number in ca_numbers:
            report = CustomerReport.objects.get(ca_number=ca_number)
            self.assertEqual(report.related_case, anchor)

        notified_reports = [
            CustomerReport.objects.get(id=call.kwargs["report_id"])
            for call in mock_send_alert.call_args_list
        ]
        self.assertEqual(
            {report.session_id for report in notified_reports},
            {"session-a", "session-b"},
        )
        self.assertEqual(
            {call.kwargs["event_type"] for call in mock_send_alert.call_args_list},
            {"mass_outage"},
        )
        for call in mock_send_alert.call_args_list:
            self.assertIn(
                "ขณะนี้เกิดเหตุไฟดับวงกว้างในพื้นที่ค่ะ",
                call.kwargs["message"],
            )
            self.assertIn("คาดว่าจะจ่ายไฟคืนประมาณ", call.kwargs["message"])
        self.assertEqual(mock_revoke.call_count, 2)

    @patch("oms.views_api.get_pea_assessment")
    def test_new_ca_inside_existing_mass_outage_links_to_anchor(self, mock_assessment):
        CustomerLocation.objects.create(
            ca_number="123456789012",
            fullname="Mass Area CA User",
            latitude=14.0020,
            longitude=100.0000,
        )
        mass_case = OutageCase.objects.create(
            title="Existing mass outage",
            case_type=CASE_TYPE_MASS_OUTAGE,
            latitude=14.0000,
            longitude=100.0000,
            pluem_etr_minutes=69.0,
            pluem_etr_target_time=timezone.now() + timedelta(minutes=69),
        )
        response = self._post_sync("session-mass-area", "123456789012")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["event_type"], "mass_outage")
        self.assertEqual(str(response.data["case_id"]), str(mass_case.case_id))
        self.assertIsNotNone(response.data["etr_target_time"])
        self.assertEqual(response.data["etr_source"], "pluem_model")
        self.assertEqual(OutageCase.objects.count(), 1)
        mock_assessment.assert_not_called()

    @patch("oms.views_api.get_pea_assessment")
    @patch("oms.views_api.check_eta_timeout.apply_async")
    def test_sync_report_creates_new_case_outside_active_case_radius(
        self, mock_apply_async, mock_assessment
    ):
        mock_apply_async.return_value.id = "eta-task-id"
        mock_assessment.return_value = {
            "fastest_branch": "การไฟฟ้าส่วนภูมิภาค สาขา รังสิต",
            "eta_formatted": "~ 8 min",
            "estimated_etr_minutes": 69.0,
        }
        CustomerLocation.objects.create(
            ca_number="123456789012",
            fullname="Outside Radius User",
            latitude=14.0000,
            longitude=100.0000,
        )
        outside_case = OutageCase.objects.create(
            title="Outside active case",
            latitude=14.1000,
            longitude=100.0000,
        )

        response = self.client.post(
            "/api/reports/sync/",
            {
                "session_id": "session-outside-radius",
                "ca_number": "123456789012",
                "pdpa_consent": True,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["event_type"], "new_event")
        self.assertNotEqual(str(response.data["case_id"]), str(outside_case.case_id))
        self.assertEqual(OutageCase.objects.count(), 2)
        mock_assessment.assert_called_once()

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

    def test_session_context_returns_etr_timeout_sla(self):
        now = timezone.now()
        sla_target = now + timedelta(hours=2)
        case = OutageCase.objects.create(
            title="Session ETR timeout SLA",
            latitude=9.2917,
            longitude=100.926296,
            eta_target_time=now - timedelta(hours=1),
            oms_etr=now - timedelta(minutes=1),
            sla_target_time=sla_target,
            sla_reference_time=now - timedelta(hours=2),
            sla_reason="oms_etr_update",
        )
        CustomerReport.objects.create(
            session_id="session-etr-timeout-context",
            ca_number="123456789012",
            related_case=case,
        )
        case.sync_affected_ca_numbers()

        response = self.client.get(
            "/api/reports/session-context/session-etr-timeout-context/"
        )

        self.assertEqual(response.status_code, 200)
        outage = response.data["latest_outage"]
        self.assertEqual(outage["event_type"], "etr_timeout_sla")
        self.assertEqual(parse_datetime(outage["sla_target_time"]), sla_target)

    def test_session_context_returns_active_normal_case_sla_window(self):
        now = timezone.now()
        sla_reference = now - timedelta(hours=1)
        sla_target = sla_reference + timedelta(hours=OutageCase.SLA_HOURS)
        case = OutageCase.objects.create(
            title="Active normal context",
            latitude=9.2917,
            longitude=100.926296,
            sla_reference_time=sla_reference,
            sla_target_time=sla_target,
            sla_reason="case_created",
        )
        CustomerReport.objects.create(
            session_id="session-normal-context",
            ca_number="123456789012",
            related_case=case,
        )
        case.sync_affected_ca_numbers()

        response = self.client.get(
            "/api/reports/session-context/session-normal-context/"
        )

        self.assertEqual(response.status_code, 200)
        outage = response.data["latest_outage"]
        self.assertEqual(outage["event_type"], "active_case_exists")
        self.assertEqual(outage["case_type"], CASE_TYPE_NORMAL)
        self.assertEqual(parse_datetime(outage["sla_reference_time"]), sla_reference)
        self.assertEqual(parse_datetime(outage["sla_target_time"]), sla_target)

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

    @patch("oms.views_api.get_pea_assessment")
    @patch("oms.views_api.check_eta_timeout.apply_async")
    def test_sync_report_after_restored_case_creates_normal_case_with_eta(
        self, mock_apply_async, mock_assessment
    ):
        mock_apply_async.return_value.id = "restored-new-eta-task-id"
        mock_assessment.return_value = self._assessment_payload(eta_minutes=12)
        original_case = OutageCase.objects.create(
            title="Original restored case",
            status="restored",
            latitude=9.2917,
            longitude=100.926296,
        )
        CustomerReport.objects.create(
            session_id="session-restored-new",
            ca_number="123456789012",
            latitude=9.2917,
            longitude=100.926296,
            related_case=original_case,
            is_resolved=True,
        )
        CustomerLocation.objects.create(
            ca_number="123456789012",
            fullname="Restored New User",
            latitude=9.2917,
            longitude=100.926296,
        )

        base_time = timezone.now()
        response = self.client.post(
            "/api/reports/sync/",
            {
                "ca_number": "123456789012",
                "session_id": "session-restored-new",
                "time_stamp": base_time.isoformat(),
                "pdpa_consent": True,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["event_type"], "new_event")
        case = OutageCase.objects.get(case_id=response.data["case_id"])
        self.assertNotEqual(case.case_id, original_case.case_id)
        self.assertEqual(case.case_type, CASE_TYPE_NORMAL)
        self.assertEqual(case.sla_reason, "case_created")
        self.assertEqual(case.sla_reference_time, base_time)
        self.assertEqual(case.sla_target_time, base_time + timedelta(hours=4))
        self.assertEqual(case.eta_target_time, base_time + timedelta(minutes=12))
        self.assertEqual(
            response.data["fastest_branch"], "การไฟฟ้าส่วนภูมิภาค สาขา รังสิต"
        )
        self.assertEqual(response.data["affected_ca_numbers"], ["123456789012"])
        mock_assessment.assert_called_once_with(
            {
                "ca_number": "123456789012",
                "lat": 9.2917,
                "lon": 100.926296,
            }
        )
        mock_apply_async.assert_called_once_with(
            args=[case.case_id, response.data["report_id"]],
            eta=case.eta_target_time,
        )

    @patch("oms.views_api.get_pea_assessment")
    def test_sync_report_does_not_use_customer_location_eta_when_assessment_errors(
        self, mock_assessment
    ):
        mock_assessment.return_value = {"error": "assessment unavailable"}
        CustomerLocation.objects.create(
            ca_number="123456789012",
            fullname="No Customer ETA Fallback User",
            latitude=9.2917,
            longitude=100.926296,
            eta_result=14.0,
        )

        base_time = timezone.now()
        response = self.client.post(
            "/api/reports/sync/",
            {
                "ca_number": "123456789012",
                "session_id": "session-fallback-eta",
                "time_stamp": base_time.isoformat(),
                "pdpa_consent": True,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["event_type"], "assessment_error")
        self.assertEqual(response.data["message"], "assessment unavailable")
        self.assertEqual(OutageCase.objects.count(), 0)
        report = CustomerReport.objects.get(session_id="session-fallback-eta")
        self.assertIsNone(report.related_case)
        self.assertEqual(report.latitude, 9.2917)
        self.assertEqual(report.longitude, 100.926296)
        mock_assessment.assert_called_once_with(
            {
                "ca_number": "123456789012",
                "lat": 9.2917,
                "lon": 100.926296,
            }
        )

    @patch("oms.views_api.get_pea_assessment")
    def test_sync_report_returns_assessment_error_without_usable_eta(
        self, mock_assessment
    ):
        mock_assessment.return_value = {"error": "assessment unavailable"}
        CustomerLocation.objects.create(
            ca_number="123456789012",
            fullname="No Fallback ETA User",
            latitude=9.2917,
            longitude=100.926296,
        )

        response = self.client.post(
            "/api/reports/sync/",
            {
                "ca_number": "123456789012",
                "session_id": "session-no-fallback-eta",
                "pdpa_consent": True,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["event_type"], "assessment_error")
        self.assertEqual(OutageCase.objects.count(), 0)
        mock_assessment.assert_called_once_with(
            {
                "ca_number": "123456789012",
                "lat": 9.2917,
                "lon": 100.926296,
            }
        )

    @patch("oms.views_api.get_pea_assessment")
    @patch("oms.views_api.check_eta_timeout.apply_async")
    def test_sync_report_reuses_same_session_active_case_on_repeat(
        self, mock_apply_async, mock_assessment
    ):
        mock_apply_async.return_value.id = "sync-repeat-task-id"
        mock_assessment.return_value = self._assessment_payload()
        CustomerLocation.objects.create(
            ca_number="123456789012",
            fullname="Sync Repeat User",
            latitude=9.2917,
            longitude=100.926296,
        )

        first_response = self.client.post(
            "/api/reports/sync/",
            {
                "ca_number": "123456789012",
                "session_id": "session-sync-repeat",
                "pdpa_consent": True,
            },
            format="json",
        )
        second_response = self.client.post(
            "/api/reports/sync/",
            {
                "ca_number": "123456789012",
                "session_id": "session-sync-repeat",
                "pdpa_consent": True,
            },
            format="json",
        )

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(second_response.status_code, 200)
        self.assertEqual(first_response.data["event_type"], "new_event")
        self.assertEqual(second_response.data["event_type"], "existing_ca_case")
        self.assertEqual(first_response.data["case_id"], second_response.data["case_id"])
        self.assertEqual(OutageCase.objects.count(), 1)
        mock_assessment.assert_called_once()
        mock_apply_async.assert_called_once()

    def test_fast_track_route_is_removed(self):
        response = self.client.post(
            "/api/reports/fast-track/",
            {"ca_number": "123456789012", "session_id": "session-removed"},
            format="json",
        )

        self.assertEqual(response.status_code, 404)

    def test_fast_track_case_type_choice_removed(self):
        case_type_values = {choice[0] for choice in OutageCase.CASE_TYPE_CHOICES}
        self.assertNotIn("fast_track", case_type_values)


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
        self.assertIn("ขออัปเดตสถานะ", payload["message"])
        self.assertIn("ทีมงานกำลังดำเนินการอยู่", payload["message"])
        self.assertIn("คาดว่าจะจ่ายไฟคืน", payload["message"])
        self.assertNotIn("ครบเวลาประเมินการเข้าหน้างาน", payload["message"])
        self.assertIn(timezone.localtime(etr).strftime("%H:%M น."), payload["message"])
        self.assertNotIn("ภายในประมาณ", payload["message"])
        self.assertNotIn("ETA", payload["message"])
        self.assertNotIn("ETR", payload["message"])
        self.assertNotIn("SLA", payload["message"])
        self.assertNotIn("ช้ากว่ากำหนด", payload["message"])
        self.assertNotIn("ช่างช้า", payload["message"])
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
        self.assertIn("ขออัปเดตสถานะ", payload["message"])
        self.assertIn("ทีมงานกำลังดำเนินการอยู่", payload["message"])
        self.assertIn("คาดว่าจะจ่ายไฟคืน", payload["message"])
        self.assertNotIn("ครบเวลาประเมินการเข้าหน้างาน", payload["message"])
        self.assertIn(
            timezone.localtime(pluem_etr).strftime("%H:%M น."), payload["message"]
        )
        self.assertNotIn("ภายในประมาณ", payload["message"])
        self.assertNotIn("ETA", payload["message"])
        self.assertNotIn("ETR", payload["message"])
        self.assertNotIn("SLA", payload["message"])
        self.assertNotIn("ช้ากว่ากำหนด", payload["message"])
        self.assertNotIn("ช่างช้า", payload["message"])
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
        self.assertIn("ขออัปเดตสถานะ", payload["message"])
        self.assertIn("ทีมงานกำลังดำเนินการอยู่", payload["message"])
        self.assertIn("คาดว่าจะจ่ายไฟคืน", payload["message"])
        self.assertNotIn("ครบเวลาประเมินการเข้าหน้างาน", payload["message"])
        self.assertRegex(payload["message"], r"\d{2}:\d{2} น\.")
        self.assertNotIn("ภายในประมาณ", payload["message"])
        self.assertNotIn("ETA", payload["message"])
        self.assertNotIn("ETR", payload["message"])
        self.assertNotIn("SLA", payload["message"])
        self.assertNotIn("ช้ากว่ากำหนด", payload["message"])
        self.assertNotIn("ช่างช้า", payload["message"])
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
        self.assertIn("ขออัปเดตสถานะ", payload["message"])
        self.assertIn("ทีมงานกำลังดำเนินการอยู่", payload["message"])
        self.assertIn("กำลังประเมินเวลาไฟกลับล่าสุด", payload["message"])
        self.assertNotIn("ครบเวลาประเมินการเข้าหน้างาน", payload["message"])
        self.assertNotIn("ETA", payload["message"])
        self.assertNotIn("ETR", payload["message"])
        self.assertNotIn("SLA", payload["message"])
        self.assertNotIn("ช้ากว่ากำหนด", payload["message"])
        self.assertNotIn("ช่างช้า", payload["message"])
        self.assertNotIn("พี่ปลื้ม", payload["message"])
        self.assertNotIn("OMS", payload["message"])

    @patch("oms.tasks.requests.post")
    def test_eta_timeout_notifies_repairing_status(self, mock_post):
        case = OutageCase.objects.create(
            title="ETA timeout repairing status",
            status="repairing",
            latitude=9.2917,
            longitude=100.926296,
            eta_target_time=timezone.now() - timedelta(minutes=1),
            oms_etr=timezone.now() + timedelta(minutes=30),
        )
        report = CustomerReport.objects.create(
            session_id="session-repairing-timeout",
            ca_number="123456789012",
            related_case=case,
        )

        check_eta_timeout(case.case_id, report.id)

        payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(payload["event_type"], "eta_timeout")
        self.assertIn("ขออัปเดตสถานะ", payload["message"])
        self.assertIn("ทีมงานกำลังดำเนินการอยู่", payload["message"])
        self.assertNotIn("ครบเวลาประเมินการเข้าหน้างาน", payload["message"])
        self.assertNotIn("ETA", payload["message"])
        self.assertNotIn("ETR", payload["message"])
        self.assertNotIn("SLA", payload["message"])

    @patch("oms.tasks.requests.post")
    def test_eta_timeout_skips_restored_status(self, mock_post):
        case = OutageCase.objects.create(
            title="ETA timeout restored status",
            status="restored",
            latitude=9.2917,
            longitude=100.926296,
            eta_target_time=timezone.now() - timedelta(minutes=1),
            oms_etr=timezone.now() + timedelta(minutes=30),
        )
        report = CustomerReport.objects.create(
            session_id="session-restored-timeout",
            ca_number="123456789012",
            related_case=case,
        )

        check_eta_timeout(case.case_id, report.id)

        mock_post.assert_not_called()

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

    @patch("oms.tasks.requests.post")
    def test_etr_timeout_sends_sla_notification(self, mock_post):
        now = timezone.now()
        case = OutageCase.objects.create(
            title="ETR timeout SLA",
            latitude=9.2917,
            longitude=100.926296,
            eta_target_time=now - timedelta(hours=1),
            oms_etr=now - timedelta(minutes=1),
        )
        wrong_sla_reference = case.created_at + timedelta(hours=1)
        OutageCase.objects.filter(pk=case.pk).update(
            oms_etr_updated_at=wrong_sla_reference,
            sla_reference_time=wrong_sla_reference,
            sla_target_time=wrong_sla_reference + timedelta(hours=4),
            sla_reason="oms_etr_update",
        )
        case.refresh_from_db()
        CustomerReport.objects.create(
            session_id="session-etr-timeout",
            ca_number="123456789012",
            related_case=case,
        )

        check_etr_timeout(case.case_id)
        case.refresh_from_db()
        expected_sla_target = case.created_at + timedelta(hours=OutageCase.SLA_HOURS)

        payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(payload["event_type"], "etr_timeout_sla")
        self.assertIn("ขออัปเดต", payload["message"])
        self.assertIn("การจ่ายไฟจะไม่เกินเวลา", payload["message"])
        self.assertEqual(case.sla_reference_time, case.created_at)
        self.assertEqual(case.sla_target_time, expected_sla_target)
        self.assertEqual(case.sla_reason, "case_created")
        self.assertIn(
            timezone.localtime(expected_sla_target).strftime("%H:%M น."),
            payload["message"],
        )
        self.assertNotIn("เหลือเวลา", payload["message"])
        self.assertNotIn("เร่งดำเนินการให้ไม่เกิน", payload["message"])
        self.assertNotIn("เวลาไฟกลับที่ประเมินไว้เลยกำหนด", payload["message"])
        self.assertNotIn("ภายในประมาณ", payload["message"])
        self.assertNotIn("ETA", payload["message"])
        self.assertNotIn("ETR", payload["message"])
        self.assertNotIn("SLA", payload["message"])


class OutageCaseSignalTests(TestCase):
    @patch("oms.signals.check_etr_timeout.apply_async")
    @patch("oms.signals.send_proactive_alert.delay")
    def test_oms_etr_update_sends_etr_update_to_active_sessions(
        self, mock_send_alert, mock_apply_async
    ):
        mock_apply_async.return_value.id = "etr-task-id"
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
        original_sla_reference_time = case.sla_reference_time
        original_sla_target_time = case.sla_target_time
        original_sla_reason = case.sla_reason

        case.oms_etr = timezone.now() + timedelta(hours=1)
        case.save(update_fields=["oms_etr"])

        case.refresh_from_db()
        self.assertIsNotNone(case.oms_etr_updated_at)
        self.assertIsNotNone(case.sla_reference_time)
        self.assertIsNotNone(case.sla_target_time)
        self.assertEqual(case.sla_reason, original_sla_reason)
        self.assertEqual(case.celery_etr_task_id, "etr-task-id")
        self.assertEqual(case.sla_reference_time, original_sla_reference_time)
        self.assertEqual(case.sla_target_time, original_sla_target_time)
        self.assertNotEqual(
            case.sla_target_time, case.oms_etr_updated_at + timedelta(hours=4)
        )
        mock_apply_async.assert_called_once_with(
            args=[case.case_id], eta=case.oms_etr
        )
        self.assertEqual(mock_send_alert.call_count, 2)
        event_types = {
            call.kwargs["event_type"] for call in mock_send_alert.call_args_list
        }
        self.assertEqual(event_types, {"etr_update"})

    @patch("oms.signals.check_etr_timeout.apply_async")
    @patch("oms.signals.send_proactive_alert.delay")
    def test_oms_etr_update_on_mass_outage_anchor_sends_to_active_sessions(
        self, mock_send_alert, mock_apply_async
    ):
        mock_apply_async.return_value.id = "mass-etr-task-id"
        case = OutageCase.objects.create(
            title="Mass outage ETR update case",
            case_type=CASE_TYPE_MASS_OUTAGE,
            latitude=9.2917,
            longitude=100.926296,
        )
        for session_id, ca_number in [
            ("session-a", "123456789012"),
            ("session-b", "123456789013"),
            ("session-c", "123456789014"),
        ]:
            CustomerReport.objects.create(
                session_id=session_id,
                ca_number=ca_number,
                related_case=case,
            )

        case.oms_etr = timezone.now() + timedelta(hours=1)
        case.save(update_fields=["oms_etr"])

        self.assertEqual(mock_send_alert.call_count, 3)
        self.assertEqual(
            {call.kwargs["event_type"] for call in mock_send_alert.call_args_list},
            {"etr_update"},
        )
        self.assertEqual(case.celery_etr_task_id, "mass-etr-task-id")

    @patch("oms.signals.check_etr_timeout.apply_async")
    @patch("oms.signals.send_proactive_alert.delay")
    def test_oms_etr_update_after_pluem_etr_sends_replacement_to_sessions(
        self, mock_send_alert, mock_apply_async
    ):
        mock_apply_async.return_value.id = "etr-replacement-task-id"
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
        self.assertIn("อัปเดตล่าสุด", call.kwargs["message"])
        self.assertIn("คาดว่าจะจ่ายไฟคืน", call.kwargs["message"])
        self.assertNotIn("ภายในประมาณ", call.kwargs["message"])
        self.assertNotIn("ETA", call.kwargs["message"])
        self.assertNotIn("ETR", call.kwargs["message"])
        self.assertNotIn("SLA", call.kwargs["message"])
        self.assertRegex(call.kwargs["message"], r"\d{2}:\d{2} น\.")
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
        call = mock_send_alert.call_args
        self.assertEqual(call.kwargs["event_type"], "closed_loop_prompt")
        self.assertIn("กรุณาเลือกสถานะไฟฟ้า", call.kwargs["message"])
        self.assertNotIn("พิมพ์", call.kwargs["message"])
        self.assertNotIn("เปิดเคสเร่งด่วน", call.kwargs["message"])
        self.assertNotIn("เบรกเกอร์", call.kwargs["message"])
        self.assertNotIn("คัตเอาต์", call.kwargs["message"])

    @patch("oms.signals.send_proactive_alert.delay")
    def test_restored_status_sends_closed_loop_once_per_session(self, mock_send_alert):
        case = OutageCase.objects.create(
            title="Restored duplicate session case",
            latitude=9.2917,
            longitude=100.926296,
        )
        CustomerReport.objects.create(
            session_id="session-duplicate",
            ca_number="123456789012",
            related_case=case,
        )
        CustomerReport.objects.create(
            session_id="session-duplicate",
            ca_number="123456789013",
            related_case=case,
        )

        case.status = "restored"
        case.save(update_fields=["status"])

        self.assertEqual(mock_send_alert.call_count, 1)
        self.assertEqual(
            mock_send_alert.call_args.kwargs["event_type"],
            "closed_loop_prompt",
        )


class OpsWebhookConsoleTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_ops_webhook_page_renders(self):
        response = self.client.get("/ops/webhook/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "OMS Webhook Console")
        self.assertContains(response, "Export")

    def test_chat_page_contains_closed_loop_button_options(self):
        response = self.client.get("/chat/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "เลือกสถานะไฟฟ้า")
        self.assertContains(response, "ไฟมาแล้ว / ใช้งานได้แล้ว")
        self.assertContains(response, "ยังไม่มีไฟ / แจ้งเหตุใหม่")
        self.assertContains(response, "closed_loop_prompt")
        content = response.content.decode()
        self.assertNotIn("<select", content)
        self.assertNotIn("ระบบ OMS", content)
        self.assertNotIn("เปิดเคสเร่งด่วน", content)
        self.assertNotIn("เคสเร่งด่วน", content)

    def test_ops_map_page_renders(self):
        response = self.client.get("/ops/map/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "OMS Location Map")
        self.assertContains(response, "Legend")
        self.assertContains(response, "Filter")
        self.assertContains(response, "Radius selection")
        content = response.content.decode()
        self.assertIn(
            'id="showAllInput" name="show_all" type="checkbox" checked',
            content,
        )
        self.assertIn('id="radiusKmInput" type="number"', content)
        self.assertIn('value="0.5"', content)
        self.assertIn('const DEFAULT_RADIUS_KM = Number.parseFloat("0.5")', content)
        self.assertIn("L.featureGroup()", content)
        self.assertNotIn("leaflet.markercluster", content)
        self.assertNotIn("markerClusterGroup", content)

    def test_ops_map_data_returns_customer_location_with_case_metadata(self):
        location = CustomerLocation.objects.create(
            ca_number="123456789012",
            fullname="Somchai Map",
            pea_area="PEA Rangsit",
            latitude=13.7563,
            longitude=100.5018,
        )
        case = OutageCase.objects.create(
            title="Map normal case",
            status="reported",
            affected_ca_numbers=[location.ca_number],
            latitude=13.7563,
            longitude=100.5018,
        )
        CustomerReport.objects.create(
            session_id="session-map",
            ca_number=location.ca_number,
            related_case=case,
        )

        response = self.client.get("/ops/map/data/")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["case_link_radius_km"], CASE_LINK_RADIUS_KM)
        self.assertEqual(payload["summary"]["total_locations"], 1)
        marker = payload["markers"][0]
        self.assertEqual(marker["ca_number"], location.ca_number)
        self.assertEqual(marker["customer_name"], "Somchai Map")
        self.assertEqual(marker["case"]["case_id"], str(case.case_id))
        self.assertEqual(marker["case"]["case_type"], CASE_TYPE_NORMAL)
        self.assertEqual(marker["marker"]["status"], "reported")
        self.assertNotIn("is_fast_track", marker["marker"])

    def test_ops_map_show_all_controls_locations_without_cases(self):
        assigned_location = CustomerLocation.objects.create(
            ca_number="123456789012",
            fullname="Assigned Map",
            pea_area="PEA Rangsit",
            latitude=13.7563,
            longitude=100.5018,
        )
        unassigned_location = CustomerLocation.objects.create(
            ca_number="999999999999",
            fullname="No Case Map",
            pea_area="PEA Khlong Luang",
            latitude=14.0081,
            longitude=100.5247,
        )
        case = OutageCase.objects.create(
            title="Assigned map case",
            affected_ca_numbers=[assigned_location.ca_number],
            latitude=13.7563,
            longitude=100.5018,
        )
        CustomerReport.objects.create(
            session_id="session-map-assigned",
            ca_number=assigned_location.ca_number,
            related_case=case,
        )

        default_response = self.client.get("/ops/map/data/")
        show_all_response = self.client.get("/ops/map/data/", {"show_all": "true"})

        self.assertEqual(
            {marker["ca_number"] for marker in default_response.json()["markers"]},
            {assigned_location.ca_number},
        )
        self.assertEqual(
            {marker["ca_number"] for marker in show_all_response.json()["markers"]},
            {assigned_location.ca_number, unassigned_location.ca_number},
        )
        no_case_marker = [
            marker
            for marker in show_all_response.json()["markers"]
            if marker["ca_number"] == unassigned_location.ca_number
        ][0]
        self.assertIsNone(no_case_marker["case"])
        self.assertEqual(no_case_marker["marker"]["status"], "no_case")

    def test_ops_map_data_filters_by_status_type_date_and_text(self):
        normal_location = CustomerLocation.objects.create(
            ca_number="123456789012",
            fullname="Normal Customer",
            pea_area="PEA Rangsit",
            latitude=13.7563,
            longitude=100.5018,
        )
        restored_location = CustomerLocation.objects.create(
            ca_number="123456789013",
            fullname="Restored Customer",
            pea_area="PEA Khlong Luang",
            latitude=14.0081,
            longitude=100.5247,
        )
        normal_case = OutageCase.objects.create(
            title="Normal Search Case",
            case_type=CASE_TYPE_NORMAL,
            status="repairing",
            affected_ca_numbers=[normal_location.ca_number],
            latitude=13.7563,
            longitude=100.5018,
        )
        restored_case = OutageCase.objects.create(
            title="Restored Search Case",
            status="restored",
            affected_ca_numbers=[restored_location.ca_number],
            latitude=14.0081,
            longitude=100.5247,
        )
        CustomerReport.objects.create(
            session_id="session-map-normal",
            ca_number=normal_location.ca_number,
            related_case=normal_case,
        )
        CustomerReport.objects.create(
            session_id="session-map-restored",
            ca_number=restored_location.ca_number,
            related_case=restored_case,
        )

        text_response = self.client.get("/ops/map/data/", {"search": "rangsit"})
        type_response = self.client.get("/ops/map/data/", {"case_type": "normal"})
        status_response = self.client.get("/ops/map/data/", {"status": "restored"})
        date_response = self.client.get(
            "/ops/map/data/",
            {"created_from": timezone.localdate().isoformat()},
        )
        future_response = self.client.get(
            "/ops/map/data/",
            {"created_from": (timezone.localdate() + timedelta(days=1)).isoformat()},
        )

        self.assertEqual(
            {marker["ca_number"] for marker in text_response.json()["markers"]},
            {normal_location.ca_number},
        )
        self.assertEqual(
            {marker["ca_number"] for marker in type_response.json()["markers"]},
            {normal_location.ca_number},
        )
        self.assertEqual(
            {marker["ca_number"] for marker in status_response.json()["markers"]},
            {restored_location.ca_number},
        )
        self.assertIn(
            normal_location.ca_number,
            {marker["ca_number"] for marker in date_response.json()["markers"]},
        )
        self.assertEqual(future_response.json()["markers"], [])

    def test_ops_map_case_search_includes_all_customer_locations_in_case(self):
        first_location = CustomerLocation.objects.create(
            ca_number="123456789012",
            fullname="First Case Customer",
            pea_area="PEA Rangsit",
            latitude=13.7563,
            longitude=100.5018,
        )
        second_location = CustomerLocation.objects.create(
            ca_number="123456789013",
            fullname="Second Case Customer",
            pea_area="PEA Rangsit",
            latitude=13.7564,
            longitude=100.5019,
        )
        case = OutageCase.objects.create(
            title="Shared CA map case",
            affected_ca_numbers=[first_location.ca_number, second_location.ca_number],
            latitude=13.7563,
            longitude=100.5018,
        )
        CustomerReport.objects.create(
            session_id="session-map-shared",
            ca_number=first_location.ca_number,
            related_case=case,
        )
        CustomerReport.objects.create(
            session_id="session-map-shared-second",
            ca_number=second_location.ca_number,
            related_case=case,
        )

        response = self.client.get(
            "/ops/map/data/",
            {"search": first_location.ca_number},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            {marker["ca_number"] for marker in response.json()["markers"]},
            {first_location.ca_number, second_location.ca_number},
        )

    def test_ops_map_active_view_excludes_merged_child_cases(self):
        anchor_location = CustomerLocation.objects.create(
            ca_number="123456789012",
            fullname="Anchor Mass Map",
            pea_area="PEA Rangsit",
            latitude=13.7563,
            longitude=100.5018,
        )
        merged_location = CustomerLocation.objects.create(
            ca_number="123456789013",
            fullname="Merged Child Map",
            pea_area="PEA Rangsit",
            latitude=13.7564,
            longitude=100.5019,
        )
        anchor_case = OutageCase.objects.create(
            title="Anchor mass outage map case",
            case_type=CASE_TYPE_MASS_OUTAGE,
            latitude=13.7563,
            longitude=100.5018,
        )
        merged_case = OutageCase.objects.create(
            title="Merged map child case",
            status=STATUS_MERGED,
            merged_into=anchor_case,
            merged_at=timezone.now(),
            latitude=13.7564,
            longitude=100.5019,
        )
        CustomerReport.objects.create(
            session_id="session-map-anchor",
            ca_number=anchor_location.ca_number,
            related_case=anchor_case,
        )
        CustomerReport.objects.create(
            session_id="session-map-merged",
            ca_number=merged_location.ca_number,
            related_case=merged_case,
        )

        active_response = self.client.get("/ops/map/data/")
        merged_response = self.client.get("/ops/map/data/", {"status": "merged"})

        self.assertEqual(
            {marker["ca_number"] for marker in active_response.json()["markers"]},
            {anchor_location.ca_number},
        )
        self.assertEqual(
            {marker["ca_number"] for marker in merged_response.json()["markers"]},
            {merged_location.ca_number},
        )
        self.assertEqual(
            merged_response.json()["markers"][0]["case"]["status"],
            STATUS_MERGED,
        )

    def test_ops_cases_export_csv_uses_current_filters(self):
        matching_case = OutageCase.objects.create(
            title="Matching CSV case",
            affected_ca_numbers=["123456789012"],
            latitude=9.2917,
            longitude=100.926296,
        )
        OutageCase.objects.create(
            title="Other CSV case",
            affected_ca_numbers=["999999999999"],
            latitude=9.2918,
            longitude=100.926396,
        )
        OutageCase.objects.create(
            title="Restored CSV case",
            status="restored",
            affected_ca_numbers=["123456789012"],
            latitude=9.2919,
            longitude=100.926496,
        )

        response = self.client.get(
            "/ops/cases/export/",
            {
                "include_restored": "false",
                "status": "reported",
                "search": "123456789012",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn("ops-cases-", response["Content-Disposition"])

        content = response.content.decode("utf-8-sig")
        rows = list(csv.reader(io.StringIO(content)))
        self.assertEqual(rows[0][:5], ["LV", "Case ID", "Title", "Case Type", "Status"])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1][1], str(matching_case.case_id))
        self.assertEqual(rows[1][2], "Matching CSV case")
        self.assertIn("123456789012", rows[1][6])

    @patch("oms.signals.check_etr_timeout.apply_async")
    @patch("oms.signals.send_proactive_alert.delay")
    def test_ops_action_sets_etr_for_selected_cases(
        self, mock_send_alert, mock_apply_async
    ):
        mock_apply_async.return_value.id = "ops-etr-task-id"
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
        self.assertEqual(selected_case.celery_etr_task_id, "ops-etr-task-id")
        self.assertIsNone(untouched_case.oms_etr)
        mock_send_alert.assert_called_once()

    @patch("oms.signals.check_eta_timeout.apply_async")
    def test_ops_action_sets_eta_to_now_for_selected_cases(self, mock_apply_async):
        mock_apply_async.return_value.id = "eta-now-task-id"
        selected_case = OutageCase.objects.create(
            title="Selected ETA case",
            latitude=9.2917,
            longitude=100.926296,
            eta_target_time=timezone.now() + timedelta(minutes=30),
        )
        untouched_eta = timezone.now() + timedelta(minutes=45)
        untouched_case = OutageCase.objects.create(
            title="Untouched ETA case",
            latitude=9.2918,
            longitude=100.926396,
            eta_target_time=untouched_eta,
        )
        report = CustomerReport.objects.create(
            session_id="session-ops-eta",
            ca_number="123456789012",
            related_case=selected_case,
        )

        before = timezone.now()
        response = self.client.post(
            "/ops/cases/action/",
            {
                "action": "set_eta_now",
                "target_mode": "selected",
                "case_ids": [str(selected_case.case_id)],
            },
            format="json",
        )
        after = timezone.now()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["updated_count"], 1)
        selected_case.refresh_from_db()
        untouched_case.refresh_from_db()
        self.assertGreaterEqual(selected_case.eta_target_time, before)
        self.assertLessEqual(selected_case.eta_target_time, after)
        self.assertEqual(untouched_case.eta_target_time, untouched_eta)
        self.assertEqual(selected_case.celery_eta_task_id, "eta-now-task-id")
        mock_apply_async.assert_called_once_with(
            args=[selected_case.case_id, report.id],
            eta=selected_case.eta_target_time,
        )

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
        merged_case = OutageCase.objects.create(
            title="Merged restore skip case",
            status=STATUS_MERGED,
            latitude=9.2919,
            longitude=100.926496,
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
        merged_case.refresh_from_db()
        self.assertEqual(active_case.status, "restored")
        self.assertEqual(already_restored_case.status, "restored")
        self.assertEqual(merged_case.status, STATUS_MERGED)


class CsvExportAdminTests(TestCase):
    def test_registered_admin_tables_have_csv_export_action(self):
        admin_site = AdminSite()
        admins = [
            OutageCaseAdmin(OutageCase, admin_site),
            CustomerLocationAdmin(CustomerLocation, admin_site),
            CustomerReportAdmin(CustomerReport, admin_site),
            OutageRestorationLogAdmin(OutageRestorationLog, admin_site),
        ]

        for model_admin in admins:
            self.assertIn("export_selected_csv", model_admin.actions)

    def test_outage_admin_countdown_sla_states(self):
        model_admin = OutageCaseAdmin(OutageCase, AdminSite())
        future_case = OutageCase.objects.create(
            title="Future SLA case",
            latitude=9.2917,
            longitude=100.926296,
            sla_target_time=timezone.now() + timedelta(hours=2),
        )
        expired_case = OutageCase.objects.create(
            title="Expired SLA case",
            latitude=9.2918,
            longitude=100.926396,
            sla_target_time=timezone.now() - timedelta(minutes=1),
        )

        self.assertIn("เหลือ", str(model_admin.countdown_sla(future_case)))
        self.assertIn("เลย SLA", str(model_admin.countdown_sla(expired_case)))

    def test_admin_export_selected_csv_returns_model_rows(self):
        location = CustomerLocation.objects.create(
            ca_number="123456789012",
            fullname="CSV User",
            latitude=9.2917,
            longitude=100.926296,
        )
        model_admin = CustomerLocationAdmin(CustomerLocation, AdminSite())
        request = RequestFactory().get("/admin/oms/customerlocation/")

        response = model_admin.export_selected_csv(
            request,
            CustomerLocation.objects.filter(id=location.id),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn("customerlocation-", response["Content-Disposition"])

        content = response.content.decode("utf-8-sig")
        rows = list(csv.reader(io.StringIO(content)))
        self.assertEqual(rows[0][:6], ["id", "timestamp", "prefix", "fullname", "address", "ca_number"])
        self.assertEqual(rows[1][3], "CSV User")
        self.assertEqual(rows[1][5], "123456789012")


class DistanceLinkingTests(TestCase):
    def test_case_link_radius_constant_is_half_km(self):
        self.assertEqual(CASE_LINK_RADIUS_KM, 0.5)
        self.assertEqual(MASS_OUTAGE_CONFIRMATION_COUNT, 3)

    def test_calculate_distance_allows_effectively_same_coordinates(self):
        distance = calculate_distance(14.0626077, 100.6109053, 14.0626077, 100.6109053)

        self.assertEqual(distance, 0)

    def test_calculate_distance_returns_finite_inside_half_km_radius(self):
        distance = calculate_distance(14.0000, 100.0000, 14.0030, 100.0000)

        self.assertFalse(math.isinf(distance))
        self.assertLessEqual(distance, CASE_LINK_RADIUS_KM)

    def test_calculate_distance_returns_infinite_outside_half_km_radius(self):
        distance = calculate_distance(14.0000, 100.0000, 14.0100, 100.0000)

        self.assertTrue(math.isinf(distance))
