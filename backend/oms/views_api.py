import math
from rest_framework import serializers
from rest_framework.decorators import api_view
from rest_framework.response import Response
from django.utils import timezone
from datetime import timedelta
from .models import CustomerReport, OutageCase
from .serializers import (
    AgentReportSerializer,
    ActionStatusRequestSerializer,
    ActionStatusResponseSerializer,
    validate_ca_number_format,
)
from .tasks import check_eta_timeout


def calculate_distance(lat1, lon1, lat2, lon2):
    if None in [lat1, lon1, lat2, lon2]:
        return float("inf")
    R = 6371.0
    lat1_rad = math.radians(lat1)
    lon1_rad = math.radians(lon1)
    lat2_rad = math.radians(lat2)
    lon2_rad = math.radians(lon2)
    dlon = lon2_rad - lon1_rad
    dlat = lat2_rad - lat1_rad
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(dlon / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


@api_view(["POST"])
def sync_agent_report(request):
    serializer = AgentReportSerializer(data=request.data)
    if serializer.is_valid():
        data = serializer.validated_data
        session_id = data.get("session_id")
        ca_number = data.get("ca_number")

        report, created = CustomerReport.objects.get_or_create(
            session_id=session_id, ca_number=ca_number, is_resolved=False
        )

        if data.get("latitude"):
            report.latitude = data.get("latitude")
        if data.get("longitude"):
            report.longitude = data.get("longitude")
        if data.get("time_stamp"):
            report.time_stamp = data.get("time_stamp")
        if data.get("pdpa_consent"):
            report.pdpa_consent = True
            if not report.pdpa_consent_at:
                report.pdpa_consent_at = timezone.now()

        event_type = "new_event"

        if report.latitude and report.longitude and not report.related_case:
            active_cases = OutageCase.objects.exclude(status="restored")
            nearest_case = None

            for case in active_cases:
                            dist = calculate_distance(report.latitude, report.longitude, case.latitude, case.longitude)
                            print(f"DEBUG: Checking case {case.case_id}, Distance: {dist}")
                            if dist <= 5.0:
                                nearest_case = case
                                event_type = "repeated_event"
                                break

            if nearest_case:
                report.related_case = nearest_case
            else:
                base_time = report.time_stamp if report.time_stamp else timezone.now()
                mock_eta = base_time + timedelta(minutes=45)

                new_case = OutageCase.objects.create(
                    title=f"แจ้งไฟดับจาก CA {ca_number}",
                    latitude=report.latitude,
                    longitude=report.longitude,
                    eta_target_time=mock_eta,
                )
                report.related_case = new_case

                # สั่งรัน Task และเก็บ task_id ลง Database
                task = check_eta_timeout.apply_async(
                    args=[new_case.case_id, report.id], eta=mock_eta
                )
                new_case.celery_eta_task_id = task.id
                new_case.save(update_fields=["celery_eta_task_id"])

        report.save()
        response_data = {
            "status": "success",
            "event_type": event_type,
            "report_id": report.id,
            "case_id": report.related_case.case_id if report.related_case else None,
            "eta_target_time": report.related_case.eta_target_time.isoformat()
            if report.related_case and report.related_case.eta_target_time
            else None,
            "oms_etr": report.related_case.oms_etr.isoformat()
            if report.related_case and report.related_case.oms_etr
            else None,
        }
        return Response(response_data)
    return Response(serializer.errors, status=400)


@api_view(["GET"])
def get_action_status(request):
    request_serializer = ActionStatusRequestSerializer(data=request.query_params)
    if not request_serializer.is_valid():
        return Response(request_serializer.errors, status=400)

    ca_number = request_serializer.validated_data.get("ca_number")
    try:
        report = CustomerReport.objects.filter(
            ca_number=ca_number, is_resolved=False
        ).last()
        response_data = {
            "status": "first_time",
            "case_id": None,
            "case_status": None,
            "case_status_display": None,
            "oms_etr": None,
        }
        if report and report.related_case:
            case = report.related_case
            response_data.update(
                {
                    "status": "active_case_exists",
                    "case_id": case.case_id,
                    "case_status": case.status,
                    "case_status_display": case.get_status_display(),
                    "oms_etr": case.oms_etr if case.oms_etr else None,
                }
            )
        response_serializer = ActionStatusResponseSerializer(response_data)
        return Response(response_serializer.data)
    except Exception as e:
        return Response({"error": str(e)}, status=500)


# --- API ใหม่สำหรับ Anti-Loop (Fast Track) ---
@api_view(["POST"])
def fast_track_report(request):
    """
    รับคำสั่งจากการยืนยันสวิตช์เบรกเกอร์ของลูกค้า
    """
    ca_number = request.data.get("ca_number")
    try:
        ca_number = validate_ca_number_format(ca_number)
    except serializers.ValidationError as e:
        return Response({"error": str(e)}, status=400)

    # ดึงประวัติลูกค้าล่าสุดที่เคสเพิ่งถูกปิดไป (is_resolved=True ล่าสุด) หรือเคสเดิม
    report = (
        CustomerReport.objects.filter(ca_number=ca_number)
        .order_by("-updated_at")
        .first()
    )

    if not report:
        return Response({"event_type": "fallback", "message": "ไม่พบข้อมูลประวัติ"})

    if report.fast_track_quota > 0:
        # หักโควต้าและสร้างเคส Fast Track
        report.fast_track_quota -= 1

        # รีเซ็ตสถานะเป็นรอการแก้ไข
        report.is_resolved = False

        new_case = OutageCase.objects.create(
            title=f"[ด่วน! ไฟดับซ้ำซ้อน] CA {ca_number}",
            status="reported",
            latitude=report.latitude or 13.0,
            longitude=report.longitude or 100.0,
        )
        report.related_case = new_case
        report.save()

        return Response(
            {
                "event_type": "fast_track_created",
                "message": "ส่งเรื่องตรวจสอบซ้ำ (Fast-track) ให้ช่างเรียบร้อยแล้ว",
            }
        )
    else:
        # โควต้าหมด (ป้องกันการวนลูป) -> เปลี่ยนเป็นโอนสาย
        return Response(
            {
                "event_type": "fallback_to_human",
                "message": "โควต้าการตรวจสอบซ้ำหมดแล้ว ระบบกำลังโอนสายให้เจ้าหน้าที่",
            }
        )
