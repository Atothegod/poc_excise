# Process Workflow Version 1.8 (Current System / As-Is)

เอกสารฉบับนี้อธิบายกระบวนการทำงานของระบบปัจจุบันตามโค้ดที่มีอยู่จริงใน repository นี้ โดยยึดชื่อและแนวคิดจาก `Process Workflow Version 1.8` แต่ปรับเนื้อหาให้ตรงกับพฤติกรรมปัจจุบันของระบบ

หมายเหตุสำคัญ:

- เอกสารนี้เป็น As-Is Workflow ไม่ใช่ Target Workflow
- การ "ปิดเคส" ในระบบปัจจุบันหมายถึงการตั้งค่า `OutageCase.status = restored`
- สถานะ `restored` มี display label ว่า `จ่ายไฟคืนกระแสสำเร็จ`
- หลัง `OutageCase` ถูกปิด ระบบจึงค่อยตั้ง `CustomerReport.is_resolved = True` และส่ง closed-loop prompt ไปหาลูกค้า

## 1. ภาพรวมระบบ

ระบบปัจจุบันเป็นระบบรับแจ้งเหตุไฟฟ้าขัดข้องผ่านหน้าเว็บแชท โดยมี DSPy Agent เป็นตัวกลางสนทนากับลูกค้า และมี Django OMS เป็นเจ้าของข้อมูลเคส, รายงานลูกค้า, เวลา ETA/ETR, SLA, timer และ proactive notification

ระบบทำงานโดยใช้ CA Number เป็นตัวระบุผู้ใช้ไฟหลัก เมื่อผู้ใช้เข้าสู่ระบบด้วย CA แล้ว Agent จะใช้ CA จาก login context เพื่อเปิดหรือค้นหาเคสไฟดับใน OMS

## 2. Actors

- ผู้ใช้ไฟฟ้า: ผู้แจ้งเหตุผ่านหน้าเว็บแชท
- Chat UI: หน้า login และหน้า chat ของ Django
- DSPy Agent: ระบบสนทนา, คุม context, เรียก tool, เก็บ chat history
- Django OMS API: ระบบจัดการ CustomerReport และ OutageCase
- OMS / Admin Console: ช่องทางเจ้าหน้าที่ตั้ง ETR, ตั้ง ETA เป็นเวลาปัจจุบัน, และปิดเคส
- Celery Worker: ระบบจับเวลา ETA timeout และ ETR timeout
- DSPy Webhook Server: ตัวรับ proactive notification และส่งต่อให้หน้า chat ผ่าน polling
- pea-estimated.services: บริการประเมินสาขาที่เร็วที่สุด, ETA และ model ETR

## 3. Vocabulary & Current State

### CA

หมายเลขผู้ใช้ไฟ 12 หลัก ใช้เป็น key หลักในการตรวจสอบ CustomerLocation และ deduplicate เคสของผู้ใช้ไฟรายเดียวกัน

### OutageCase

เคสไฟฟ้าขัดข้องใน OMS ปัจจุบันมีสถานะหลัก:

- `reported`: ได้รับแจ้งเหตุ
- `investigating`: กำลังดำเนินการตรวจสอบ
- `repairing`: กำลังดำเนินการซ่อมแซม
- `restored`: จ่ายไฟคืนกระแสสำเร็จ

### CustomerReport

รายงานหรือ session ของลูกค้าที่ผูกกับ CA และอาจผูกกับ OutageCase หนึ่งรายการ

### Active Case

ในระบบปัจจุบัน active case คือเคสที่ `OutageCase.status != restored` และ report ที่เกี่ยวข้องยังไม่ `is_resolved`

### ETA

เวลาเป้าหมายที่ช่างจะถึงหน้างาน เก็บใน `OutageCase.eta_target_time`

### OMS ETR

เวลาคาดการณ์จ่ายไฟคืนจากเจ้าหน้าที่ OMS/Admin เก็บใน `OutageCase.oms_etr` และถือเป็นแหล่งข้อมูลหลักเหนือ model ETR

### Model ETR

เวลาคาดการณ์จ่ายไฟคืนจาก `pea-estimated.services` เก็บใน `pluem_etr_minutes` และ `pluem_etr_target_time` แต่จะไม่แสดงให้ลูกค้าตอนเปิดเคสใหม่ ยกเว้นหลัง ETA timeout หรือในบริบทที่ระบบอนุญาต

### SLA

ระบบปัจจุบันใช้ SLA 4 ชั่วโมง โดยเก็บ:

- `sla_reference_time`
- `sla_target_time`
- `sla_reason`

โดยทั่วไป SLA จะอิงจากเวลาเริ่มเคสหรือเวลาที่ระบบสร้างเคส

## 4. Login, CA Validation และ Session Registration

1. ผู้ใช้เข้าสู่หน้า login
2. ผู้ใช้กรอก CA 12 หลัก
3. ระบบปัจจุบันยังบังคับให้ติ๊ก PDPA consent ก่อนเข้าสู่แชท
4. Chat UI เรียก `GET /api/reports/validate-ca/`
5. Django ตรวจสอบว่ามี CA ใน `CustomerLocation`
6. ถ้าพบ CA ระบบบันทึก CA และ consent ลง localStorage แล้วเข้าสู่หน้า chat
7. เมื่อเข้า chat ระบบเรียก `POST /api/reports/session-login/`
8. Backend สร้างหรือดึง `CustomerReport` ของ session นี้
9. Backend เติมข้อมูลพิกัดจาก `CustomerLocation`
10. Backend grant PDPA consent ให้ report
11. Backend พยายามผูก report นี้เข้ากับ active case ของ CA เดียวกัน ถ้ามี

สถานะปัจจุบัน:

- PDPA ยังเป็น gate ใน UI, serializer, agent context และ tool
- ระบบยังไม่ใช่ flow แบบถอด PDPA ออกตาม target v1.8

## 5. Agent Context Management / Heart Mode ปัจจุบัน

DSPy Agent ใช้ `chat_history` และ system context เพื่อคุมการสนทนา โดยมี guardrail หลัก:

- ใช้ server-side Thailand time เป็นเวลาจริง
- ไม่เชื่อเวลาที่ผู้ใช้พิมพ์เอง
- ใช้ CA จาก login context
- ไม่ถาม CA หรือ PDPA ซ้ำใน chat
- ถ้าเจตนาผู้ใช้เกี่ยวกับไฟดับหรือถามสถานะไฟดับ ให้เรียก `Check_Outage_Tool`
- ไม่ใช้คำว่า ETA, ETR, SLA ในข้อความลูกค้า
- ถ้ามี `closed_loop_prompt` และลูกค้าเลือก/บอกว่าไฟมาแล้ว ให้ตอบรับและจบ flow
- ถ้ามี `closed_loop_prompt` และลูกค้าเลือก/บอกว่าไฟยังไม่มา ให้เรียก `Fast_Track_Tool`

## 6. Sync Report และการสร้าง/ผูกเคส

เมื่อ Agent เรียก `Check_Outage_Tool` ระบบจะเรียก `POST /api/reports/sync/`

### 6.1 ตรวจ active case ของ CA เดียวกัน

1. Backend สร้างหรือดึง active `CustomerReport` จาก `session_id + ca_number`
2. เติมข้อมูลลูกค้าจาก `CustomerLocation`
3. ถ้า report นี้มี active related case อยู่แล้ว และเคสนั้นยังไม่ `restored`:
   - คืน `event_type = existing_ca_case`
   - ส่งข้อมูลเคสเดิมกลับไปให้ Agent
4. ถ้า report นี้ยังไม่มีเคส แต่มี report อื่นของ CA เดียวกันที่ผูก active case:
   - ผูก report ใหม่เข้ากับเคสเดิม
   - คืน `event_type = existing_ca_case`

ผลลัพธ์:

- ระบบ deduplicate ตาม CA เดียวกันข้าม session ได้
- หลาย session ของ CA เดียวกันจะถูกผูกกับ OutageCase เดียวกัน

### 6.2 ถ้าไม่พบพิกัดลูกค้า

ถ้าไม่มีพิกัดจาก `CustomerLocation` และ report ยังไม่มี related case:

- คืน `event_type = ca_not_found`
- ไม่สร้าง OutageCase

### 6.3 ตรวจเคสใกล้เคียง

ถ้ามีพิกัด และยังไม่มี related case:

1. Backend loop หา active OutageCase ที่ยังไม่ `restored`
2. ใช้ `calculate_distance()` ตรวจระยะ
3. แม้ caller จะเช็ค `dist <= 5.0` แต่ฟังก์ชันปัจจุบันคืนระยะจริงเฉพาะเมื่อจุดอยู่ในรัศมีประมาณ 0.0001 km หรือประมาณ 10 ซม.
4. ถ้าเจอเคสใกล้เคียง:
   - ผูก report เข้ากับเคสนั้น
   - คืน `event_type = repeated_event`

ผลลัพธ์:

- ในทางปฏิบัติ repeated event จะเกิดเมื่อพิกัดแทบตรงกันเท่านั้น
- ระบบยังไม่มี Area Topic หรือ Mass Outage Subscription ตาม target v1.8

### 6.4 สร้างเคสใหม่

ถ้าไม่พบ active case ที่เกี่ยวข้อง:

1. Backend เรียก `pea-estimated.services` ด้วย `ca_number`, `lat`, `lon`
2. รับข้อมูล:
   - fastest branch
   - ETA label
   - estimated ETR minutes จาก model
3. Parse ETA เป็นนาที
4. สร้าง `OutageCase` ใหม่:
   - `status = reported`
   - `case_type = normal`
   - `eta_target_time = base_time + eta_minutes`
   - `sla_reference_time = base_time`
   - `sla_target_time = base_time + 4 hours`
   - `sla_reason = case_created`
   - cache model ETR ใน `pluem_etr_minutes` และ `pluem_etr_target_time`
5. ผูก `CustomerReport.related_case` เข้ากับเคสใหม่
6. สั่ง Celery `check_eta_timeout` ให้รันตอน `eta_target_time`
7. เก็บ `celery_eta_task_id` ลง OutageCase
8. sync `affected_ca_numbers`
9. คืน `event_type = new_event`

ผลลัพธ์:

- ตอนเปิดเคสใหม่ response จะส่ง ETA ให้ Agent
- model ETR ถูกเก็บไว้ในเคส แต่ปกติไม่ expose ให้ลูกค้าทันที

## 7. การตอบกลับลูกค้าหลัง Sync Report

### New Event

Agent แจ้งว่าเปิดใบงานแล้ว และแจ้งเวลาช่างจะถึงหน้างานเป็นเวลาแบบ `HH:MM น.`

ถ้ามี OMS ETR อยู่ใน response จึงอาจแจ้งเวลาไฟกลับด้วย แต่กรณีเปิดเคสใหม่ปกติจะยังไม่มี OMS ETR

### Existing CA Case

Agent แจ้งว่าพบเคสเดิมของ CA นี้ และตอบเวลาล่าสุดที่ระบบมี:

- ถ้ามี OMS ETR ให้แจ้งเวลาไฟกลับ
- ถ้าไม่มี OMS ETR แต่มี ETA ให้แจ้งเวลาช่างถึงหน้างานเดิม

### Repeated Event

Agent ถือเป็นเหตุซ้ำ/เหตุบริเวณเดียวกัน และตอบเวลาไฟกลับถ้ามี ETR

## 8. OMS ETR Update

เจ้าหน้าที่สามารถตั้ง ETR ผ่าน OMS/Admin Console หรือ Ops Webhook Console

เมื่อ `OutageCase.oms_etr` เปลี่ยน:

1. Signal ตรวจจับว่า OMS ETR มีการเพิ่มหรือเปลี่ยน
2. ระบบตั้ง `oms_etr_updated_at`
3. ระบบไม่อัปเดต `sla_reference_time`, `sla_target_time` หรือ `sla_reason`
4. ถ้าเคสยังไม่ `restored` ระบบ schedule `check_etr_timeout` ที่เวลา `oms_etr`
5. เก็บ `celery_etr_task_id`
6. sync `affected_ca_numbers`
7. ส่ง proactive alert `event_type = etr_update` ไปยัง active session ที่ไม่ซ้ำกัน

หลักการปัจจุบัน:

- OMS ETR เป็นค่าหลัก
- ถ้ามี OMS ETR แล้ว ระบบจะใช้ OMS ETR เหนือ model ETR
- SLA ยึดเวลาเปิดเคสเท่านั้น ไม่เลื่อนตามเวลา ETR
- ระบบส่ง ETR update ทุกครั้งที่ `oms_etr` เปลี่ยน ยังไม่มี anti-spam threshold แบบเลื่อนอย่างมีนัยสำคัญ

## 9. ETA Timeout Flow

เมื่อถึง `eta_target_time` Celery task `check_eta_timeout` จะทำงาน

ขั้นตอนปัจจุบัน:

1. โหลด OutageCase
2. ถ้า `status = restored` ให้หยุด ไม่แจ้งเตือน
3. ถ้าเคสยังไม่ `restored` ให้ sync `affected_ca_numbers`
4. ถ้ายังไม่มี OMS ETR:
   - ใช้ model ETR ที่ cache ไว้ ถ้ามี
   - ถ้าไม่มี model ETR ให้เรียก `pea-estimated.services` เพื่อประเมินใหม่
5. ถ้ามี effective ETR:
   - ส่งข้อความว่าอยู่ระหว่างดำเนินการ และคาดว่าจะจ่ายไฟคืนประมาณเวลา `HH:MM น.`
6. ถ้าไม่มี effective ETR:
   - ส่งข้อความว่าอยู่ระหว่างดำเนินการ และระบบกำลังประเมินเวลาไฟกลับล่าสุด
7. ส่ง proactive notification ด้วย `event_type = eta_timeout` ไปยัง active session ของเคส

ข้อจำกัดปัจจุบัน:

- ระบบยังไม่มี status `Arrived`
- ETA timeout จึงเช็คแค่ว่าเคสปิดเป็น `restored` หรือยัง
- ถ้าเคสอยู่ใน `investigating` หรือ `repairing` task ยังสามารถส่ง ETA timeout notification ได้
- ยังไม่มี logic ระงับ Timer_ETA เมื่อได้รับ field update ประเภทอื่นนอกจากปิดเคสหรือแก้ ETA

## 10. ETR Timeout และ SLA Notification

เมื่อมี OMS ETR ระบบจะ schedule `check_etr_timeout` ให้รันที่เวลา `oms_etr`

ขั้นตอนปัจจุบัน:

1. โหลด OutageCase
2. ถ้า `status = restored` ให้หยุด
3. ถ้าไม่มี ETR target ให้หยุด
4. ถ้ายังไม่ถึง ETR target ให้หยุด
5. sync `affected_ca_numbers`
6. ensure SLA โดยใช้เวลาเริ่มเคสเดิมเท่าที่ระบบมี
7. ส่ง proactive notification `event_type = etr_timeout_sla`
8. ข้อความลูกค้าจะบอกว่าเวลาไฟกลับที่ประเมินไว้เลยกำหนดแล้ว และจะเร่งดำเนินการให้ไม่เกินเวลา SLA target

ข้อจำกัดปัจจุบัน:

- ระบบไม่เปลี่ยน `case_type` เป็น `fast_track` อัตโนมัติเมื่อ ETR timeout
- ระบบไม่เก็บ emergency escalation level แยกต่างหาก
- ระบบแสดง deadline เป็นเวลา `HH:MM น.` ไม่ได้ส่งค่า remaining duration แบบ `[SLA Remaining]`
- Celery ETR timeout ถูก schedule จาก OMS ETR update เป็นหลัก ส่วน model ETR ที่หมดเวลาอาจถูกสะท้อนผ่าน session context หรือ sync report มากกว่า timer แยก

## 11. Field Operation Status ปัจจุบัน

ระบบปัจจุบันรองรับ status ของ OutageCase เท่านี้:

- `reported`
- `investigating`
- `repairing`
- `restored`

Ops Console ปัจจุบันทำ action หลัก:

- ตั้ง ETR
- ตั้ง ETA เป็นเวลาปัจจุบัน
- ปิดเคสโดยตั้ง `status = restored`

ระบบยังไม่มีสถานะเฉพาะ:

- `arrived`
- `internal_fault`
- `unreachable`
- `cancelled`
- `manual_fallback`
- `completed`

ดังนั้น State Cleansing เต็มรูปแบบจะเกิดชัดเจนที่สุดตอนเคสถูกปิดเป็น `restored`

## 12. การปิดเคส / Restoration / Closed-Loop

การปิดเคสในระบบปัจจุบันหมายถึง:

```text
OutageCase.status = restored
```

โดย `restored` มี label ว่า:

```text
จ่ายไฟคืนกระแสสำเร็จ
```

เมื่อ OutageCase เปลี่ยนจากสถานะอื่นมาเป็น `restored`:

1. Signal ตั้ง flag ว่าเคสเพิ่ง restored
2. หลัง save ระบบสร้าง `OutageRestorationLog`
3. ระบบบันทึก snapshot:
   - lv_group_id
   - affected_ca_numbers
   - restored_at
   - ETA ตอนปิดเคส
   - OMS ETR ตอนปิดเคส
   - model ETR ตอนปิดเคส
   - effective ETR
   - etr_source
   - etr_delta_minutes
4. ระบบ revoke `celery_eta_task_id` ถ้ามี
5. ระบบ revoke `celery_etr_task_id` ถ้ามี
6. ระบบ clear task id ทั้งสองจาก OutageCase
7. ระบบหา CustomerReport ที่ผูกกับเคสนี้และยัง `is_resolved = False`
8. ระบบตั้ง `CustomerReport.is_resolved = True`
9. ระบบส่ง proactive alert `event_type = closed_loop_prompt`

ข้อความ closed-loop prompt ปัจจุบัน:

```text
ระบบแจ้งว่าจ่ายไฟคืนแล้วครับ ไฟกลับมาใช้งานได้แล้วหรือยังครับ
```

ความหมายเชิง workflow:

- OutageCase ถูกปิดก่อนด้วย `status = restored`
- CustomerReport ถูก mark resolved ตามหลัง เพื่อบอกว่า session/report นี้จบจากเคสเดิมแล้ว
- หน้า chat แสดง dropdown ให้ลูกค้าเลือก `ไฟมาแล้ว / ใช้งานได้แล้ว` หรือ `ยังไม่มีไฟ / เปิดเคสเร่งด่วน`
- ถ้าลูกค้าเลือกว่ายังไม่มีไฟ จะเปิด flow fast-track ใหม่ผ่าน Agent

## 13. Fast-Track หลังปิดเคสแล้วไฟยังไม่มา

เมื่อ chat history มี `event_type = closed_loop_prompt` และลูกค้าเลือกจาก dropdown หรือพิมพ์ว่าไฟยังไม่มา เช่น:

- ยังไม่มีไฟ
- ไฟยังไม่มา
- ยังใช้งานไม่ได้

Agent จะเรียก `Fast_Track_Tool`

Backend จะเรียก `POST /api/reports/fast-track/`

ขั้นตอนปัจจุบัน:

1. ตรวจ CA format 12 หลัก
2. เลือก report เดิมของ CA/session ถ้ามี
3. เติมพิกัดจาก CustomerLocation
4. ถ้ามี active fast-track case ของ CA นี้อยู่แล้ว:
   - ผูก report เข้ากับเคสนั้น
   - ตั้ง `report.is_resolved = False`
   - คืน `event_type = fast_track_existing`
5. ถ้ายังไม่มี active fast-track case:
   - สร้าง OutageCase ใหม่
   - `case_type = fast_track`
   - `status = reported`
   - title เป็น `[ด่วน! ไฟดับซ้ำซ้อน] CA ...`
   - ใช้พิกัดจาก report ถ้ามี ถ้าไม่มีใช้ fallback lat/lon
   - ตั้ง `sla_reference_time = timezone.now()`
   - ตั้ง `sla_target_time = now + 4 hours`
   - ตั้ง `sla_reason = fast_track`
   - ผูก report เข้ากับเคสใหม่
   - ตั้ง `report.is_resolved = False`
   - sync `affected_ca_numbers`
   - คืน `event_type = fast_track_created`

ข้อจำกัดปัจจุบัน:

- Fast-track ปัจจุบันตั้ง SLA ใหม่จากเวลาที่สร้าง fast-track
- ยังไม่ได้ preserve SLA จากเวลาเปิดเคสแรกตาม target v1.8
- Fast-track ไม่ตั้ง ETA หรือ ETR ใหม่

## 14. Chat History และ Notification Hydration

ระบบปัจจุบันเก็บ chat history ลง `CustomerReport.chat_history`

เมื่อผู้ใช้กลับเข้า session เดิม:

1. Agent เรียก session context จาก backend
2. Backend คืน chat history และ latest outage
3. Agent hydrate memory จาก DB
4. Chat UI render ประวัติสนทนาเดิมหรือแสดง summary ของ active case

เมื่อ Celery หรือ signal ส่ง proactive alert:

1. Backend ยิง webhook ไปที่ DSPy `/webhook/notify`
2. DSPy server เก็บ notification ลง pending queue ตาม session
3. DSPy server เพิ่ม System Alert เข้า agent memory
4. DSPy server sync chat history กลับ backend
5. Chat UI poll `/notifications/{session_id}` ทุก 2 วินาที
6. ถ้ามี notification จะแสดงเป็นข้อความจากระบบในหน้า chat

## 15. API Summary ปัจจุบัน

### Customer / Agent APIs

- `GET /api/reports/validate-ca/`: ตรวจ CA ใน CustomerLocation
- `POST /api/reports/session-login/`: register session หลังเข้า chat
- `POST /api/reports/sync/`: สร้างหรือผูก CustomerReport กับ OutageCase
- `POST /api/reports/chat-history/`: sync chat history
- `GET /api/reports/session-context/<session_id>/`: hydrate context กลับเข้า Agent/UI
- `GET /api/reports/status/`: ตรวจ active case ของ CA
- `POST /api/reports/fast-track/`: เปิดหรือ reuse fast-track case

### Ops APIs

- `GET /ops/cases/`: list cases
- `POST /ops/cases/action/`: set ETR, set ETA now, restore case
- `GET /ops/cases/export/`: export CSV
- `GET /ops/map/data/`: ส่งข้อมูล marker สำหรับแผนที่

### Agent Webhook APIs

- `POST /webhook/notify`: รับ proactive notification
- `GET /notifications/{session_id}`: ให้ Chat UI poll notification

## 16. Anti-Loop / State Cleansing ที่มีจริง

ระบบปัจจุบันมี anti-loop และ cleansing หลัก ๆ ดังนี้:

- Deduplicate active case ตาม CA เดียวกัน
- Reuse active fast-track case ของ CA เดียวกัน
- Revokes ETA task เมื่อ ETA ถูกแก้
- Revokes ETR task เก่าเมื่อ OMS ETR ถูกแก้
- Revokes ETA/ETR task ทั้งหมดเมื่อ OutageCase ถูกปิดเป็น `restored`
- Mark related CustomerReport เป็น resolved หลัง OutageCase restored
- Closed-loop prompt ให้ลูกค้าบอกกลับหากไฟยังไม่มา
- Agent guardrail ไม่ให้ตอบนอกขอบเขตและไม่ให้วนถาม CA/PDPA ใน chat

## 17. สิ่งที่ยังไม่มีในระบบปัจจุบันเมื่อเทียบกับ Target v1.8

รายการนี้ไม่ได้แปลว่าระบบผิด แต่เป็นขอบเขตที่ยังไม่ได้ implement ใน As-Is ปัจจุบัน:

- ยังไม่มี Mass Outage Area Topic / Subscription List
- ยังไม่มี State: Subscribed
- ยังไม่มี persisted State: Manual_Fallback
- ยังไม่มีการ pause Timer_ETA จาก manual fallback
- ยังไม่มี status `Arrived`
- ยังไม่มี logic ระงับ Timer_ETA เมื่อช่างอัปเดตสถานะหน้างานทั่วไป
- ยังไม่มี State: Cancelled สำหรับ internal fault หรือ unreachable
- ยังไม่มี purge subscription เพราะยังไม่มี subscription model
- ยังไม่มี anti-spam threshold สำหรับ ETR update ที่เลื่อนอย่างมีนัยสำคัญ
- ยังไม่มี emergency escalation level เมื่อ SLA Remaining <= 0
- ยังไม่มี Completed state แยกจาก `CustomerReport.is_resolved`
- ยังไม่มี Grace Period timer หลัง closed-loop prompt
- ยังไม่มี auto-close fast-track หลังครบ 4 ชั่วโมงแบบ state machine
- Fast-track ยังใช้ SLA ใหม่จากเวลาสร้าง fast-track ไม่ได้อิงเวลาเปิดเคสแรก

## 18. Current Workflow Summary

```text
Login with CA + PDPA
  -> validate CA
  -> register session
  -> attach active CA case if exists
  -> user chats with Agent
  -> Agent calls Check_Outage_Tool
  -> OMS sync report
      -> existing active CA case: return existing_ca_case
      -> same/effectively same coordinate case: return repeated_event
      -> no related case: call pea-estimated.services and create OutageCase
  -> schedule ETA timer
  -> OMS/Admin may set ETR
      -> notify etr_update
      -> schedule ETR timeout
  -> ETA timeout if not restored
      -> notify eta_timeout
  -> ETR timeout if not restored
      -> notify etr_timeout_sla
  -> OMS/Admin restores case
      -> OutageCase.status = restored
      -> create restoration log
      -> revoke timers
      -> CustomerReport.is_resolved = True
      -> notify closed_loop_prompt
  -> if customer says power still unavailable
      -> Fast_Track_Tool
      -> create/reuse fast_track OutageCase
      -> CustomerReport.is_resolved = False
```
