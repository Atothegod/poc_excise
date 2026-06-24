import requests
import os
import json

#------------------------------------------------------------------------------------------------------------------------------------------#

DJANGO_API_URL = "http://backend:8000/api"

def save_report_to_db(ca_number: str, latitude: float, longitude: float, tool_used: str, session_id: str = "unknown", chat_history: list = None):
    """
    ฟังก์ชันส่งข้อมูลไปบันทึก/อัปเดตที่ Django Backend ผ่าน REST API
    """
    endpoint = f"{DJANGO_API_URL}/reports/sync/"
    
    # แปลง list เป็น JSON String ก่อนส่ง ถ้าไม่มีประวัติให้เป็นว่างๆไว้
    chat_history_str = json.dumps(chat_history) if chat_history else "[]"
    
    payload = {
        "session_id": session_id,
        "ca_number": ca_number,
        "latitude": latitude,
        "longitude": longitude,
        "chat_history": chat_history_str,
        "tool_used": tool_used
    }
    
    try:
        # ยิงไปที่ Backend (ใช้ timeout เพื่อไม่ให้ Agent ค้างนานถ้า Backend ปิดอยู่)
        response = requests.post(endpoint, json=payload, timeout=5)
        response.raise_for_status()
        print(f"[API Sync] บันทึกข้อมูล CA {ca_number} สำเร็จ:", response.json())
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"[API Sync Error] ยิง API ไม่สำเร็จ: {e}")
        # แม้ยิงไม่ผ่าน ก็แค่ปรินท์ error แจ้งเตือน Agent จะได้ทำงานต่อไม่ล่ม
        return None


#------------------------------------------------------------------------------------------------------------------------------------------#


def check_CA_number(ca_number: str):
    """
    ตรวจสอบหมายเลขบัญชีผู้ใช้ (CA Number) เพื่อคืนค่าพิกัดละติจูดและลองจิจูด
    """
    latitude, longitude = 13.0, 104.0
    return latitude, longitude


def ETA_estimator(ca_number: str):
    """
    Use lat, lon to estimate the ETA of the technician's arrival at the destination via api
    """
    latitude, longitude = check_CA_number(ca_number)
    save_report_to_db(ca_number, latitude, longitude, tool_used="ETA")
    return "25 minutes"


#------------------------------------------------------------------------------------------------------------------------------------------#


def check_event(ca_number: str):
    """
    ยิงไปถาม Django ว่า CA นี้ ผูกกับ OutageCase ไหนอยู่ และสถานะเป็นอย่างไร
    """
    endpoint = f"{DJANGO_API_URL}/reports/status/"
    try:
        # ยิง GET พร้อมส่ง Query Parameters (?ca_number=xxxx)
        response = requests.get(endpoint, params={"ca_number": ca_number}, timeout=5)
        response.raise_for_status()
        data = response.json()

        # คืนค่าสถานะ และข้อมูลจากเคสหลักกลับไปให้ตัวประมาณการใช้งาน
        return data.get("status"), data.get("case_status_display"), data.get("oms_etr")
    except Exception as e:
        print(f"[API Status Error] ไม่สามารถดึงสถานะได้: {e}")
        return "first_time", None, None


        #####
        #####
        #####
        #####
        #####
    #############
        ######
          ##


def ETR_estimator(ca_number: str):
    """
    ประเมินเวลาไฟมา (ETR) โดยตรวจสอบจากสถานะเคสที่ผูกไว้ในระบบ
    """
    latitude, longitude = check_CA_number(ca_number)

    # 1. บันทึก/อัปเดตการแจ้งเหตุก่อน (สร้างเคสจำลองเชื่อมโยงในระบบ)
    save_report_to_db(ca_number, latitude, longitude, tool_used="ETR")

    # 2. เรียกเช็คสถานะเหตุการณ์ผ่าน API เส้น GET ตัวใหม่ (ค้นหาตาม case_id ของ CA นี้)
    status, case_status_display, oms_etr = check_event(ca_number)

    if status == "repeated_event":
        # ดึงประโยคจากฐานข้อมูลไปให้ Gemini สรุปคำพูดตอบลูกค้า
        return f"ตรวจพบเหตุการณ์ไฟดับในระบบพิกัดของคุณ ปัจจุบันสถานะคือ '{case_status_display}' คาดว่าจะจ่ายไฟคืนกระแสสำเร็จในเวลา {oms_etr}"

    # ถ้าเป็นเคสใหม่ (First time) ที่ไม่มีประวัติในรัศมี 5km (เดี๋ยวเราจะเขียนคณิตศาสตร์ค้นหาเพิ่มใน Django ทีหลัง)
    print(
        f"Fallback: Querying historical DB within 5km of ({latitude}, {longitude})..."
    )
    return "เพิ่งได้รับแจ้งเหตุครั้งแรกในบริเวณนี้ คาดว่าจะใช้เวลาแก้ไขประมาณ 45 นาที"








