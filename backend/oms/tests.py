import csv
import io
from datetime import timedelta
from unittest.mock import Mock, patch

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
    STATUS_MERGED,
)
from .models import (
    AgentJob,
    ChatMessage,
    CustomerLocation,
    CustomerReport,
    OutageCase,
    OutageRestorationLog,
)
from .tasks import (
    SLA_EXPIRED_CLOSED_LOOP_KIND,
    SLA_EXPIRED_CLOSED_LOOP_MESSAGE,
    check_eta_timeout,
    check_etr_timeout,
    check_sla_timeout,
    process_agent_job,
    send_proactive_alert,
)


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

    @patch("oms.views_api.check_sla_timeout.apply_async")
    @patch("oms.views_api.check_eta_timeout.apply_async")
    @patch("oms.views_api.get_pea_assessment")
    def test_session_switching_ca_resolves_previous_active_report(
        self, mock_assessment, mock_eta_apply_async, mock_sla_apply_async
    ):
        mock_eta_apply_async.return_value.id = "eta-task-id"
        mock_sla_apply_async.return_value.id = "sla-task-id"
        mock_assessment.return_value = self._assessment_payload()
        for ca_number, latitude in [
            ("123456789012", 14.0),
            ("123456789013", 14.1),
        ]:
            CustomerLocation.objects.create(
                ca_number=ca_number,
                fullname=ca_number,
                latitude=latitude,
                longitude=100.0,
            )

        self._post_sync("one-active-session", "123456789012")
        self._post_sync("one-active-session", "123456789013")

        reports = CustomerReport.objects.filter(
            session_id="one-active-session"
        ).order_by("created_at")
        self.assertEqual(reports.count(), 2)
        self.assertTrue(reports[0].is_resolved)
        self.assertFalse(reports[1].is_resolved)
        self.assertEqual(reports[1].ca_number, "123456789013")

    @patch("oms.signals.send_proactive_alert.delay")
    @patch("oms.views_api.get_pea_assessment")
    @patch("oms.views_api.check_sla_timeout.apply_async")
    @patch("oms.views_api.check_eta_timeout.apply_async")
    def test_new_event_uses_customer_location_and_assessment(
        self, mock_eta_apply_async, mock_sla_apply_async, mock_assessment, mock_send_alert
    ):
        mock_eta_apply_async.return_value.id = "eta-task-id"
        mock_sla_apply_async.return_value.id = "sla-task-id"
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
        self.assertIsNotNone(case.celery_sla_task_id)
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
        CustomerLocation.objects.create(
            ca_number="123456789012",
            fullname="Same CA active user",
            latitude=9.2917,
            longitude=100.926296,
        )
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
    @patch("oms.views_api.check_sla_timeout.apply_async")
    @patch("oms.views_api.check_eta_timeout.apply_async")
    def test_new_case_attaches_waiting_same_ca_sessions(
        self, mock_eta_apply_async, mock_sla_apply_async, mock_assessment
    ):
        mock_eta_apply_async.return_value.id = "eta-task-id"
        mock_sla_apply_async.return_value.id = "sla-task-id"
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
    @patch("oms.views_api.check_sla_timeout.apply_async")
    @patch("oms.views_api.check_eta_timeout.apply_async")
    def test_second_nearby_ca_stays_new_event_until_threshold(
        self, mock_eta_apply_async, mock_sla_apply_async, mock_assessment, mock_send_alert
    ):
        mock_eta_apply_async.return_value.id = "eta-task-id"
        mock_sla_apply_async.return_value.id = "sla-task-id"
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

    @patch("oms.views_api.send_proactive_alert.delay")
    @patch("oms.views_api.get_pea_assessment")
    @patch("oms.views_api.check_sla_timeout.apply_async")
    @patch("oms.views_api.check_eta_timeout.apply_async")
    def test_third_nearby_ca_stays_separate_without_oms_group_event(
        self, mock_eta_apply_async, mock_sla_apply_async, mock_assessment, mock_send_alert
    ):
        mock_eta_apply_async.return_value.id = "eta-task-id"
        mock_sla_apply_async.return_value.id = "sla-task-id"
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
        self.assertEqual(third_response.data["event_type"], "new_event")
        self.assertEqual(third_response.data["case_type"], CASE_TYPE_NORMAL)
        self.assertEqual(OutageCase.objects.count(), 3)
        self.assertEqual(
            set(OutageCase.objects.values_list("case_type", flat=True)),
            {CASE_TYPE_NORMAL},
        )

        for ca_number in ca_numbers:
            report = CustomerReport.objects.get(ca_number=ca_number)
            self.assertEqual(report.related_case.affected_ca_numbers, [ca_number])
        mock_send_alert.assert_not_called()

    @patch("oms.views_api.celery_app.control.revoke")
    @patch("oms.views_api.send_proactive_alert.delay")
    @patch("oms.views_api.get_pea_assessment")
    @patch("oms.views_api.check_etr_timeout.apply_async")
    @patch("oms.views_api.check_sla_timeout.apply_async")
    @patch("oms.views_api.check_eta_timeout.apply_async")
    def test_oms_group_event_attaches_reports_and_closed_loop_to_all_sessions(
        self,
        mock_eta_apply_async,
        mock_sla_apply_async,
        mock_etr_apply_async,
        mock_assessment,
        mock_send_alert,
        _mock_revoke,
    ):
        mock_eta_apply_async.return_value.id = "eta-task-id"
        mock_sla_apply_async.return_value.id = "sla-task-id"
        mock_etr_apply_async.return_value.id = "etr-task-id"
        mock_assessment.return_value = self._assessment_payload()
        ca_numbers = ["123456789012", "123456789013"]
        sessions = ["session-a", "session-b"]
        for ca_number, latitude in zip(ca_numbers, [14.0000, 14.0020]):
            CustomerLocation.objects.create(
                ca_number=ca_number,
                fullname=f"Mass restore CA {ca_number}",
                latitude=latitude,
                longitude=100.0000,
            )

        original_case = OutageCase.objects.create(
            title="Original restored A case",
            status="restored",
            latitude=14.0000,
            longitude=100.0000,
        )
        CustomerReport.objects.create(
            session_id=sessions[0],
            ca_number=ca_numbers[0],
            related_case=original_case,
            is_resolved=True,
        )

        for session_id, ca_number in zip(sessions, ca_numbers):
            response = self._post_sync(session_id, ca_number)
            self.assertEqual(response.data["event_type"], "new_event")

        single_case_ids = set(OutageCase.objects.values_list("case_id", flat=True))
        group_case_id = "11111111-1111-1111-1111-111111111111"
        open_response = self.client.post(
            "/api/oms/events/",
            {
                "event_type": "case_opened",
                "case": {
                    "case_id": group_case_id,
                    "status": "reported",
                    "affected_ca_numbers": ca_numbers,
                    "oms_etr": (timezone.now() + timedelta(hours=2)).isoformat(),
                },
            },
            format="json",
        )

        self.assertEqual(open_response.status_code, 200)
        self.assertEqual(open_response.data["merged_count"], len(ca_numbers))
        self.assertTrue(open_response.data["etr_timer_scheduled"])
        anchor = OutageCase.objects.get(case_id=group_case_id)
        self.assertEqual(anchor.case_type, CASE_TYPE_MASS_OUTAGE)
        self.assertNotEqual(anchor.case_id, original_case.case_id)
        self.assertEqual(set(anchor.affected_ca_numbers), set(ca_numbers))
        self.assertIsNotNone(anchor.celery_etr_task_id)
        self.assertIsNotNone(anchor.oms_etr_updated_at)
        self.assertEqual(
            set(
                OutageCase.objects.filter(
                    case_id__in=single_case_ids,
                    status=STATUS_MERGED,
                    merged_into=anchor,
                ).values_list("case_id", flat=True)
            ),
            single_case_ids - {original_case.case_id},
        )
        for ca_number in ca_numbers:
            report = CustomerReport.objects.get(ca_number=ca_number, is_resolved=False)
            self.assertEqual(report.related_case, anchor)
        self.assertEqual(
            {call.kwargs["event_type"] for call in mock_send_alert.call_args_list},
            {"mass_outage"},
        )
        mock_etr_apply_async.assert_called_once_with(
            args=[anchor.case_id],
            eta=anchor.oms_etr,
            task_id=anchor.celery_etr_task_id,
        )

        mock_send_alert.reset_mock()
        close_response = self.client.post(
            "/api/oms/events/",
            {
                "event_type": "case_closed",
                "case": {
                    "case_id": group_case_id,
                    "status": "restored",
                },
            },
            format="json",
        )

        self.assertEqual(close_response.status_code, 200)
        self.assertEqual(mock_send_alert.call_count, 2)
        alerted_reports = [
            CustomerReport.objects.get(id=call.kwargs["report_id"])
            for call in mock_send_alert.call_args_list
        ]
        self.assertEqual({report.session_id for report in alerted_reports}, set(sessions))
        self.assertEqual(
            {call.kwargs["event_type"] for call in mock_send_alert.call_args_list},
            {"closed_loop_prompt"},
        )
        self.assertFalse(
            CustomerReport.objects.filter(
                session_id__in=sessions,
                related_case=anchor,
                is_resolved=False,
            ).exists()
        )

    @patch("oms.views_api.check_sla_timeout.apply_async")
    @patch("oms.views_api.send_proactive_alert.delay")
    def test_oms_merge_moves_every_active_report_from_superseded_case(
        self, mock_send_alert, mock_sla_apply_async
    ):
        child = OutageCase.objects.create(
            title="Superseded multi-CA case",
            affected_ca_numbers=["123456789012", "123456789099"],
        )
        reports = [
            CustomerReport.objects.create(
                session_id="merge-session-a",
                ca_number="123456789012",
                related_case=child,
            ),
            CustomerReport.objects.create(
                session_id="merge-session-b",
                ca_number="123456789099",
                related_case=child,
            ),
        ]

        response = self.client.post(
            "/api/oms/events/",
            {
                "event_type": "case_opened",
                "case": {
                    "case_id": "22222222-2222-2222-2222-222222222222",
                    "status": "reported",
                    "affected_ca_numbers": ["123456789012", "123456789013"],
                },
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        child.refresh_from_db()
        anchor = OutageCase.objects.get(
            case_id="22222222-2222-2222-2222-222222222222"
        )
        self.assertEqual(child.status, STATUS_MERGED)
        self.assertEqual(child.merged_into, anchor)
        for report in reports:
            report.refresh_from_db()
            self.assertEqual(report.related_case, anchor)

    @patch("oms.views_api.check_sla_timeout.apply_async")
    @patch("oms.views_api.send_proactive_alert.delay")
    def test_oms_group_event_revives_resolved_affected_ca_report_for_notification(
        self, mock_send_alert, mock_sla_apply_async
    ):
        active_ca = "020001291771"
        resolved_ca = "020025790865"
        active_report = CustomerReport.objects.create(
            session_id="active-ca-session",
            ca_number=active_ca,
        )
        resolved_report = CustomerReport.objects.create(
            session_id="resolved-ca-session",
            ca_number=resolved_ca,
            is_resolved=True,
        )

        response = self.client.post(
            "/api/oms/events/",
            {
                "event_type": "case_opened",
                "case": {
                    "case_id": "44444444-4444-4444-4444-444444444444",
                    "status": "reported",
                    "affected_ca_numbers": [active_ca, resolved_ca],
                    "oms_etr": (timezone.now() + timedelta(hours=2)).isoformat(),
                },
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        anchor = OutageCase.objects.get(case_id="44444444-4444-4444-4444-444444444444")
        active_report.refresh_from_db()
        resolved_report.refresh_from_db()
        self.assertEqual(active_report.related_case, anchor)
        self.assertEqual(resolved_report.related_case, anchor)
        self.assertFalse(resolved_report.is_resolved)
        calls_by_report_id = {
            call.kwargs["report_id"]: call.kwargs["event_type"]
            for call in mock_send_alert.call_args_list
        }
        self.assertEqual(
            calls_by_report_id,
            {
                active_report.id: "mass_outage",
                resolved_report.id: "mass_outage",
            },
        )

    @patch("oms.views_api.check_sla_timeout.apply_async")
    @patch("oms.signals.check_etr_timeout.apply_async")
    @patch("oms.signals.send_proactive_alert.delay")
    def test_single_ca_oms_case_merges_into_active_case_and_notifies_etr(
        self, mock_send_alert, mock_etr_apply_async, mock_sla_apply_async
    ):
        mock_etr_apply_async.return_value.id = "single-ca-etr-task-id"
        ca_number = "123456789012"
        anchor = OutageCase.objects.create(
            title="Local active single CA",
            affected_ca_numbers=[ca_number],
            eta_target_time=timezone.now() + timedelta(minutes=25),
            pluem_etr_minutes=67,
            pluem_etr_target_time=timezone.now() + timedelta(minutes=67),
        )
        report = CustomerReport.objects.create(
            session_id="single-ca-oms-session",
            ca_number=ca_number,
            related_case=anchor,
        )
        oms_etr = timezone.now() + timedelta(hours=2)

        response = self.client.post(
            "/api/oms/events/",
            {
                "event_type": "case_opened",
                "case": {
                    "case_id": "33333333-3333-3333-3333-333333333333",
                    "external_event_id": "OMS-SINGLE-CA",
                    "status": "reported",
                    "affected_ca_numbers": [ca_number],
                    "oms_etr": oms_etr.isoformat(),
                },
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["merged_count"], 1)
        child = OutageCase.objects.get(external_event_id="OMS-SINGLE-CA")
        anchor.refresh_from_db()
        report.refresh_from_db()
        self.assertEqual(child.status, STATUS_MERGED)
        self.assertEqual(child.merged_into, anchor)
        self.assertEqual(anchor.oms_etr, oms_etr)
        self.assertIsNotNone(anchor.celery_etr_task_id)
        self.assertEqual(report.related_case, anchor)
        mock_send_alert.assert_called_once()
        self.assertEqual(mock_send_alert.call_args.kwargs["report_id"], report.id)
        self.assertEqual(mock_send_alert.call_args.kwargs["event_type"], "etr_update")

    @patch("oms.signals.check_etr_timeout.apply_async")
    @patch("oms.signals.send_proactive_alert.delay")
    def test_callback_for_merged_child_is_ignored(
        self, mock_send_alert, mock_etr_apply_async
    ):
        anchor_etr = timezone.now() + timedelta(hours=2)
        anchor = OutageCase.objects.create(title="Merge anchor", oms_etr=anchor_etr)
        child = OutageCase.objects.create(
            title="Merged callback child",
            status=STATUS_MERGED,
            external_event_id="OLD-OMS-EVENT",
            merged_into=anchor,
            merged_at=timezone.now(),
        )

        response = self.client.post(
            "/api/oms/events/",
            {
                "event_type": "case_opened",
                "case": {
                    "external_event_id": "OLD-OMS-EVENT",
                    "status": "reported",
                    "oms_etr": (anchor_etr + timedelta(hours=1)).isoformat(),
                },
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(str(response.data["case_id"]), str(anchor.case_id))
        child.refresh_from_db()
        anchor.refresh_from_db()
        self.assertEqual(child.status, STATUS_MERGED)
        self.assertEqual(anchor.oms_etr, anchor_etr)
        mock_etr_apply_async.assert_not_called()
        mock_send_alert.assert_not_called()

    @patch("oms.signals.check_etr_timeout.apply_async")
    @patch("oms.signals.send_proactive_alert.delay")
    def test_repeated_oms_etr_updates_keep_latest_time_without_mass_rebroadcast(
        self, mock_send_alert, mock_etr_apply_async
    ):
        first_etr = timezone.now() + timedelta(hours=1)
        later_etr = first_etr + timedelta(hours=1)
        case = OutageCase.objects.create(
            title="Repeated OMS ETR",
            case_type=CASE_TYPE_MASS_OUTAGE,
            external_event_id="REPEATED-ETR",
            affected_ca_numbers=["123456789012", "123456789013"],
            oms_etr=first_etr,
        )
        CustomerReport.objects.create(
            session_id="repeated-etr-session",
            ca_number="123456789012",
            related_case=case,
        )

        def post_etr(value):
            return self.client.post(
                "/api/oms/events/",
                {
                    "event_type": "case_opened",
                    "case": {
                        "external_event_id": "REPEATED-ETR",
                        "status": "reported",
                        "affected_ca_numbers": ["123456789012", "123456789013"],
                        "oms_etr": value,
                    },
                },
                format="json",
            )

        self.assertEqual(post_etr(later_etr.isoformat()).status_code, 200)
        case.refresh_from_db()
        self.assertEqual(case.oms_etr, later_etr)
        self.assertEqual(mock_send_alert.call_count, 1)
        self.assertEqual(
            mock_send_alert.call_args.kwargs["event_type"], "etr_update"
        )

        mock_send_alert.reset_mock()
        for ignored_value in [first_etr.isoformat(), later_etr.isoformat(), None]:
            self.assertEqual(post_etr(ignored_value).status_code, 200)
        case.refresh_from_db()
        self.assertEqual(case.oms_etr, later_etr)
        mock_send_alert.assert_not_called()

    @patch("oms.signals.check_etr_timeout.apply_async")
    @patch("oms.signals.send_proactive_alert.delay")
    def test_group_expansion_and_later_etr_notify_each_session_once(
        self, mock_send_alert, mock_etr_apply_async
    ):
        first_etr = timezone.now() + timedelta(hours=1)
        anchor = OutageCase.objects.create(
            title="Expanding OMS group",
            case_type=CASE_TYPE_MASS_OUTAGE,
            external_event_id="EXPANDING-GROUP",
            affected_ca_numbers=["123456789012"],
            oms_etr=first_etr,
        )
        existing_report = CustomerReport.objects.create(
            session_id="existing-group-session",
            ca_number="123456789012",
            related_case=anchor,
        )
        child = OutageCase.objects.create(
            title="Newly merged child",
            affected_ca_numbers=["123456789013"],
        )
        new_report = CustomerReport.objects.create(
            session_id="new-group-session",
            ca_number="123456789013",
            related_case=child,
        )

        response = self.client.post(
            "/api/oms/events/",
            {
                "event_type": "case_opened",
                "case": {
                    "external_event_id": "EXPANDING-GROUP",
                    "status": "reported",
                    "affected_ca_numbers": ["123456789012", "123456789013"],
                    "oms_etr": (first_etr + timedelta(hours=1)).isoformat(),
                },
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(mock_send_alert.call_count, 2)
        calls_by_report = {
            call.kwargs["report_id"]: call.kwargs["event_type"]
            for call in mock_send_alert.call_args_list
        }
        self.assertEqual(
            calls_by_report,
            {
                existing_report.id: "etr_update",
                new_report.id: "mass_outage",
            },
        )

    @patch("oms.views_api.get_pea_assessment")
    @patch("oms.views_api.check_sla_timeout.apply_async")
    @patch("oms.views_api.check_eta_timeout.apply_async")
    def test_new_ca_inside_existing_mass_outage_only_links_when_ca_is_affected(
        self, mock_eta_apply_async, mock_sla_apply_async, mock_assessment
    ):
        mock_eta_apply_async.return_value.id = "eta-task-id"
        mock_sla_apply_async.return_value.id = "sla-task-id"
        mock_assessment.return_value = self._assessment_payload()
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
        self.assertEqual(response.data["event_type"], "new_event")
        self.assertNotEqual(str(response.data["case_id"]), str(mass_case.case_id))
        self.assertEqual(OutageCase.objects.count(), 2)
        mock_assessment.assert_called_once()

    @patch("oms.views_api.get_pea_assessment")
    @patch("oms.views_api.check_sla_timeout.apply_async")
    @patch("oms.views_api.check_eta_timeout.apply_async")
    def test_sync_report_creates_new_case_outside_active_case_radius(
        self, mock_eta_apply_async, mock_sla_apply_async, mock_assessment
    ):
        mock_eta_apply_async.return_value.id = "eta-task-id"
        mock_sla_apply_async.return_value.id = "sla-task-id"
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
    @patch("oms.views_api.check_sla_timeout.apply_async")
    @patch("oms.views_api.check_eta_timeout.apply_async")
    def test_sync_report_after_restored_case_creates_normal_case_with_eta(
        self, mock_eta_apply_async, mock_sla_apply_async, mock_assessment
    ):
        mock_eta_apply_async.return_value.id = "restored-new-eta-task-id"
        mock_sla_apply_async.return_value.id = "restored-new-sla-task-id"
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
        self.assertIsNotNone(case.celery_sla_task_id)
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
        mock_eta_apply_async.assert_called_once_with(
            args=[case.case_id, response.data["report_id"]],
            eta=case.eta_target_time,
            task_id=case.celery_eta_task_id,
        )
        mock_sla_apply_async.assert_called_once_with(
            args=[case.case_id],
            eta=case.sla_target_time,
            task_id=case.celery_sla_task_id,
        )

    @patch("oms.signals.send_proactive_alert.delay")
    @patch("oms.views_api.get_pea_assessment")
    @patch("oms.views_api.check_sla_timeout.apply_async")
    @patch("oms.views_api.check_eta_timeout.apply_async")
    def test_repeated_restored_cases_send_new_closed_loop_prompt_for_same_session(
        self, mock_eta_apply_async, mock_sla_apply_async, mock_assessment, mock_send_alert
    ):
        mock_eta_apply_async.return_value.id = "repeated-restored-eta-task-id"
        mock_sla_apply_async.return_value.id = "repeated-restored-sla-task-id"
        mock_assessment.return_value = self._assessment_payload(eta_minutes=12)
        session_id = "session-repeated-restored"
        ca_number = "123456789012"
        CustomerLocation.objects.create(
            ca_number=ca_number,
            fullname="Repeated Restored User",
            latitude=9.2917,
            longitude=100.926296,
        )
        current_case = OutageCase.objects.create(
            title="Initial repeated restored case",
            latitude=9.2917,
            longitude=100.926296,
        )
        CustomerReport.objects.create(
            session_id=session_id,
            ca_number=ca_number,
            latitude=9.2917,
            longitude=100.926296,
            related_case=current_case,
        )

        cycles = 4
        seen_case_ids = {current_case.case_id}
        seen_report_ids = set()
        message = None

        for cycle_index in range(cycles):
            current_case.status = "restored"
            current_case.save(update_fields=["status"])

            call = mock_send_alert.call_args_list[-1]
            self.assertEqual(call.kwargs["event_type"], "closed_loop_prompt")
            if message is None:
                message = call.kwargs["message"]
            self.assertEqual(call.kwargs["message"], message)
            self.assertNotIn(call.kwargs["report_id"], seen_report_ids)
            seen_report_ids.add(call.kwargs["report_id"])

            report = CustomerReport.objects.get(id=call.kwargs["report_id"])
            self.assertTrue(report.is_resolved)
            self.assertEqual(report.session_id, session_id)
            self.assertEqual(report.related_case_id, current_case.case_id)

            if cycle_index == cycles - 1:
                continue

            response = self._post_sync(session_id, ca_number)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data["event_type"], "new_event")
            current_case = OutageCase.objects.get(case_id=response.data["case_id"])
            self.assertNotIn(current_case.case_id, seen_case_ids)
            seen_case_ids.add(current_case.case_id)

        self.assertEqual(mock_send_alert.call_count, cycles)
        self.assertEqual(len(seen_case_ids), cycles)
        self.assertEqual(len(seen_report_ids), cycles)

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
    @patch("oms.views_api.check_sla_timeout.apply_async")
    @patch("oms.views_api.check_eta_timeout.apply_async")
    def test_sync_report_reuses_same_session_active_case_on_repeat(
        self, mock_eta_apply_async, mock_sla_apply_async, mock_assessment
    ):
        mock_eta_apply_async.return_value.id = "sync-repeat-task-id"
        mock_sla_apply_async.return_value.id = "sync-repeat-sla-task-id"
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
        mock_eta_apply_async.assert_called_once()
        mock_sla_apply_async.assert_called_once()

    @patch("oms.views_api.get_pea_assessment")
    @patch("oms.views_api.check_sla_timeout.apply_async")
    @patch("oms.views_api.check_eta_timeout.apply_async")
    def test_force_new_case_resolves_current_report_and_opens_new_case(
        self, mock_eta_apply_async, mock_sla_apply_async, mock_assessment
    ):
        mock_eta_apply_async.return_value.id = "force-new-eta-task-id"
        mock_sla_apply_async.return_value.id = "force-new-sla-task-id"
        mock_assessment.return_value = self._assessment_payload(eta_minutes=9)
        CustomerLocation.objects.create(
            ca_number="123456789012",
            fullname="Force New User",
            latitude=9.2917,
            longitude=100.926296,
        )
        old_case = OutageCase.objects.create(
            title="Still active after SLA",
            latitude=9.2917,
            longitude=100.926296,
            affected_ca_numbers=["123456789012"],
        )
        old_report = CustomerReport.objects.create(
            session_id="session-force-new",
            ca_number="123456789012",
            related_case=old_case,
        )

        response = self.client.post(
            "/api/reports/sync/",
            {
                "ca_number": "123456789012",
                "session_id": "session-force-new",
                "pdpa_consent": True,
                "force_new_case": True,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["event_type"], "new_event")
        new_case = OutageCase.objects.get(case_id=response.data["case_id"])
        self.assertNotEqual(new_case.case_id, old_case.case_id)
        self.assertEqual(new_case.case_type, CASE_TYPE_NORMAL)
        self.assertIsNotNone(new_case.celery_sla_task_id)
        old_report.refresh_from_db()
        self.assertTrue(old_report.is_resolved)
        new_report = CustomerReport.objects.get(id=response.data["report_id"])
        self.assertFalse(new_report.is_resolved)
        self.assertEqual(new_report.related_case, new_case)

    def test_closed_loop_response_marks_report_resolved(self):
        report = CustomerReport.objects.create(
            session_id="session-closed-loop-response",
            ca_number="123456789012",
        )

        response = self.client.post(
            "/api/reports/closed-loop-response/",
            {
                "session_id": "session-closed-loop-response",
                "ca_number": "123456789012",
                "report_id": report.id,
                "response": "resolved",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["resolved_count"], 1)
        report.refresh_from_db()
        self.assertTrue(report.is_resolved)

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
    def test_eta_timeout_redelivery_with_same_task_id_sends_once(self, mock_post):
        case = OutageCase.objects.create(
            title="Idempotent ETA timer",
            eta_target_time=timezone.now() - timedelta(minutes=1),
            celery_eta_task_id="same-eta-task-id",
        )
        report = CustomerReport.objects.create(
            session_id="session-idempotent-eta",
            ca_number="123456789012",
            related_case=case,
        )

        check_eta_timeout.apply(
            args=[case.case_id, report.id], task_id="same-eta-task-id"
        ).get()
        check_eta_timeout.apply(
            args=[case.case_id, report.id], task_id="same-eta-task-id"
        ).get()

        case.refresh_from_db()
        self.assertIsNone(case.celery_eta_task_id)
        self.assertEqual(
            ChatMessage.objects.filter(
                session_id=report.session_id,
                event_type="eta_timeout",
            ).count(),
            1,
        )

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

        notification = ChatMessage.objects.get(session_id=report.session_id)
        self.assertEqual(notification.event_type, "eta_timeout")
        self.assertIn("ขออัปเดตสถานะ", notification.content)
        self.assertIn("ทีมงานกำลังดำเนินการอยู่", notification.content)
        self.assertIn("คาดว่าจะจ่ายไฟคืน", notification.content)
        self.assertNotIn("ครบเวลาประเมินการเข้าหน้างาน", notification.content)
        self.assertIn(timezone.localtime(etr).strftime("%H:%M น."), notification.content)
        for forbidden in ["ภายในประมาณ", "ETA", "ETR", "SLA", "ช้ากว่ากำหนด", "ช่างช้า", "พี่ปลื้ม", "OMS"]:
            self.assertNotIn(forbidden, notification.content)

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

        notification = ChatMessage.objects.get(session_id=report.session_id)
        self.assertEqual(notification.event_type, "eta_timeout")
        self.assertIn("ขออัปเดตสถานะ", notification.content)
        self.assertIn("ทีมงานกำลังดำเนินการอยู่", notification.content)
        self.assertIn("คาดว่าจะจ่ายไฟคืน", notification.content)
        self.assertNotIn("ครบเวลาประเมินการเข้าหน้างาน", notification.content)
        self.assertIn(
            timezone.localtime(pluem_etr).strftime("%H:%M น."), notification.content
        )
        for forbidden in ["ภายในประมาณ", "ETA", "ETR", "SLA", "ช้ากว่ากำหนด", "ช่างช้า", "พี่ปลื้ม", "OMS"]:
            self.assertNotIn(forbidden, notification.content)

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

        notification = ChatMessage.objects.get(session_id=report.session_id)
        self.assertEqual(notification.event_type, "eta_timeout")
        self.assertIn("ขออัปเดตสถานะ", notification.content)
        self.assertIn("ทีมงานกำลังดำเนินการอยู่", notification.content)
        self.assertIn("คาดว่าจะจ่ายไฟคืน", notification.content)
        self.assertNotIn("ครบเวลาประเมินการเข้าหน้างาน", notification.content)
        self.assertRegex(notification.content, r"\d{2}:\d{2} น\.")
        for forbidden in ["ภายในประมาณ", "ETA", "ETR", "SLA", "ช้ากว่ากำหนด", "ช่างช้า", "พี่ปลื้ม", "OMS"]:
            self.assertNotIn(forbidden, notification.content)
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

        notification = ChatMessage.objects.get(session_id=report.session_id)
        self.assertEqual(notification.event_type, "eta_timeout")
        self.assertIn("ขออัปเดตสถานะ", notification.content)
        self.assertIn("ทีมงานกำลังดำเนินการอยู่", notification.content)
        self.assertIn("กำลังประเมินเวลาไฟกลับล่าสุด", notification.content)
        self.assertNotIn("ครบเวลาประเมินการเข้าหน้างาน", notification.content)
        for forbidden in ["ETA", "ETR", "SLA", "ช้ากว่ากำหนด", "ช่างช้า", "พี่ปลื้ม", "OMS"]:
            self.assertNotIn(forbidden, notification.content)

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

        notification = ChatMessage.objects.get(session_id=report.session_id)
        self.assertEqual(notification.event_type, "eta_timeout")
        self.assertIn("ขออัปเดตสถานะ", notification.content)
        self.assertIn("ทีมงานกำลังดำเนินการอยู่", notification.content)
        self.assertNotIn("ครบเวลาประเมินการเข้าหน้างาน", notification.content)
        self.assertNotIn("ETA", notification.content)
        self.assertNotIn("ETR", notification.content)
        self.assertNotIn("SLA", notification.content)

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
            is_resolved=True,
        )
        CustomerReport.objects.create(
            session_id="session-resolved",
            ca_number="123456789015",
            related_case=case,
            is_resolved=True,
        )

        check_eta_timeout(case.case_id, first_report.id)

        self.assertEqual(
            set(ChatMessage.objects.values_list("session_id", flat=True)),
            {"session-a", "session-b"},
        )
        self.assertEqual(ChatMessage.objects.count(), 2)

    @patch("oms.signals.check_sla_timeout.apply_async")
    @patch("oms.tasks.requests.post")
    def test_etr_timeout_sends_sla_notification(self, mock_post, mock_sla_apply_async):
        mock_sla_apply_async.return_value.id = "etr-timeout-sla-task-id"
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

        notification = ChatMessage.objects.get(session_id="session-etr-timeout")
        self.assertEqual(notification.event_type, "etr_timeout_sla")
        self.assertIn("ขออัปเดต", notification.content)
        self.assertIn("การจ่ายไฟจะไม่เกินเวลา", notification.content)
        self.assertEqual(case.sla_reference_time, case.created_at)
        self.assertEqual(case.sla_target_time, expected_sla_target)
        self.assertEqual(case.sla_reason, "case_created")
        self.assertIsNotNone(case.celery_sla_task_id)
        self.assertIn(
            timezone.localtime(expected_sla_target).strftime("%H:%M น."),
            notification.content,
        )
        for forbidden in ["เหลือเวลา", "เร่งดำเนินการให้ไม่เกิน", "เวลาไฟกลับที่ประเมินไว้เลยกำหนด", "ภายในประมาณ", "ETA", "ETR", "SLA"]:
            self.assertNotIn(forbidden, notification.content)


class CheckSlaTimeoutTests(TestCase):
    @patch("oms.signals.send_proactive_alert.delay")
    def test_sla_timeout_restores_case_and_sends_closed_loop_prompt(
        self, mock_send_alert
    ):
        now = timezone.now()
        case = OutageCase.objects.create(
            title="SLA timeout prompt",
            latitude=9.2917,
            longitude=100.926296,
            sla_target_time=now - timedelta(minutes=1),
            celery_sla_task_id="same-sla-task-id",
        )
        CustomerReport.objects.create(
            session_id="session-sla-a",
            ca_number="123456789012",
            related_case=case,
        )
        CustomerReport.objects.create(
            session_id="session-sla-a",
            ca_number="123456789013",
            related_case=case,
            is_resolved=True,
        )
        CustomerReport.objects.create(
            session_id="session-sla-resolved",
            ca_number="123456789014",
            related_case=case,
            is_resolved=True,
        )

        check_sla_timeout.apply(
            args=[case.case_id], task_id="same-sla-task-id"
        ).get()
        check_sla_timeout.apply(
            args=[case.case_id], task_id="same-sla-task-id"
        ).get()

        case.refresh_from_db()
        report = CustomerReport.objects.get(
            session_id="session-sla-a", ca_number="123456789012"
        )
        self.assertEqual(case.status, "restored")
        self.assertIsNone(case.celery_sla_task_id)
        self.assertTrue(report.is_resolved)
        self.assertTrue(OutageRestorationLog.objects.filter(case=case).exists())
        self.assertEqual(mock_send_alert.call_count, 1)
        payload = mock_send_alert.call_args.kwargs
        self.assertEqual(payload["event_type"], "closed_loop_prompt")
        self.assertEqual(
            payload["closed_loop_kind"], SLA_EXPIRED_CLOSED_LOOP_KIND
        )
        self.assertIn("ไฟฟ้ากลับมาใช้งานได้หรือยัง", payload["message"])
        self.assertIn("123456789012", payload["message"])
        self.assertNotIn("SLA", payload["message"])
        self.assertNotIn("ETA", payload["message"])
        self.assertNotIn("ETR", payload["message"])

    @patch("oms.signals.send_proactive_alert.delay")
    def test_sla_timeout_skips_inactive_cases(self, mock_send_alert):
        case = OutageCase.objects.create(
            title="SLA timeout restored",
            status="restored",
            latitude=9.2917,
            longitude=100.926296,
            sla_target_time=timezone.now() - timedelta(minutes=1),
        )
        CustomerReport.objects.create(
            session_id="session-sla-restored",
            ca_number="123456789012",
            related_case=case,
        )

        check_sla_timeout(case.case_id)

        mock_send_alert.assert_not_called()


class ProactiveAlertTaskTests(TestCase):
    @patch("oms.tasks.requests.post")
    def test_send_proactive_alert_includes_notification_identity(self, mock_post):
        case = OutageCase.objects.create(
            title="Notification identity case",
            latitude=9.2917,
            longitude=100.926296,
        )
        report = CustomerReport.objects.create(
            session_id="session-notification-identity",
            ca_number="123456789012",
            related_case=case,
        )
        message = "ระบบแจ้งว่าจ่ายไฟคืนแล้วค่ะ กรุณาเลือกสถานะไฟฟ้าด้านล่างค่ะ"

        send_proactive_alert(report.id, message, "closed_loop_prompt")

        notification = ChatMessage.objects.get(session_id=report.session_id)
        self.assertEqual(notification.ca_number, report.ca_number)
        self.assertEqual(notification.content, message)
        self.assertEqual(notification.event_type, "closed_loop_prompt")
        self.assertEqual(notification.report_id, report.id)
        self.assertEqual(notification.case_id, case.case_id)
        self.assertEqual(
            notification.notification_key,
            (
                f"closed_loop_prompt|123456789012|{report.id}|"
                f"{case.case_id}|{message}"
            ),
        )

        send_proactive_alert(report.id, message, "closed_loop_prompt")
        self.assertEqual(ChatMessage.objects.count(), 1)


class DurableAgentJobTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.report = CustomerReport.objects.create(
            session_id="durable-agent-session",
            ca_number="123456789012",
            pdpa_consent=True,
        )

    def _submit(self, question="ไฟดับครับ"):
        return self.client.post(
            "/agent/ask/",
            {
                "session_id": self.report.session_id,
                "ca_number": self.report.ca_number,
                "pdpa_consent": True,
                "question": question,
            },
            format="json",
        )

    @patch("oms.views.process_agent_job.apply_async")
    def test_submit_persists_task_identity_before_publish_and_returns_202(
        self, mock_apply_async
    ):
        response = self._submit()

        self.assertEqual(response.status_code, 202)
        job = AgentJob.objects.get(pk=response.json()["job_id"])
        self.assertEqual(job.status, AgentJob.STATUS_QUEUED)
        self.assertEqual(job.user_message.content, "ไฟดับครับ")
        self.assertEqual(response.json()["user_message_id"], job.user_message_id)
        mock_apply_async.assert_called_once_with(
            args=[str(job.id)],
            task_id=job.celery_task_id,
            queue="agent",
        )

    @patch("oms.views.process_agent_job.apply_async")
    def test_one_active_job_per_session_returns_existing_job(self, mock_apply_async):
        first = self._submit("ข้อความแรก")
        second = self._submit("ข้อความที่สอง")

        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.json()["detail"], "agent_job_in_progress")
        self.assertEqual(second.json()["job_id"], first.json()["job_id"])
        self.assertEqual(ChatMessage.objects.filter(role="user").count(), 1)

    @patch("oms.views.process_agent_job.apply_async")
    def test_submit_uses_pending_closed_loop_report_when_active_report_is_resolved(
        self, mock_apply_async
    ):
        self.report.is_resolved = True
        self.report.save(update_fields=["is_resolved", "updated_at"])
        ChatMessage.objects.create(
            session_id=self.report.session_id,
            report=self.report,
            role=ChatMessage.ROLE_SYSTEM,
            content="ไฟฟ้ากลับมาใช้งานได้หรือยังคะ",
            ca_number=self.report.ca_number,
            event_type="closed_loop_prompt",
        )

        response = self._submit("ไฟมาแล้ว")

        self.assertEqual(response.status_code, 202)
        job = AgentJob.objects.get(pk=response.json()["job_id"])
        self.assertEqual(job.user_message.report_id, self.report.id)
        self.assertEqual(job.user_message.content, "ไฟมาแล้ว")
        mock_apply_async.assert_called_once()

    @patch("oms.views.process_agent_job.apply_async")
    def test_submit_reopens_resolved_report_when_related_case_is_still_active(
        self, mock_apply_async
    ):
        case = OutageCase.objects.create(
            title="Still active follow-up case",
            affected_ca_numbers=[self.report.ca_number],
        )
        self.report.related_case = case
        self.report.is_resolved = True
        self.report.save(update_fields=["related_case", "is_resolved", "updated_at"])

        response = self._submit("จะมายังครับ")

        self.assertEqual(response.status_code, 202)
        self.report.refresh_from_db()
        self.assertFalse(self.report.is_resolved)
        job = AgentJob.objects.get(pk=response.json()["job_id"])
        self.assertEqual(job.user_message.report_id, self.report.id)
        self.assertEqual(job.user_message.content, "จะมายังครับ")
        mock_apply_async.assert_called_once()

    @patch("oms.views.process_agent_job.apply_async")
    def test_submit_without_active_report_or_pending_closed_loop_returns_409(
        self, mock_apply_async
    ):
        self.report.is_resolved = True
        self.report.save(update_fields=["is_resolved", "updated_at"])

        response = self._submit("ไฟดับครับ")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"], "active_session_report_not_found")
        self.assertFalse(AgentJob.objects.exists())
        self.assertFalse(ChatMessage.objects.filter(role=ChatMessage.ROLE_USER).exists())
        mock_apply_async.assert_not_called()

    @patch("oms.views.process_agent_job.apply_async", side_effect=RuntimeError("redis down"))
    def test_enqueue_failure_marks_job_failed(self, mock_apply_async):
        response = self._submit()

        self.assertEqual(response.status_code, 503)
        job = AgentJob.objects.get(pk=response.json()["job_id"])
        self.assertEqual(job.status, AgentJob.STATUS_FAILED)
        self.assertEqual(job.error_code, "enqueue_failed")

    @patch("oms.tasks.requests.post")
    def test_worker_success_is_transactional_and_duplicate_delivery_is_noop(
        self, mock_post
    ):
        user_message = ChatMessage.objects.create(
            session_id=self.report.session_id,
            report=self.report,
            role=ChatMessage.ROLE_USER,
            content="ไฟดับครับ",
            ca_number=self.report.ca_number,
        )
        job = AgentJob.objects.create(
            session_id=self.report.session_id,
            ca_number=self.report.ca_number,
            pdpa_consent=True,
            user_message=user_message,
            celery_task_id="agent-task-id",
        )
        agent_response = Mock()
        agent_response.raise_for_status.return_value = None
        agent_response.json.return_value = {
            "answer": "รับทราบค่ะ",
            "state": {"flow_step": "checking_outage"},
        }
        mock_post.return_value = agent_response

        process_agent_job.apply(args=[str(job.id)], task_id="agent-task-id").get()
        process_agent_job.apply(args=[str(job.id)], task_id="agent-task-id").get()

        job.refresh_from_db()
        self.assertEqual(job.status, AgentJob.STATUS_SUCCEEDED)
        self.assertEqual(job.response_message.content, "รับทราบค่ะ")
        self.assertEqual(job.response_state["flow_step"], "checking_outage")
        self.assertEqual(mock_post.call_count, 1)
        self.assertEqual(
            ChatMessage.objects.filter(session_id=self.report.session_id).count(),
            2,
        )
        self.assertEqual(
            mock_post.call_args.kwargs["json"]["user_message_id"],
            user_message.id,
        )
        self.assertIn("X-Internal-Token", mock_post.call_args.kwargs["headers"])

        status_response = self.client.get(
            f"/agent/jobs/{job.id}/",
            {"session_id": self.report.session_id},
        )
        self.assertEqual(status_response.status_code, 200)
        self.assertEqual(status_response.json()["answer"], "รับทราบค่ะ")
        self.assertEqual(
            status_response.json()["response_message_id"], job.response_message_id
        )


class DurableNotificationEndpointTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.report = CustomerReport.objects.create(
            session_id="durable-notification-session",
            ca_number="123456789012",
        )

    def test_notification_ack_and_latest_closed_loop_contract(self):
        message = "ไฟฟ้ากลับมาใช้งานได้หรือยังคะ"
        send_proactive_alert(
            self.report.id,
            message,
            "closed_loop_prompt",
            closed_loop_kind="sla_expired",
        )
        notification = ChatMessage.objects.get()

        response = self.client.get(
            f"/agent/notifications/{self.report.session_id}/"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["notifications"][0]["message"], message)

        ack = self.client.post(
            f"/agent/notifications/{self.report.session_id}/ack/",
            {"notification_key": notification.notification_key},
            format="json",
        )
        self.assertEqual(ack.status_code, 200)
        self.assertEqual(ack.json()["acked"], 1)
        self.assertEqual(
            self.client.get(
                f"/agent/notifications/{self.report.session_id}/"
            ).json()["notifications"],
            [],
        )

        latest = self.client.get(
            f"/agent/notifications/{self.report.session_id}/latest-closed-loop/"
        )
        self.assertEqual(len(latest.json()["notifications"]), 1)
        self.assertEqual(
            latest.json()["notifications"][0]["closed_loop_kind"], "sla_expired"
        )

        ChatMessage.objects.create(
            session_id=self.report.session_id,
            report=self.report,
            role=ChatMessage.ROLE_USER,
            content="ไฟมาแล้ว",
            ca_number=self.report.ca_number,
        )
        latest = self.client.get(
            f"/agent/notifications/{self.report.session_id}/latest-closed-loop/"
        )
        self.assertEqual(latest.json()["notifications"], [])

    def test_session_context_cutoff_excludes_current_user_message(self):
        first = ChatMessage.objects.create(
            session_id=self.report.session_id,
            report=self.report,
            role=ChatMessage.ROLE_AGENT,
            content="ก่อนหน้า",
            ca_number=self.report.ca_number,
        )
        current = ChatMessage.objects.create(
            session_id=self.report.session_id,
            report=self.report,
            role=ChatMessage.ROLE_USER,
            content="ข้อความปัจจุบัน",
            ca_number=self.report.ca_number,
        )

        response = self.client.get(
            f"/api/reports/session-context/{self.report.session_id}/",
            {"before_message_id": current.id},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [item["message_id"] for item in response.data["chat_history"]],
            [first.id],
        )

    def test_session_context_and_latest_closed_loop_are_scoped_to_ca_number(self):
        old_report = CustomerReport.objects.create(
            session_id="ca-scoped-session",
            ca_number="020025790865",
            is_resolved=True,
        )
        current_report = CustomerReport.objects.create(
            session_id="ca-scoped-session",
            ca_number="020001291771",
        )
        old_prompt = ChatMessage.objects.create(
            session_id="ca-scoped-session",
            report=old_report,
            role=ChatMessage.ROLE_SYSTEM,
            content="เรียนผู้ใช้ไฟฟ้าหมายเลข CA 020025790865 ขณะนี้ไฟฟ้ากลับมาใช้งานได้หรือยังคะ",
            ca_number=old_report.ca_number,
            event_type="closed_loop_prompt",
            notification_key="old-ca-prompt",
        )
        current_message = ChatMessage.objects.create(
            session_id="ca-scoped-session",
            report=current_report,
            role=ChatMessage.ROLE_AGENT,
            content="ข้อความของ CA ปัจจุบัน",
            ca_number=current_report.ca_number,
        )

        context = self.client.get(
            "/api/reports/session-context/ca-scoped-session/",
            {"ca_number": current_report.ca_number},
        )
        self.assertEqual(context.status_code, 200)
        self.assertEqual(
            [item["message_id"] for item in context.data["chat_history"]],
            [current_message.id],
        )

        latest = self.client.get(
            "/agent/notifications/ca-scoped-session/latest-closed-loop/",
            {"ca_number": current_report.ca_number},
        )
        self.assertEqual(latest.json()["notifications"], [])

        old_latest = self.client.get(
            "/agent/notifications/ca-scoped-session/latest-closed-loop/",
            {"ca_number": old_report.ca_number},
        )
        self.assertEqual(
            old_latest.json()["notifications"][0]["message_id"],
            old_prompt.id,
        )


class OutageCaseSignalTests(TestCase):
    @patch("oms.signals.celery_app.control.revoke")
    @patch("oms.signals.check_eta_timeout.apply_async")
    @patch("oms.tasks.requests.post")
    def test_clearing_eta_invalidates_stored_and_redelivered_task(
        self, mock_post, mock_apply_async, mock_revoke
    ):
        case = OutageCase.objects.create(
            title="Clear ETA task",
            eta_target_time=timezone.now() + timedelta(hours=1),
            celery_eta_task_id="old-eta-task-id",
        )
        report = CustomerReport.objects.create(
            session_id="clear-eta-session",
            ca_number="123456789012",
            related_case=case,
        )

        case.eta_target_time = None
        case.save(update_fields=["eta_target_time"])
        case.refresh_from_db()
        self.assertIsNone(case.celery_eta_task_id)
        mock_revoke.assert_called_once_with("old-eta-task-id", terminate=True)
        mock_apply_async.assert_not_called()

        check_eta_timeout.apply(
            args=[case.case_id, report.id], task_id="old-eta-task-id"
        ).get()
        mock_post.assert_not_called()

    @patch("oms.signals.check_etr_timeout.apply_async")
    @patch("oms.signals.send_proactive_alert.delay")
    def test_oms_etr_only_moves_later(
        self, mock_send_alert, mock_apply_async
    ):
        initial_etr = timezone.now() + timedelta(hours=2)
        case = OutageCase.objects.create(title="Monotonic ETR", oms_etr=initial_etr)
        CustomerReport.objects.create(
            session_id="session-monotonic-etr",
            ca_number="123456789012",
            related_case=case,
        )
        stale_case = OutageCase.objects.get(pk=case.pk)

        case.oms_etr = initial_etr - timedelta(hours=1)
        case.save(update_fields=["oms_etr"])
        case.refresh_from_db()
        self.assertEqual(case.oms_etr, initial_etr)
        mock_apply_async.assert_not_called()
        mock_send_alert.assert_not_called()

        case.oms_etr = initial_etr
        case.save(update_fields=["oms_etr"])
        mock_apply_async.assert_not_called()

        mock_apply_async.return_value.id = "later-etr-task-id"
        later_etr = initial_etr + timedelta(hours=1)
        case.oms_etr = later_etr
        with self.captureOnCommitCallbacks(execute=True):
            case.save(update_fields=["oms_etr"])
        case.refresh_from_db()
        self.assertEqual(case.oms_etr, later_etr)
        mock_apply_async.assert_called_once_with(
            args=[case.case_id],
            eta=later_etr,
            task_id=case.celery_etr_task_id,
        )
        mock_send_alert.assert_called_once()

        stale_case.oms_etr = initial_etr + timedelta(minutes=30)
        stale_case.save(update_fields=["oms_etr"])
        stale_case.refresh_from_db()
        self.assertEqual(stale_case.oms_etr, later_etr)
        self.assertEqual(mock_apply_async.call_count, 1)
        self.assertEqual(mock_send_alert.call_count, 1)

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
            is_resolved=True,
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
        with self.captureOnCommitCallbacks(execute=True):
            case.save(update_fields=["oms_etr"])

        case.refresh_from_db()
        self.assertIsNotNone(case.oms_etr_updated_at)
        self.assertIsNotNone(case.sla_reference_time)
        self.assertIsNotNone(case.sla_target_time)
        self.assertEqual(case.sla_reason, original_sla_reason)
        self.assertIsNotNone(case.celery_etr_task_id)
        self.assertEqual(case.sla_reference_time, original_sla_reference_time)
        self.assertEqual(case.sla_target_time, original_sla_target_time)
        self.assertNotEqual(
            case.sla_target_time, case.oms_etr_updated_at + timedelta(hours=4)
        )
        mock_apply_async.assert_called_once_with(
            args=[case.case_id],
            eta=case.oms_etr,
            task_id=case.celery_etr_task_id,
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
        self.assertIsNotNone(case.celery_etr_task_id)

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
        self.assertIn("ไฟฟ้ากลับมาใช้งานได้หรือยัง", call.kwargs["message"])
        self.assertIn("123456789012", call.kwargs["message"])
        self.assertNotIn("{ca_number}", call.kwargs["message"])
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
            is_resolved=True,
        )

        case.status = "restored"
        case.save(update_fields=["status"])

        self.assertEqual(mock_send_alert.call_count, 1)
        self.assertEqual(
            mock_send_alert.call_args.kwargs["event_type"],
            "closed_loop_prompt",
        )

    @patch("oms.signals.send_proactive_alert.delay")
    def test_restored_status_uses_latest_report_ca_for_duplicate_session(
        self, mock_send_alert
    ):
        case = OutageCase.objects.create(
            title="Restored duplicate session current CA",
            latitude=9.2917,
            longitude=100.926296,
        )
        old_report = CustomerReport.objects.create(
            session_id="session-ca-changed",
            ca_number="020025790865",
            related_case=case,
            is_resolved=True,
        )
        latest_report = CustomerReport.objects.create(
            session_id="session-ca-changed",
            ca_number="020001291771",
            related_case=case,
        )

        case.status = "restored"
        case.save(update_fields=["status"])

        self.assertEqual(mock_send_alert.call_count, 1)
        call = mock_send_alert.call_args
        self.assertEqual(call.kwargs["report_id"], latest_report.id)
        self.assertIn(latest_report.ca_number, call.kwargs["message"])
        self.assertNotIn(old_report.ca_number, call.kwargs["message"])
        old_report.refresh_from_db()
        latest_report.refresh_from_db()
        self.assertTrue(old_report.is_resolved)
        self.assertTrue(latest_report.is_resolved)

    @patch("oms.signals.send_proactive_alert.delay")
    def test_restored_mass_outage_includes_reports_on_merged_children(
        self, mock_send_alert
    ):
        anchor = OutageCase.objects.create(
            title="Mass outage anchor",
            case_type=CASE_TYPE_MASS_OUTAGE,
            latitude=9.2917,
            longitude=100.926296,
        )
        merged_child = OutageCase.objects.create(
            title="Merged child with stale report",
            status=STATUS_MERGED,
            merged_into=anchor,
            merged_at=timezone.now(),
            latitude=9.2918,
            longitude=100.926396,
        )
        anchor_report = CustomerReport.objects.create(
            session_id="session-anchor",
            ca_number="123456789012",
            related_case=anchor,
        )
        child_report = CustomerReport.objects.create(
            session_id="session-child",
            ca_number="123456789013",
            related_case=merged_child,
        )

        anchor.status = "restored"
        anchor.save(update_fields=["status"])

        self.assertEqual(mock_send_alert.call_count, 2)
        self.assertEqual(
            {
                CustomerReport.objects.get(id=call.kwargs["report_id"]).session_id
                for call in mock_send_alert.call_args_list
            },
            {"session-anchor", "session-child"},
        )
        anchor_report.refresh_from_db()
        child_report.refresh_from_db()
        self.assertTrue(anchor_report.is_resolved)
        self.assertTrue(child_report.is_resolved)


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
        self.assertIn(
            "notificationKey(eventType, caNumber, message, reportId, caseId, explicitKey)",
            content,
        )
        self.assertIn("notification.report_id", content)
        self.assertIn("notification.case_id", content)
        self.assertIn("notification.notification_key", content)
        self.assertIn('notificationUrl("/ack")', content)
        self.assertIn('notificationUrl("/latest-closed-loop")', content)
        self.assertIn("acknowledgeNotification(key)", content)
        self.assertIn("recoverLatestClosedLoopPrompt", content)
        self.assertIn("agentErrorMessage(data.detail)", content)
        self.assertIn("ไม่พบรายการแจ้งเหตุที่กำลังดำเนินการอยู่ค่ะ", content)
        self.assertNotIn("appendBotMessage(`Error:", content)
        self.assertIn("if (document.hidden) return;", content)
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

    def test_ops_cases_payload_marks_merged_target_and_action_skips_child(self):
        anchor_case = OutageCase.objects.create(
            title="Ops merged anchor",
            case_type=CASE_TYPE_MASS_OUTAGE,
            latitude=13.7563,
            longitude=100.5018,
        )
        merged_case = OutageCase.objects.create(
            title="Ops merged child",
            status=STATUS_MERGED,
            merged_into=anchor_case,
            merged_at=timezone.now(),
            latitude=13.7564,
            longitude=100.5019,
        )

        response = self.client.get(
            "/ops/cases/",
            {"include_restored": "true", "status": "merged"},
        )

        self.assertEqual(response.status_code, 200)
        cases = response.json()["cases"]
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0]["case_id"], str(merged_case.case_id))
        self.assertEqual(cases[0]["merged_into_case_id"], str(anchor_case.case_id))
        self.assertEqual(cases[0]["merged_into_lv_group_id"], anchor_case.lv_group_id)
        self.assertIsNotNone(cases[0]["merged_at"])

        action_response = self.client.post(
            "/ops/cases/action/",
            {
                "action": "restore",
                "target_mode": "selected",
                "case_ids": [str(merged_case.case_id)],
            },
            format="json",
        )

        self.assertEqual(action_response.status_code, 200)
        self.assertEqual(action_response.json()["updated_count"], 0)
        self.assertEqual(action_response.json()["skipped_count"], 1)
        merged_case.refresh_from_db()
        self.assertEqual(merged_case.status, STATUS_MERGED)

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
        affected_ca_index = rows[0].index("Affected CA")
        self.assertIn("123456789012", rows[1][affected_ca_index])

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
        self.assertIsNotNone(selected_case.celery_etr_task_id)
        self.assertIsNone(untouched_case.oms_etr)
        mock_send_alert.assert_called_once()

    @patch("oms.signals.check_etr_timeout.apply_async")
    @patch("oms.signals.send_proactive_alert.delay")
    def test_ops_action_ignores_earlier_or_equal_etr(
        self, mock_send_alert, mock_apply_async
    ):
        current_etr = timezone.now() + timedelta(hours=2)
        case = OutageCase.objects.create(title="Ops monotonic ETR", oms_etr=current_etr)

        for requested_etr in [current_etr - timedelta(hours=1), current_etr]:
            response = self.client.post(
                "/ops/cases/action/",
                {
                    "action": "set_etr",
                    "target_mode": "selected",
                    "case_ids": [str(case.case_id)],
                    "etr_mode": "datetime",
                    "etr_at": requested_etr.isoformat(),
                },
                format="json",
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["updated_count"], 0)
            self.assertEqual(response.json()["skipped_count"], 1)

        case.refresh_from_db()
        self.assertEqual(case.oms_etr, current_etr)
        mock_apply_async.assert_not_called()
        mock_send_alert.assert_not_called()

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

        def assert_task_id_was_persisted(*, args, eta, task_id):
            persisted_id = OutageCase.objects.values_list(
                "celery_eta_task_id", flat=True
            ).get(pk=selected_case.pk)
            self.assertEqual(persisted_id, task_id)
            return mock_apply_async.return_value

        mock_apply_async.side_effect = assert_task_id_was_persisted

        before = timezone.now()
        with self.captureOnCommitCallbacks(execute=True):
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
        self.assertIsNotNone(selected_case.celery_eta_task_id)
        mock_apply_async.assert_called_once_with(
            args=[selected_case.case_id, report.id],
            eta=selected_case.eta_target_time,
            task_id=selected_case.celery_eta_task_id,
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

    def test_outage_admin_merged_row_only_shows_merge_target(self):
        model_admin = OutageCaseAdmin(OutageCase, AdminSite())
        anchor_case = OutageCase.objects.create(
            title="Admin merged anchor",
            case_type=CASE_TYPE_MASS_OUTAGE,
            latitude=9.2917,
            longitude=100.926296,
        )
        merged_case = OutageCase.objects.create(
            title="Admin merged child",
            status=STATUS_MERGED,
            merged_into=anchor_case,
            merged_at=timezone.now(),
            affected_ca_numbers=["123456789012"],
            eta_target_time=timezone.now() + timedelta(minutes=20),
            oms_etr=timezone.now() + timedelta(hours=1),
            sla_target_time=timezone.now() + timedelta(hours=2),
            assessment_fastest_branch="การไฟฟ้าส่วนภูมิภาค สาขา รังสิต",
            assessment_eta_formatted="~ 20 min",
            pluem_etr_minutes=60,
            latitude=9.2918,
            longitude=100.926396,
        )

        merged_target = str(model_admin.merged_into_display(merged_case))
        self.assertIn(f"LV {anchor_case.lv_group_id}", merged_target)
        self.assertIn(str(anchor_case.case_id)[:8], merged_target)
        self.assertEqual(model_admin.status_display(merged_case), "ถูกรวมเข้าเคสอื่น")

        blank_columns = [
            model_admin.lv_group(merged_case),
            model_admin.case_id_display(merged_case),
            model_admin.case_type_display(merged_case),
            model_admin.affected_CA(merged_case),
            model_admin.countdown_eta(merged_case),
            model_admin.countdown_etr(merged_case),
            model_admin.countdown_sla(merged_case),
            model_admin.fastest_branch_display(merged_case),
            model_admin.eta_formatted_display(merged_case),
            model_admin.pluem_etr_minutes_display(merged_case),
            model_admin.created_at_display(merged_case),
        ]
        for rendered in blank_columns:
            self.assertIn("&mdash;", str(rendered))

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
