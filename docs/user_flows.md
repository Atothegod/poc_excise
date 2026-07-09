# User Flows

เอกสารนี้สรุป flow หลักของระบบในมุมผู้ใช้ ลูกค้า operator และระบบ webhook/timer

## 1. Customer Login Flow

1. User เปิดหน้า Django login ที่ `/`.
2. UI เรียก `GET /api/reports/validate-ca/?ca_number=...`.
3. Django ตรวจ CA จาก `CustomerLocation`.
4. ถ้า CA ถูกต้อง UI เรียก `POST /api/reports/session-login/`.
5. Django สร้างหรือ reuse `CustomerReport` ของ `session_id + ca_number`.
6. Django บันทึก PDPA consent และ customer name/lat/lon.
7. ถ้า CA นั้นอยู่ใน active group case จาก OMS อยู่แล้ว Django link report เข้ากับ case นั้น.
8. User เข้าหน้า `/chat/`.

Failure cases:

- CA format ไม่ใช่ 12 หลัก: return `400`.
- ไม่พบ CA ใน Django `CustomerLocation`: return `404`.
- PDPA consent ไม่ครบ: return validation error.

## 2. Customer Reports Power Outage via Chat

1. User ส่งข้อความใน `/chat/`.
2. Browser เรียก Django `/agent/ask/`.
3. Django proxy ไป `dspy-agent /ask`.
4. DSPy agent วิเคราะห์ข้อความและเรียก tool `Check_Outage_Tool`.
5. Tool เรียก Django `POST /api/reports/sync/`.
6. Django ตรวจ CA จาก `CustomerLocation`.
7. Django reuse active case เฉพาะกรณี:
   - session นี้มี active case อยู่แล้ว
   - CA เดียวกันมี active report/case อยู่แล้ว
   - CA อยู่ใน `affected_ca_numbers` ของ active OMS group case
8. ถ้าไม่พบ active case Django สร้าง `OutageCase` ใหม่แบบ `normal` และ `affected_ca_numbers=[ca_number]`.
9. Django เรียก PEA assessment เพื่อหา ETA/Pluem ETR.
10. Django schedule Celery ETA timer.
11. Response กลับ agent พร้อม `event_type`, `case_id`, `eta_target_time`, `fastest_branch`, ETR/SLA fields.
12. Agent ตอบ user และ sync chat history กลับ Django.

Important behavior:

- Django ไม่เรียก `oms_api /cases/report`.
- Django ไม่รวมเคสจาก CA ใกล้กัน.
- 3 CA ใกล้กันจาก chat flow จะยังเป็น 3 normal cases.
- Mass outage จะเกิดจาก OMS event เท่านั้น.

## 3. Same CA Re-Report Flow

1. User หรือ session อื่นแจ้ง CA เดิมซ้ำ.
2. DSPy agent เรียก `/api/reports/sync/`.
3. Django หา active case ของ CA เดิม.
4. ถ้ามี active case จะ return `event_type=existing_ca_case`.
5. Django attach waiting reports ของ CA เดียวกันเข้ากับ case เดิม.
6. ไม่สร้าง case ใหม่และไม่เรียก assessment ซ้ำ.

## 4. OMS Operator Opens Group Case from Map UI

1. Operator เปิด `oms_api /ui` ที่ `http://localhost:8002/ui`.
2. UI โหลด CA จาก `GET /customers`.
3. UI plot CA บน Leaflet map.
4. Operator คลิก marker CA.
5. UI แสดงวงกลม radius สีฟ้า.
6. Operator ปรับ `radius_km`.
7. Operator กด Select.
8. UI เรียก `GET /customers/nearby?ca_number=...&radius_km=...`.
9. `oms_api` คำนวณระยะจาก CSV และเติม `Affected CA Numbers`.
10. Operator กด Open.
11. `oms_api` generate หรือใช้ `case_id` UUID แล้ว POST event ไป Django `/api/oms/events/`.
12. Django upsert `OutageCase` เป็น `mass_outage` เมื่อ CA >= 3.
13. Django attach active `CustomerReport` ที่ CA อยู่ในกลุ่ม.
14. Django mark single active cases ที่ถูกแทนที่เป็น `merged`.
15. Django ส่ง proactive mass outage alert ไป session ที่เกี่ยวข้องผ่าน Celery/DSPy webhook.

Result:

- Case จริงอยู่ใน Django DB.
- `oms_api` ไม่เก็บตาราง case เป็น source of truth.
- Existing single-CA cases ที่ถูกรวมจะกลายเป็น child/merged case เพื่อคง audit trail.

## 5. External OMS/PEA Sync Flow

1. External OMS/PEA เรียก `POST /api/v1/oms/outage/sync` ที่ `oms_api`.
2. ถ้า `OMS_API_TOKEN` ถูก set ต้องส่ง `Authorization: Bearer <token>`.
3. `oms_api` map fields:
   - `eventId` -> `external_event_id`
   - `caList` -> `affected_ca_numbers`
   - `outageTime` -> `outage_time`
   - `etr` -> `oms_etr`
   - `OPEN` -> `case_opened`
   - `CLOSED` -> `case_closed`
4. `oms_api` POST ต่อไป Django `/api/oms/events/`.
5. Django upsert by `external_event_id`; ถ้าไม่พบจึงสร้าง `case_id` ภายในใหม่.
6. Django response success กลับ `oms_api`.
7. `oms_api` ตอบตาม API spec ด้วย `{statusCode, message, data}`.

## 6. OMS Updates ETR

1. Operator เปิด OMS UI หรือ external OMS sync ส่ง ETR ใหม่.
2. `oms_api` ส่ง `event_type=etr_updated` ไป Django `/api/oms/events/`.
3. Django update `OutageCase.oms_etr` ด้วย `save()`.
4. Django signal ตรวจพบ ETR เปลี่ยน.
5. Signal ยกเลิก Celery ETR task เก่า ถ้ามี.
6. Signal schedule `check_etr_timeout` ใหม่.
7. Signal ส่ง proactive alert ไป active sessions ผ่าน `send_proactive_alert.delay`.
8. Celery task POST ไป `dspy-agent /webhook/notify`.
9. dspy-agent เก็บ pending notification และ append system alert เข้า chat history.
10. Chat UI poll notification ผ่าน Django proxy แล้วแสดงให้ user.

## 7. OMS Closes Case and Closed-Loop Flow

1. Operator กด Close ใน OMS UI หรือ external OMS ส่ง `status=CLOSED`.
2. `oms_api` ส่ง `event_type=case_closed` ไป Django `/api/oms/events/`.
3. Django update `OutageCase.status=restored` ด้วย `save()`.
4. Django signal:
   - สร้าง `OutageRestorationLog`
   - ยกเลิก ETA/ETR timers
   - mark related `CustomerReport.is_resolved=True`
   - ส่ง closed-loop prompt ไป user ผ่าน `dspy-agent /webhook/notify`
5. User เห็น prompt ว่าไฟกลับหรือยัง.
6. ถ้า user ตอบว่าไฟมาแล้ว:
   - Agent thank/confirm resolved.
7. ถ้า user ตอบว่ายังไม่มีไฟ:
   - Agent เรียก `Check_Outage_Tool` อีกครั้ง.
   - Django สร้าง normal case ใหม่สำหรับ CA นั้น.

## 8. ETA Timeout Flow

1. ตอนสร้าง normal case Django schedule `check_eta_timeout`.
2. เมื่อถึง ETA แล้วยังไม่ restored, Celery task ตรวจ case.
3. ถ้ามี `oms_etr` ใช้ OMS ETR.
4. ถ้าไม่มี `oms_etr` แต่มี `pluem_etr_target_time` ใช้ Pluem ETR.
5. ถ้ายังไม่มี Pluem ETR task จะเรียก PEA assessment อีกครั้งโดยใช้ `ca_number` และ lat/lon จาก report/case.
6. Celery ส่ง proactive alert ไป `dspy-agent /webhook/notify`.
7. Agent memory/chat history ถูก update เพื่อไม่ให้ bot ตีความเวลาเองผิด.

## 9. ETR Timeout/SLA Flow

1. เมื่อ OMS ETR ถูกตั้ง ระบบ schedule `check_etr_timeout`.
2. เมื่อถึง ETR แล้วยังไม่ restored, Celery ส่ง `event_type=etr_timeout_sla`.
3. Agent ใช้ event นี้ใน chat history เพื่อบอก user ว่าเวลาคาดการณ์เดิมผ่านแล้ว.
4. Agent relay SLA/next deadline จาก Django response แทนการเดาเอง.

## 10. Ops Page Flow in Django

1. Operator เปิด `/ops/webhook/` หรือ `/ops/map/`.
2. UI เรียก:
   - `/ops/cases/`
   - `/ops/map/data/`
3. Operator filter/inspect cases.
4. Operator ใช้ `/ops/cases/action/` เพื่อ set ETR, set ETA now, หรือ close selected cases.
5. การแก้ผ่าน Django ops page ยัง trigger signals เช่นเดียวกับ OMS event.

## Flow Summary

| Scenario | Owner | Result |
| --- | --- | --- |
| Customer reports outage | Django | Single normal case |
| Same CA repeats | Django | Reuse active same-CA case |
| Nearby CAs report separately | Django | Separate normal cases |
| Operator selects group by map | OMS UI -> Django | Mass outage case in Django |
| External OMS syncs event | OMS API -> Django | Upsert by external event |
| ETR update | Django signals | Notify sessions + schedule timer |
| Case close | Django signals | Restoration log + closed-loop prompt |

