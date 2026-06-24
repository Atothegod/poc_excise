from rest_framework import serializers


class AgentReportSerializer(serializers.Serializer):
    session_id = serializers.CharField(
        max_length=255, required=False, allow_blank=True, allow_null=True
    )
    ca_number = serializers.CharField(max_length=12)
    latitude = serializers.FloatField(required=False, allow_null=True)
    longitude = serializers.FloatField(required=False, allow_null=True)
    chat_history = serializers.CharField(
        required=False, allow_blank=True, allow_null=True
    )
    tool_used = serializers.CharField(
        max_length=50, required=False, allow_blank=True, allow_null=True
    )

    # ใช้ DateTimeField เพื่อให้ DRF ตรวจสอบความถูกต้องของ ISO Format ทันที
    time_stamp = serializers.DateTimeField(required=False, allow_null=True)


class ActionStatusRequestSerializer(serializers.Serializer):
    """ใช้ตรวจพารามิเตอร์ขาเข้าตอน Agent ยิง GET มาถาม (?ca_number=xxxx)"""

    # ปรับ min_length เป็น 9 เพื่อไม่ให้บล็อกหมายเลขผู้ใช้ไฟรุ่นเก่าขอรับ
    ca_number = serializers.CharField(max_length=12, min_length=9)


class ActionStatusResponseSerializer(serializers.Serializer):
    """ใช้ควบคุมหน้าตาข้อมูลขากลับส่งไปให้ Agent"""

    status = serializers.CharField()  # 'first_time' หรือ 'repeated_event'
    case_id = serializers.UUIDField(allow_null=True)
    case_status = serializers.CharField(allow_null=True)
    case_status_display = serializers.CharField(allow_null=True)

    # ปรับจาก CharField เป็น DateTimeField เพื่อให้เวลาขากลับอยู่ในรูป ISO 8601 ที่ได้มาตรฐานขอรับ
    oms_etr = serializers.DateTimeField(allow_null=True)
