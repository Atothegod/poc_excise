from rest_framework import serializers


class AgentReportSerializer(serializers.Serializer):
    session_id = serializers.CharField(
        max_length=255, required=False, allow_blank=True, allow_null=True
    )
    ca_number = serializers.CharField(max_length=12)
    latitude = serializers.FloatField(required=False, allow_null=True)
    longitude = serializers.FloatField(required=False, allow_null=True)
    chat_history = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    tool_used = serializers.CharField(max_length=10, required=False, allow_blank=True, allow_null=True)




class ActionStatusRequestSerializer(serializers.Serializer):
    """ใช้ตรวจพารามิเตอร์ขาเข้าตอน Agent ยิง GET มาถาม (?ca_number=xxxx)"""

    ca_number = serializers.CharField(max_length=12, min_length=12)


class ActionStatusResponseSerializer(serializers.Serializer):
    """ใช้ควบคุมหน้าตาข้อมูลขากลับส่งไปให้ Agent"""

    status = serializers.CharField()  # 'first_time' หรือ 'repeated_event'
    case_id = serializers.UUIDField(allow_null=True)
    case_status = serializers.CharField(allow_null=True)
    case_status_display = serializers.CharField(allow_null=True)  # ข้อความภาษาไทย
    oms_etr = serializers.CharField(allow_null=True)  # เวลาซ่อมเสร็จ