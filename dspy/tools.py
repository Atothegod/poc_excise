import requests
import os
import json
import contextvars

DJANGO_API_URL = os.getenv("DJANGO_API_URL", "http://backend:8000/api")

# สร้างตัวแปรทะลุมิติสำหรับเก็บค่าชั่วคราว
current_session_id = contextvars.ContextVar("current_session_id", default="unknown")
current_time_stamp = contextvars.ContextVar("current_time_stamp", default=None)


def save_report_to_db(ca_number: str, latitude: float, longitude: float):
    """
    ฟังก์ชันส่งข้อมูลไปบันทึกที่ Django และรับค่าการคำนวณ (ETA/ETR/Event Type) กลับมาในรวดเดียว
    """
    endpoint = f"{DJANGO_API_URL}/reports/sync/"

    # ดึงค่าจากตัวแปรทะลุมิติมาใช้งานตรงนี้เลย
    session_id = current_session_id.get()
    time_stamp = current_time_stamp.get()

    payload = {
        "session_id": session_id,
        "ca_number": ca_number,
        "latitude": latitude,
        "longitude": longitude,
        "chat_history": "[]",
        "tool_used": "CHECK",
        "time_stamp": time_stamp,
    }

    try:
        response = requests.post(endpoint, json=payload, timeout=5)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"[API Sync Error] ยิง API ไม่สำเร็จ: {e}")
        return None


# -------------------------------------------------------------------------#


# import random  # <--- อย่าลืม import random เพิ่มไว้ด้านบนสุดของไฟล์ tools.py นะขอรับ


# def check_CA_number(ca_number: str):
#     """
#     จำลองการหาพิกัดจาก CA Number
#     สุ่มให้อยู่ในพื้นที่ จังหวัดปทุมธานี (Lat: 13.92 - 14.28, Lon: 100.37 - 100.93)
#     """
#     min_lat, max_lat = 13.92, 14.28
#     min_lon, max_lon = 100.37, 100.93

#     # สุ่มตัวเลขทศนิยม (Float) ให้อยู่ในขอบเขตที่กำหนด
#     latitude = random.uniform(min_lat, max_lat)
#     longitude = random.uniform(min_lon, max_lon)

#     # ปัดเศษทศนิยมให้เหลือ 6 ตำแหน่ง (มาตรฐาน GPS ทั่วไป)
#     return round(latitude, 6), round(longitude, 6)

def check_CA_number(ca_number: str):
    """จำลองการหาพิกัดจาก CA Number"""
    latitude, longitude = 9.2117, 100.926296
    return latitude, longitude

def Check_Outage_Tool(ca_number: str):
    """
    ตรวจสอบข้อมูลไฟดับจากหมายเลขผู้ใช้ไฟ (CA Number)
    เพื่อประเมินว่าเป็นเหตุขัดข้องใหม่ (Normal) หรือเหตุวงกว้าง (Mass Outage)
    ระบบจะคืนค่าคำแนะนำให้ AI ตอบลูกค้าอย่างถูกต้องตามขั้นตอน
    """
    latitude, longitude = check_CA_number(ca_number)

    # ไม่ต้องส่ง session_id หรือ time_stamp แล้ว เพราะมันทะลุมิติไปรออยู่แล้ว!
    db_response = save_report_to_db(ca_number, latitude, longitude)

    if db_response:
        event_type = db_response.get("event_type")
        eta = db_response.get("eta_target_time")
        etr = db_response.get("oms_etr")

        # สาย A: กรณีไฟดับบริเวณกว้าง (Mass Outage / Repeated Event)
        if event_type == "repeated_event":
            if etr:
                return f"[เหตุวงกว้าง] พบเหตุขัดข้องในพื้นที่ แจ้งเวลาไฟมา (ETR) แก่ลูกค้าคือ: {etr} (ไม่ต้องแจ้งเวลาช่างถึง)"
            else:
                return "[เหตุวงกว้าง] พบเหตุขัดข้องในพื้นที่ แต่ช่างยังไม่ได้ประเมินเวลาไฟมา (ETR) โปรดแจ้งลูกค้าว่ารับทราบปัญหาและกำลังเร่งแก้ไข (ไม่ต้องแจ้งเวลาช่างถึง)"

        # สาย B: กรณีไฟดับปกติ (Normal / New Outage)
        elif event_type == "new_event":
            if etr:
                return f"[เหตุปกติ] ระบบอัปเดตข้อมูลแล้ว แจ้งเวลาไฟมา (ETR): {etr}"
            else:
                # ตรงตาม Flow: แจ้ง ETA ก่อนเสมอ และบอกว่า ETR ต้องรอประเมิน
                return f"[เหตุแจ้งใหม่] ให้แจ้งเวลาช่างถึงหน้างาน (ETA) แก่ลูกค้าคือ: {eta}"

    return "ขัดข้อง: ไม่สามารถเชื่อมต่อกับระบบฐานข้อมูลได้ โปรดแจ้งลูกค้าว่ากำลังประสานงานรับเรื่องให้"
