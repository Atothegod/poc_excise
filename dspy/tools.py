import requests
import os
import contextvars

DJANGO_API_URL = os.getenv("DJANGO_API_URL", "http://backend:8000/api")

current_session_id = contextvars.ContextVar("current_session_id", default="unknown")
current_time_stamp = contextvars.ContextVar("current_time_stamp", default=None)


def save_report_to_db(ca_number: str, latitude: float, longitude: float):
    endpoint = f"{DJANGO_API_URL}/reports/sync/"
    payload = {
        "session_id": current_session_id.get(),
        "ca_number": ca_number,
        "latitude": latitude,
        "longitude": longitude,
        "time_stamp": current_time_stamp.get(),
    }

    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = requests.post(endpoint, json=payload, timeout=5)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            if attempt == max_retries - 1:
                return {"event_type": "api_error"}
    return None


def check_CA_number(ca_number: str):
    # Base coordinate: (9.2117, 100.926296)
    positions = [
        # --- WITHIN 5 KM (< 0.045 degrees difference) ---
        (9.2217, 100.926296),  # Index 0: ~1.1 km North
        (9.1917, 100.926296),  # Index 1: ~2.2 km South
        (9.2117, 100.956296),  # Index 2: ~3.3 km East
        (9.2317, 100.906296),  # Index 3: ~3.1 km Northwest
        (9.1817, 100.936296),  # Index 4: ~3.5 km Southeast
        # --- GREATER THAN 5 KM (> 0.045 degrees difference) ---
        (9.2917, 100.926296),  # Index 5: ~8.8 km North
        (9.1117, 100.926296),  # Index 6: ~11.1 km South
        (9.2117, 101.076296),  # Index 7: ~16.5 km East
        (9.3317, 100.806296),  # Index 8: ~18.8 km Northwest
        (9.1517, 100.996296),  # Index 9: ~10.2 km Southeast
    ]

    try:
        int_ca = int(ca_number)
    except ValueError:  # Catching ValueError is safer than a bare Exception for casting
        return positions[0]

    return positions[int_ca % 10]


def Check_Outage_Tool(ca_number: str):
    latitude, longitude = check_CA_number(ca_number)
    db_response = save_report_to_db(ca_number, latitude, longitude)

    if not db_response:
        return "ขัดข้อง ไม่สามารถเชื่อมต่อกับระบบได้"

    event_type = db_response.get("event_type")
    eta = db_response.get("eta_target_time")
    etr = db_response.get("oms_etr")

    # เคส 1: API ขัดข้องติดต่อกันจนครบกำหนด
    if event_type == "api_error":
        return "ขัดข้อง: API_Timeout เกิน 3 ครั้ง โปรดแจ้งลูกค้าว่าเปลี่ยนสถานะเป็นโอนสายให้ Human Agent"

    # เคส 2: เกิดเหตุวงกว้าง (Mass Outage) -> บังคับแจ้ง ETR ตาม Rule 6
    if event_type == "repeated_event":
        if etr:
            return f"[เหตุวงกว้าง] แจ้ง ETR แก่ลูกค้า: {etr}"
        else:
            return "[เหตุวงกว้าง] กำลังเชื่อมต่อกับโมเดล ETR พี่ปลื้มครับ"

    # เคส 3: แจ้งครั้งแรก (New Event) หรือ เคสเดี่ยว -> บังคับแจ้ง ETA ตาม Rule 7
    # และตรวจสอบ ETR เพิ่มเติม
    elif event_type == "new_event":
        if etr:
            return (
                f"[เหตุแจ้งใหม่] ระบบได้เปิดใบงานใหม่แล้ว ให้แจ้งเวลาที่ช่างจะเดินทางไปถึง (ETA): {eta} "
                f"และแจ้งเวลาที่คาดว่าจะแก้ไขเสร็จ (ETR): {etr}"
            )
        else:
            return (
                f"[เหตุแจ้งใหม่] ระบบได้เปิดใบงานใหม่แล้ว ให้แจ้งเวลาที่ช่างจะเดินทางไปถึง (ETA): {eta}"
            )

    return "ขัดข้อง ไม่สามารถระบุประเภทเหตุการณ์ได้"


def Fast_Track_Tool(ca_number: str):
    """
    เครื่องมือสำหรับใช้สร้างตั๋ว Fast-track ด่วน
    เมื่อลูกค้าบอกว่าไฟยังไม่มาและเช็คเบรกเกอร์แล้ว
    """
    endpoint = f"{DJANGO_API_URL}/reports/fast-track/"
    payload = {"ca_number": ca_number}

    try:
        response = requests.post(endpoint, json=payload, timeout=5)
        response.raise_for_status()
        data = response.json()

        if data.get("event_type") == "fallback_to_human":
            return "[FallBack] โควต้าแจ้งซ้ำหมดแล้ว ให้ตอบว่ากำลังโอนสายให้เจ้าหน้าที่ (Force Fallback)"
        else:
            return "[Success] สร้างตั๋ว Fast-track สำเร็จ ให้ตอบลูกค้าว่าประสานงานด่วนแล้ว"

    except Exception as e:
        return "[FallBack] ระบบขัดข้อง ให้ตอบว่ากำลังโอนสายให้เจ้าหน้าที่ (Force Fallback)"
