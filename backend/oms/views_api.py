import math
from rest_framework.decorators import api_view
from rest_framework.response import Response
from django.utils import timezone
from datetime import timedelta
from .models import CustomerReport, OutageCase
from .serializers import (
    AgentReportSerializer,
    ActionStatusRequestSerializer,
    ActionStatusResponseSerializer,
)


# ---------------------------------------------------------
# ฟังก์ชันคณิตศาสตร์สำหรับคำนวณระยะทางระหว่างพิกัด 2 จุด (กิโลเมตร)
# ---------------------------------------------------------
def calculate_distance(lat1, lon1, lat2, lon2):
    if None in [lat1, lon1, lat2, lon2]:
        return float("inf")

    R = 6371.0  # รัศมีโลก (กิโลเมตร)
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

    return R * c  # ระยะทางเป็นกิโลเมตร


# ---------------------------------------------------------


@api_view(["POST"])
def sync_agent_report(request):
    serializer = AgentReportSerializer(data=request.data)

    if serializer.is_valid():
        data = serializer.validated_data
        session_id = data.get("session_id")
        ca_number = data.get("ca_number")

        # ค้นหาหรือสร้าง Report ใหม่สำหรับ Session นี้
        report, created = CustomerReport.objects.get_or_create(
            session_id=session_id, ca_number=ca_number, is_resolved=False
        )

        # อัปเดตข้อมูลทั่วไป
        if data.get("latitude"):
            report.latitude = data.get("latitude")
        if data.get("longitude"):
            report.longitude = data.get("longitude")
        if data.get("chat_history"):
            report.chat_history = data.get("chat_history")
        if data.get("time_stamp"):
            report.time_stamp = data.get("time_stamp")

        # ---------------------------------------------------------
        # แกนหลักของการแยกสายปฏิบัติการ (Decision Branch)
        # ---------------------------------------------------------
        event_type = "new_event"  # สาย B (ตั้งเป็นค่าเริ่มต้น)

        if report.latitude and report.longitude and not report.related_case:
            # 1. ค้นหาเคสไฟดับทั้งหมดในระบบที่ยังซ่อมไม่เสร็จ
            active_cases = OutageCase.objects.exclude(status="restored")
            nearest_case = None

            for case in active_cases:
                # คำนวณระยะทางจากบ้านลูกค้า ไปหาพิกัดศูนย์กลางของแต่ละเคส
                distance = calculate_distance(
                    report.latitude, report.longitude, case.latitude, case.longitude
                )

                # หากพบว่าอยู่ในรัศมี 5 กิโลเมตร ให้ถือว่าเป็น "ไฟดับบริเวณกว้าง/เหตุซ้ำซ้อน" (สาย A)
                if distance <= 5.0:
                    nearest_case = case
                    event_type = "repeated_event"
                    break  # เจอเคสแรกที่ใกล้ก็หยุดหาเลย

            if nearest_case:
                # สาย A: ผูกเข้ากับเคสเดิม (Mass Outage)
                report.related_case = nearest_case
            else:
                # สาย B: ไม่พบเคสใกล้เคียงเลย สร้างเคสใหม่ (Normal Outage)
                base_time = report.time_stamp if report.time_stamp else timezone.now()
                mock_eta = base_time + timedelta(minutes=45)  # ตั้งเวลาช่างถึงหน้างาน 45 นาที

                new_case = OutageCase.objects.create(
                    title=f"แจ้งไฟดับจาก CA {ca_number}",
                    latitude=report.latitude,
                    longitude=report.longitude,
                    eta_target_time=mock_eta,
                )
                report.related_case = new_case

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
    """
    API ตรวจสอบสถานะเหตุการณ์ไฟดับตามหมายเลข CA (GET)
    """
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
                    "status": "repeated_event",
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
