import re

from rest_framework import serializers


CA_NUMBER_PATTERN = re.compile(r"^\d{11,12}$")


def validate_ca_number_format(value):
    ca_number = str(value).strip()
    if not CA_NUMBER_PATTERN.fullmatch(ca_number):
        raise serializers.ValidationError(
            "CA number must contain 11 or 12 digits with no letters or symbols."
        )
    return ca_number


class AgentReportSerializer(serializers.Serializer):
    session_id = serializers.CharField(
        max_length=255, required=False, allow_blank=True, allow_null=True
    )
    ca_number = serializers.CharField(max_length=12, min_length=11)
    latitude = serializers.FloatField(required=False, allow_null=True)
    longitude = serializers.FloatField(required=False, allow_null=True)
    chat_history = serializers.CharField(
        required=False, allow_blank=True, allow_null=True
    )
    tool_used = serializers.CharField(
        max_length=50, required=False, allow_blank=True, allow_null=True
    )
    pdpa_consent = serializers.BooleanField(required=False, default=False)

    # ใช้ DateTimeField เพื่อให้ DRF ตรวจสอบความถูกต้องของ ISO Format ทันที
    time_stamp = serializers.DateTimeField(required=False, allow_null=True)

    def validate_ca_number(self, value):
        return validate_ca_number_format(value)


class ActionStatusRequestSerializer(serializers.Serializer):
    """ใช้ตรวจพารามิเตอร์ขาเข้าตอน Agent ยิง GET มาถาม (?ca_number=xxxx)"""

    ca_number = serializers.CharField(max_length=12, min_length=11)

    def validate_ca_number(self, value):
        return validate_ca_number_format(value)


class ActionStatusResponseSerializer(serializers.Serializer):
    """ใช้ควบคุมหน้าตาข้อมูลขากลับส่งไปให้ Agent"""

    status = serializers.CharField()  # 'first_time' หรือ 'active_case_exists'
    case_id = serializers.UUIDField(allow_null=True)
    case_status = serializers.CharField(allow_null=True)
    case_status_display = serializers.CharField(allow_null=True)

    # ปรับจาก CharField เป็น DateTimeField เพื่อให้เวลาขากลับอยู่ในรูป ISO 8601 ที่ได้มาตรฐานขอรับ
    oms_etr = serializers.DateTimeField(allow_null=True)
    etr_target_time = serializers.DateTimeField(allow_null=True, required=False)
    etr_source = serializers.CharField(allow_null=True, required=False)
    pluem_etr_minutes = serializers.FloatField(allow_null=True, required=False)
