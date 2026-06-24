from rest_framework.decorators import api_view
from rest_framework.response import Response
from .models import CustomerReport, OutageCase
from .serializers import AgentReportSerializer, ActionStatusRequestSerializer, ActionStatusResponseSerializer
from django.utils import timezone

@api_view(["POST"])
def sync_agent_report(request):
    serializer = AgentReportSerializer(data=request.data)

    if serializer.is_valid():
        data = serializer.validated_data
        session_id = data.get("session_id")
        ca_number = data.get("ca_number")
        tool_used = data.get("tool_used")

        # 1. Upsert: ค้นหาจาก Session ID และ CA ที่ยังคุยไม่จบ (is_resolved=False)
        # ถ้าไม่มีในระบบ (หรือเป็นเคสใหม่ที่เคสเก่าจบไปแล้ว) จะทำการสร้างบรรทัดใหม่
        report, created = CustomerReport.objects.get_or_create(
            session_id=session_id, ca_number=ca_number, is_resolved=False
        )

        # 2. อัปเดตข้อมูลพิกัดและประวัติการสนทนา
        if data.get("latitude"):
            report.latitude = data.get("latitude")
        if data.get("longitude"):
            report.longitude = data.get("longitude")
        if data.get("chat_history"):
            report.chat_history = data.get("chat_history")

        # 3. อัปเดต State ความต้องการ
        if tool_used == "ETA":
            report.needs_eta = True
        elif tool_used == "ETR":
            report.needs_etr = True

        # 4. Mockup: จำลองการผูกเคส (ถ้ายังไม่มีเคสหลัก ให้สร้างขึ้นมาใหม่)
        # ในอนาคตตรงนี้คือจุดที่เราจะเขียนโค้ดหาพิกัด 5km
        if not report.related_case:
            new_case = OutageCase.objects.create(
                title=f"แจ้งไฟดับจาก CA {ca_number}",
                latitude=report.latitude or 0.0,
                longitude=report.longitude or 0.0,
            )
            report.related_case = new_case

        report.save()

        return Response(
            {
                "status": "success",
                "message": "Report synced successfully",
                "is_new_report": created,
                "report_id": report.id,
                "case_id": report.related_case.case_id if report.related_case else None,
            }
        )

    return Response(serializer.errors, status=400)






@api_view(["GET"])
def get_action_status(request):
    """
    API ตรวจสอบสถานะเหตุการณ์ไฟดับตามหมายเลข CA (GET)
    """
    # 1. ตรวจสอบความถูกต้องของ query params (ca_number) ขาเข้า
    request_serializer = ActionStatusRequestSerializer(data=request.query_params)
    if not request_serializer.is_valid():
        return Response(request_serializer.errors, status=400)

    ca_number = request_serializer.validated_data.get("ca_number")

    try:
        report = CustomerReport.objects.filter(
            ca_number=ca_number, is_resolved=False
        ).last()

        # ปั้นโครงสร้างข้อมูลเตรียมส่งออก
        response_data = {
            "status": "first_time",
            "case_id": None,
            "case_status": None,
            "case_status_display": None,
            "oms_etr": None,
        }

        # ถ้าพบรายงานและผูกกับเคสหลักอยู่แล้ว
        if report and report.related_case:
            case = report.related_case
            response_data.update(
                {
                    "status": "repeated_event",
                    "case_id": case.case_id,
                    "case_status": case.status,
                    "case_status_display": case.get_status_display(),
                    "oms_etr": case.oms_etr.strftime("%H:%M น.")
                    if case.oms_etr
                    else "ยังไม่มีกำหนดเวลาชัดเจน",
                }
            )

        # 2. ใช้ Serializer จัดฟอร์แมตข้อมูลขากลับให้ได้มาตรฐานตามโครงสร้างที่ Agent รอรับ
        response_serializer = ActionStatusResponseSerializer(response_data)
        return Response(response_serializer.data)

    except Exception as e:
        return Response({"error": str(e)}, status=500)