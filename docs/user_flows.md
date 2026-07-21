# User Flows

เอกสารนี้สรุป user flow ของระบบแจ้งเหตุไฟฟ้าดับจาก implementation ปัจจุบัน โดยโฟกัส 3 ประเภทเคสหลัก:

1. เคสเดี่ยว หรือ single CA
2. เคส CA ซ้ำ
3. เคสรวมกลุ่ม หรือ mass outage จาก OMS เท่านั้น

เป้าหมายของเอกสารนี้คืออธิบายหลักการ, decision logic, state transition, timer, proactive alert และ closed-loop ที่เกี่ยวข้อง เพื่อให้ตรวจสอบ behavior ของระบบได้ครบตั้งแต่ลูกค้า login จนถึงการปิดเคสหรือเปิดเคสใหม่จาก closed-loop

## หลักการกลางของระบบ

### Source of truth

- Django OMS backend เป็น source of truth ของ `CustomerReport` และ `OutageCase`
- Agent ฝั่ง DSPy ไม่ตัดสินใจสร้างหรือผูกเคสเอง ข้อความแรกที่อาจเป็นเหตุไฟดับต้องถามยืนยันก่อน และเรียก `Check_Outage_Tool(...)` ซึ่งส่งข้อมูลไปที่ `POST /api/reports/sync/` ได้เฉพาะ turn ถัดมาที่ลูกค้ายืนยันแล้ว
- OMS หรือระบบภายนอกส่ง event เข้า Django ผ่าน `POST /api/oms/events/`
- `oms_api` เป็น service สำหรับรับคำสั่งจาก OMS/operator แล้ว forward event เข้า Django

### Entity สำคัญ

| Entity | ความหมาย |
| --- | --- |
| `CustomerLocation` | ฐานข้อมูลลูกค้าและพิกัดตาม CA |
| `CustomerReport` | record การแจ้งเหตุหรือ session context ของลูกค้า 1 session ต่อ CA |
| `OutageCase` | เคสไฟดับจริงที่ระบบใช้ติดตามสถานะ, ETA, ETR, SLA และรายการ CA ที่เกี่ยวข้อง |
| `affected_ca_numbers` | snapshot รายการ CA ที่เกี่ยวข้องกับเคส ใช้สำหรับ attach report และแจ้ง proactive |
| `related_case` | field บน `CustomerReport` ที่ชี้ไปยัง `OutageCase` |
| `is_resolved` | ใช้แยก report ที่ยัง active กับ report ที่ปิดแล้ว |

### ประเภทเคส

| `case_type` | ใครสร้างได้ | หลักการ |
| --- | --- | --- |
| `normal` | Chat/Agent หรือ OMS | เคสปกติของ CA เดี่ยว หรือ OMS ส่ง affected CA มาไม่ถึงเกณฑ์กลุ่ม |
| `mass_outage` | OMS เท่านั้น | เคสรวมกลุ่มเมื่อ OMS ส่ง affected CA หลายรายการ |

เงื่อนไขสำคัญ: แชตจากลูกค้าไม่รวมกลุ่มเอง ถึงมีลูกค้าหลาย CA แจ้งเข้ามาใกล้กัน ระบบฝั่ง chat จะสร้างหรือ attach เฉพาะเคสปกติ เว้นแต่ OMS ส่ง mass outage เข้ามา

### สถานะ active และ inactive

| Status | ความหมาย | ถือว่า active หรือไม่ |
| --- | --- | --- |
| `reported` | ได้รับแจ้งเหตุ | active |
| `investigating` | กำลังตรวจสอบ | active |
| `repairing` | กำลังซ่อม | active |
| `restored` | จ่ายไฟคืนแล้ว | inactive |
| `merged` | ถูกรวมเข้าเคสอื่น | inactive |

เคสที่เป็น `restored` หรือ `merged` จะไม่ถูกใช้เป็นปลายทางสำหรับ attach report ใหม่ และ timer ที่ค้างอยู่จะถูก revoke

### Timer และ proactive event

| Timer | ถูกตั้งเมื่อ | เมื่อครบเวลาแล้วทำอะไร |
| --- | --- | --- |
| ETA timer | เคสใหม่จาก chat มี `eta_target_time` | ส่ง `eta_timeout` เพื่ออัปเดตว่าทีมงานกำลังดำเนินการ และแจ้ง ETR ถ้ามี |
| ETR timer | OMS เติมหรือแก้ `oms_etr` | ส่ง `etr_timeout_sla` และแจ้ง deadline การจ่ายไฟตาม `sla_target_time` |
| SLA timer | เคสถูกสร้างหรือมีการตั้ง `sla_target_time` | ส่ง `closed_loop_prompt` พร้อม `closed_loop_kind="sla_expired"` เพื่อถามว่าไฟกลับมาใช้งานได้หรือยัง |

ข้อความ SLA timeout ที่ส่งให้ลูกค้า:

```text
ขออภัยที่การดำเนินการเกินเวลาที่แจ้งไว้ค่ะ ไฟฟ้ากลับมาใช้งานได้หรือยังคะ
```

ภายในระบบใช้ชื่อ field ว่า SLA แต่ข้อความที่ยิงหาลูกค้าไม่ควรพูดคำว่า SLA, ETA หรือ ETR โดยตรง

### Closed-loop หลัก

Closed-loop ใช้ event เดียวกันคือ `event_type="closed_loop_prompt"` แต่แยกที่มาด้วย metadata ได้ เช่น `closed_loop_kind="sla_expired"`

| ที่มาของ prompt | Trigger | ข้อความ | เมื่อผู้ใช้ตอบว่าไฟมาแล้ว | เมื่อผู้ใช้ตอบว่ายังไม่มีไฟ |
| --- | --- | --- | --- | --- |
| OMS close/restored | OMS หรือ admin เปลี่ยนเคสเป็น `restored` | ระบบแจ้งว่าจ่ายไฟคืนแล้ว และให้เลือกสถานะไฟฟ้า | mark report เดิมเป็น resolved | mark report เดิมเป็น resolved แล้วเรียก `Check_Outage_Tool(..., pdpa_consent=True, force_new_case=True)` |
| SLA expired | `sla_target_time` ถึงหรือเกินเวลาปัจจุบัน และเคสยัง active | ขออภัยที่เกินเวลาที่แจ้งไว้ และถามว่าไฟกลับมาใช้งานได้หรือยัง | mark report เดิมเป็น resolved | mark report เดิมเป็น resolved แล้วเปิดเคสใหม่แบบ normal |

หลักการของ still-out หลัง closed-loop: ต้องเปิดเคสใหม่เสมอ แม้ CA เดิมยังอยู่ใน `affected_ca_numbers` ของเคส active เดิม เพราะคำตอบของลูกค้าถือเป็น re-report หลังระบบถามยืนยันแล้ว

## Flow 1: เคสเดี่ยว (Single CA)

### หลักการ

เคสเดี่ยวคือกรณีที่ลูกค้าหนึ่งรายหรือหนึ่ง CA แจ้งไฟดับผ่านแชต และระบบยังไม่พบ active case ที่ครอบคลุม CA นี้ ระบบจะสร้าง `OutageCase` ใหม่ชนิด `normal`

### Entry points

- ลูกค้า login ด้วย CA ผ่านหน้า chat
- Agent ถามยืนยันว่าไฟดับอยู่และต้องการให้การไฟฟ้าตรวจสอบ
- เมื่อลูกค้าตอบยืนยันใน turn ถัดไป Agent จึงเรียก `Check_Outage_Tool(ca_number, pdpa_consent=True)`
- Tool ส่งข้อมูลไปที่ `POST /api/reports/sync/`

### Flow หลัก

1. ลูกค้า login ด้วย CA
2. ระบบ validate CA กับ `CustomerLocation`
3. ถ้า CA ถูกต้อง ระบบสร้างหรือ reuse `CustomerReport` ที่ยังไม่ resolved สำหรับ `session_id + ca_number`
4. ระบบบันทึก customer location, PDPA consent และ session context ลง report
5. เมื่อลูกค้าแจ้งหรืออาจกำลังถามถึงไฟดับ Agent ถามยืนยันก่อนโดยไม่เรียก outage API
6. เมื่อลูกค้าตอบยืนยันในข้อความถัดไป Agent เรียก `Check_Outage_Tool`; ตัว tool ปฏิเสธการทำงานหากไม่มี confirmation state เพื่อกันการเปิดใบงานจาก routing ที่ผิด
7. `/api/reports/sync/` ตรวจว่า report นี้ยังผูกกับ active case หรือไม่
8. ถ้าไม่พบ active case สำหรับ CA นี้ ระบบเรียก assessment เพื่อหา ETA, branch, และข้อมูล ETR จาก model/บริการประเมิน
9. ถ้า assessment สำเร็จ ระบบสร้าง `OutageCase` ใหม่:
   - `case_type="normal"`
   - `status="reported"`
   - `affected_ca_numbers=[ca_number]`
   - พิกัดจาก `CustomerLocation`
10. ระบบผูก `CustomerReport.related_case` เข้ากับเคสใหม่
11. ระบบ attach waiting reports ของ CA เดียวกันที่ยังไม่มีเคสเข้ากับเคสนี้
12. ระบบ sync `affected_ca_numbers`
13. ระบบตั้ง ETA timer และ SLA timer
14. API ตอบ `event_type="new_event"` กลับให้ Agent
15. Agent แจ้งลูกค้าว่าเปิดใบงานแล้ว พร้อม branch/ETA/ETR เท่าที่มี

### Decision table

| Decision | เงื่อนไข | ผลลัพธ์ |
| --- | --- | --- |
| CA format ไม่ถูกต้อง | Agent ไม่มี CA จาก login หรือ CA ไม่ผ่าน format | Tool ตอบ `[CA_INVALID]` และให้ลูกค้ากลับไป login ใหม่ |
| ยังไม่มี PDPA consent | ไม่มี consent จาก login หรือ tool call | Tool ตอบ `[CONSENT_REQUIRED]` |
| ไม่พบ CA ในฐานลูกค้า | `/api/reports/sync/` หา `CustomerLocation` ไม่เจอ | API ตอบ `event_type="ca_not_found"` |
| พบ active case สำหรับ CA นี้ | report หรือ CA มีเคส active อยู่แล้ว | ไป Flow 2: เคส CA ซ้ำ |
| ไม่พบ active case | CA นี้ยังไม่มีเคส active | สร้างเคสใหม่ชนิด `normal` |
| assessment error | ระบบประเมิน ETA/ETR ล้มเหลว | API ตอบ `event_type="assessment_error"` และ Agent fallback ไปหาเจ้าหน้าที่ |
| effective ETR เกินแล้ว | เคส active และเวลาปัจจุบันเกิน ETR แล้ว | API ตอบ `event_type="etr_timeout_sla"` |

### Timer ของเคสเดี่ยว

| เวลา | Behavior |
| --- | --- |
| ถึง `eta_target_time` | ส่ง `eta_timeout` ให้ active session ของเคสนั้น ถ้ามี OMS ETR หรือ Pluem ETR จะบอกเวลาไฟกลับโดยประมาณ |
| OMS เติมหรือแก้ `oms_etr` | revoke ETR task เดิม, ตั้ง ETR task ใหม่, ส่ง `etr_update` ให้ active session |
| ถึง OMS ETR ที่ถูกตั้งเป็น timer | ส่ง `etr_timeout_sla` และแจ้งเวลาจ่ายไฟไม่เกิน `sla_target_time` |
| ลูกค้า sync/report แล้วระบบพบว่า effective ETR ผ่านไปแล้ว | API ตอบ `event_type="etr_timeout_sla"` เพื่อให้ Agent แจ้ง deadline การจ่ายไฟ |
| ถึง `sla_target_time` | ส่ง `closed_loop_prompt` แบบ `sla_expired` เพื่อถามว่าไฟฟ้ากลับมาใช้งานได้หรือยัง |

### Closed-loop ของเคสเดี่ยว

- ถ้า OMS ปิดเคสเป็น `restored`: ระบบ mark report ที่เกี่ยวข้องเป็น resolved และส่ง closed-loop prompt เพื่อให้ลูกค้ายืนยัน
- ถ้า SLA หมดแต่เคสยัง active: ระบบส่ง closed-loop prompt โดยยังไม่เปลี่ยน status เคสเดิมทันที
- ถ้าลูกค้าตอบว่าไฟมาแล้ว: ระบบ record closed-loop response และ report เดิมเป็น resolved
- ถ้าลูกค้าตอบว่ายังไม่มีไฟ: Agent เรียก `Check_Outage_Tool(..., force_new_case=True)` เพื่อเปิดเคส normal ใหม่

### Edge cases

| กรณี | Behavior |
| --- | --- |
| Login ซ้ำ session เดิม CA เดิม | reuse unresolved `CustomerReport` เดิม |
| Login ด้วย CA ที่มีเคส active อยู่แล้ว | report ถูก attach เข้าเคสนั้น และ session context มี latest outage |
| ลูกค้าถามสถานะหลังเปิดเคส | Agent ใช้ latest outage context จาก history/session |
| เคสถูก restored ก่อน ETA/ETR/SLA | timer ทั้งหมดถูก revoke และไม่ส่ง proactive timeout ต่อ |
| เคสถูก merged เข้า mass outage | เคสเดิมเป็น inactive, timer ถูก revoke, report ถูกผูกไปยัง anchor mass outage |

## Flow 2: เคส CA ซ้ำ

### หลักการ

เคส CA ซ้ำคือกรณีที่มีการแจ้งเหตุด้วย CA เดิมในขณะที่ระบบมี active context อยู่แล้ว ระบบจะไม่สร้างเคสใหม่โดยอัตโนมัติ แต่จะพยายาม attach report ใหม่เข้ากับ active case เดิม เพื่อไม่ให้เกิด duplicate case

ข้อยกเว้นสำคัญคือ closed-loop still-out: ถ้าระบบเพิ่งถามยืนยันแล้วลูกค้าตอบว่ายังไม่มีไฟ ต้องเปิดเคสใหม่เสมอด้วย `force_new_case=True`

### ลำดับการตัดสินใจเมื่อมี report เข้ามา

เมื่อ `/api/reports/sync/` รับ report โดยไม่ได้ส่ง `force_new_case=True` ระบบจะเลือกเคสตามลำดับนี้:

1. ถ้า report ปัจจุบันมี `related_case` ที่ยัง active อยู่แล้ว ให้ใช้เคสนั้น
2. ถ้ามี unresolved report อื่นของ CA เดียวกันที่ผูก active case อยู่ ให้ attach เข้าเคสนั้น
3. ถ้าไม่มี report เดิม แต่มี active `OutageCase` ที่ `affected_ca_numbers` มี CA นี้ ให้ attach เข้าเคสนั้น
4. ถ้ายังไม่พบเคส active จึงสร้างเคสใหม่ชนิด `normal`

ระบบจะไม่ attach เข้าเคสที่ status เป็น `restored` หรือ `merged`

### Sub-flow A: ลูกค้าเดิม session เดิม แจ้งซ้ำ

1. ลูกค้าคนเดิม login หรือแชตต่อด้วย `session_id + ca_number` เดิม
2. ระบบ reuse unresolved `CustomerReport` เดิม
3. ถ้า report ยังผูก active case อยู่ ระบบตอบเป็นเคสเดิม
4. Agent แจ้งข้อมูลล่าสุดของเคส เช่น branch, ETA หรือ ETR

ผลลัพธ์ที่คาดหวัง:

- ไม่สร้าง `CustomerReport` ใหม่ถ้ายังมี unresolved report เดิม
- ไม่สร้าง `OutageCase` ใหม่
- API ตอบ `event_type="existing_ca_case"` หรือ `event_type="mass_outage"` ถ้าเคสเดิมเป็นเหตุวงกว้าง

### Sub-flow B: ลูกค้าคนละ session แต่ CA เดิม แจ้งซ้ำ

1. session ใหม่ login ด้วย CA เดิม
2. ระบบสร้างหรือ reuse `CustomerReport` ของ session นั้น
3. ระบบหา unresolved report อื่นของ CA เดียวกันที่มี active case
4. ถ้าพบ ระบบ attach report ใหม่เข้ากับ case เดิม
5. Agent ตอบว่าเป็นเคสเดิมของ CA หรือเหตุวงกว้างตาม `case_type`

ผลลัพธ์ที่คาดหวัง:

- มีหลาย `CustomerReport` ได้ เพราะหลาย session แจ้ง CA เดียวกัน
- ทุก report ของ CA เดียวกันจะชี้ไปยัง active case เดียวกัน
- proactive alert จะ dedupe ต่อ session เพื่อไม่ส่งซ้ำใน session เดียวกัน

### Sub-flow C: มี waiting report ของ CA เดียวกัน

บางกรณีอาจมี unresolved report ของ CA เดียวกันที่ยังไม่มี `related_case` เช่น report ถูกสร้างจาก login แต่ยังไม่ได้เปิดเคสสำเร็จ

เมื่อมี report หนึ่งสร้างเคสใหม่สำเร็จ ระบบจะเรียก `_attach_waiting_same_ca_reports(...)` เพื่อ attach waiting reports เหล่านั้นเข้าเคสเดียวกัน

ผลลัพธ์ที่คาดหวัง:

- CA เดียวกันที่ยังรอเคสอยู่จะถูกรวมเข้ากับเคสปกติเดียวกัน
- `affected_ca_numbers` ถูก sync ให้มี CA นั้นใน snapshot

### Sub-flow D: Closed-loop still-out หลัง OMS close หรือ SLA expired

1. ระบบส่ง `closed_loop_prompt`
2. ลูกค้าตอบว่า `ยังไม่มีไฟ`, `ไฟยังไม่มา`, `ยังดับ` หรือคำในกลุ่ม still-out
3. Agent บันทึก closed-loop response ผ่าน `/api/reports/closed-loop-response/`
4. ระบบ mark report เดิมใน session/CA นั้นเป็น resolved
5. Agent เรียก `Check_Outage_Tool(ca_number, pdpa_consent=True, force_new_case=True)`
6. `/api/reports/sync/` ข้าม logic attach เข้า active case เดิม
7. ระบบสร้าง `CustomerReport` active ใหม่ และสร้าง `OutageCase` normal ใหม่

ผลลัพธ์ที่คาดหวัง:

- เคสใหม่ถูกเปิดแม้ CA เดิมยังอยู่ใน `affected_ca_numbers` ของเคสเก่า
- report เดิมถูก resolved เพื่อปิดรอบ closed-loop เดิม
- เคสใหม่เป็นการแจ้งเหตุใหม่ตามคำยืนยันของลูกค้า

### Decision table สำหรับ CA ซ้ำ

| สถานการณ์ | `force_new_case` | เคสเดิม active หรือไม่ | ผลลัพธ์ |
| --- | --- | --- | --- |
| แจ้งซ้ำระหว่างเคสยัง active | false | active | attach เข้าเคสเดิม |
| แจ้งซ้ำหลังเคส `restored` | false | inactive | สร้างเคสใหม่ |
| แจ้งซ้ำหลังเคส `merged` | false | inactive | attach ได้เฉพาะ anchor mass outage ถ้า CA อยู่ใน active `affected_ca_numbers` |
| still-out หลัง closed-loop | true | จะ active หรือ inactive ก็ได้ | เปิดเคสใหม่เสมอ |
| CA อยู่ใน active mass outage | false | active mass outage | attach เข้า mass outage และตอบ `mass_outage` |
| assessment error ตอนต้องเปิดเคสใหม่ | true หรือ false | ไม่มีเคสที่จะ attach | ไม่สร้างเคส และ fallback |

### Event type ที่ Agent เห็นใน Flow CA ซ้ำ

| `event_type` | ความหมาย | ข้อความแนวทาง |
| --- | --- | --- |
| `existing_ca_case` | CA นี้มี normal case active อยู่แล้ว | แจ้งว่าเป็นเคสเดิม พร้อม ETA/ETR เท่าที่มี |
| `mass_outage` | CA นี้อยู่ใน active mass outage | แจ้งว่าเป็นเหตุไฟดับวงกว้าง |
| `etr_timeout_sla` | พบเคสเดิมแต่เกิน effective ETR แล้ว | แจ้ง deadline การจ่ายไฟ |
| `new_event` | ไม่มีเคส active หรือถูก force new | เปิดใบงานใหม่ |

## Flow 3: เคสรวมกลุ่มจาก OMS เท่านั้น

### หลักการ

เคสรวมกลุ่มคือ `OutageCase.case_type="mass_outage"` และต้องมาจาก OMS เท่านั้น ระบบ chat ไม่รวม CA เอง

เงื่อนไขปัจจุบัน:

- ถ้า OMS ส่ง `affected_ca_numbers` ตั้งแต่ 2 CA ขึ้นไป ระบบถือเป็น `mass_outage`
- ถ้า OMS ส่ง CA เดียว ระบบถือเป็น `normal`
- ถ้า OMS ไม่ส่ง affected CA แต่ส่ง `case_type` ที่ถูกต้อง ระบบใช้ `case_type` นั้น
- ถ้า OMS ไม่ส่งทั้ง `case_id` และ `external_event_id` ระบบ reject event

### Entry points

- OMS/operator เปิดเคสผ่าน service `oms_api`
- OMS sync outage ผ่าน `/api/v1/oms/outage/sync`
- `oms_api` ส่ง callback เข้า Django ที่ `POST /api/oms/events/`
- Django upsert case ผ่าน `_upsert_oms_case(...)`
- ถ้า Django merge case id ที่ OMS สร้างเข้ากับ active anchor, response จะคืน `canonical_case_id`; หน้า console ต้องใช้ id นี้สำหรับ Update ETR และ Close ครั้งต่อไป

### Flow เปิด mass outage

1. OMS ส่ง event `case_opened` พร้อม case payload
2. Django validate ว่ามี `case_id` หรือ `external_event_id`
3. Django หาเคสเดิมด้วย `external_event_id` ก่อน แล้วค่อยหา `case_id`
4. ถ้าไม่พบเคสเดิม ระบบสร้าง `OutageCase` ใหม่
5. ระบบคำนวณ `case_type` จากจำนวน `affected_ca_numbers`
6. ถ้าเป็น mass outage ระบบหาเคส normal active ที่มี CA อยู่ใน affected list
7. เคส normal เหล่านั้นถูก mark เป็น `merged`
8. ระบบตั้ง `merged_into` ไปยัง anchor mass outage และ `merged_at`
9. ระบบ revoke ETA/ETR/SLA timer ของเคสที่ถูก merged
10. ระบบ attach unresolved reports ที่ CA อยู่ใน affected list เข้า mass outage
11. ระบบ sync `affected_ca_numbers`
12. ถ้า OMS มี ETR ระบบตั้ง ETR timer
13. ระบบตั้ง SLA timer
14. ระบบส่ง proactive `mass_outage` ให้ session ที่ active และเกี่ยวข้อง โดย dedupe ต่อ session

### Flow upsert หรือเปิด event ซ้ำจาก OMS

OMS อาจส่ง event เดิมซ้ำได้ ระบบจึงใช้ upsert แทน create-only

| Decision | Logic |
| --- | --- |
| มี `external_event_id` | ใช้หาเคสเดิมก่อน `case_id` |
| ไม่มี `external_event_id` แต่มี `case_id` | ใช้ `case_id` หาเคสเดิม |
| พบเคสเดิม | update status, affected CA, outage time, ETR ตาม payload |
| ไม่พบเคสเดิม | create case ใหม่ |
| event เป็น `case_opened` | run merge logic และแจ้ง proactive ถ้าเป็น mass outage |
| event ไม่ใช่ `case_opened` | ไม่ run merge logic แต่ยัง attach reports และ schedule timer ตามสถานะ |

### Flow ลูกค้าแจ้งหลังมี mass outage อยู่แล้ว

1. ลูกค้า login หรือแจ้งไฟดับด้วย CA ที่อยู่ใน `affected_ca_numbers` ของ mass outage active
2. `/api/reports/sync/` หา active case จาก `affected_ca_numbers`
3. report ถูก attach เข้า mass outage
4. API ตอบ `event_type="mass_outage"`
5. Agent แจ้งว่าเป็นเหตุไฟดับวงกว้าง และบอก ETR ถ้ามี

ผลลัพธ์ที่คาดหวัง:

- ไม่สร้าง normal case ซ้ำสำหรับ CA ที่ OMS ระบุว่าอยู่ในเหตุวงกว้าง
- report ใหม่ถูกผูกเข้ากับ anchor mass outage
- session ใหม่จะได้รับ proactive update รอบต่อไปของเคสนั้น

### Flow OMS update ETR

1. OMS ส่ง event update หรือ patch ที่มี `oms_etr`
2. Django update `OutageCase.oms_etr`
3. signal ตรวจว่า OMS ETR เปลี่ยน
4. ระบบ revoke ETR task เดิมถ้ามี
5. ถ้าเคสยัง active ระบบตั้ง ETR task ใหม่ที่เวลา `oms_etr`
6. ระบบส่ง proactive `etr_update` ไปยัง unresolved reports ของเคสนั้น โดย dedupe ต่อ session

ถ้าเคสเป็น `restored` หรือ `merged` แล้ว จะไม่ตั้ง timer ใหม่

สำหรับหน้า console ที่พอร์ต `8002` หาก callback ชี้ว่า case id ถูก merge ระบบจะเปลี่ยนช่อง Case ID ไปเป็น canonical anchor อัตโนมัติหลัง Open ส่วน Update/Close ที่ยิงด้วย merged child เก่าจะถูกหยุดไว้และให้ผู้ใช้ตรวจสอบ canonical id ก่อนกดคำสั่งอีกครั้ง

### Flow OMS close/restored mass outage

1. OMS ส่ง `case_closed` หรือ update status เป็น `restored`
2. Django update case ด้วย `trigger_signals=True`
3. signal สร้าง `OutageRestorationLog`
4. ระบบ revoke ETA/ETR/SLA timer ของ anchor case
5. ระบบหา recipient reports จาก:
   - reports ที่ผูกกับ anchor mass outage
   - reports ของเคสลูกที่เคยถูก merged เข้า anchor
6. ระบบ mark reports เหล่านั้นเป็น resolved
7. ระบบส่ง `closed_loop_prompt` ให้แต่ละ session เพื่อถามยืนยันไฟกลับ

ถ้าลูกค้าตอบว่ายังไม่มีไฟหลัง mass outage ถูกปิด ระบบจะเปิดเคส normal ใหม่ผ่าน `force_new_case=True` ไม่เปิด mass outage เอง

### Decision table สำหรับ OMS group case

| สถานการณ์ | ผลลัพธ์ |
| --- | --- |
| OMS ส่ง affected CA 2 รายการขึ้นไป | สร้างหรือ update `mass_outage` |
| OMS ส่ง affected CA 1 รายการ | สร้างหรือ update `normal` |
| OMS ส่ง mass outage ที่ครอบคลุม normal active เดิม | normal เดิมถูก `merged` เข้า mass outage |
| OMS ส่ง event ซ้ำ | update เคสเดิม ไม่สร้างซ้ำ |
| OMS ปิด mass outage | mark restored, revoke timers, ส่ง closed-loop |
| ลูกค้าตอบ still-out หลังปิด mass outage | เปิด normal case ใหม่ |

## Cross-flow Decision Reference

### Case selection เมื่อ Agent sync report

```text
รับ session_id + ca_number
  -> validate CA ใน CustomerLocation
  -> ถ้า force_new_case=True
       -> mark closed-loop reports เดิมเป็น resolved
       -> ข้ามการ attach เข้าเคสเดิม
       -> สร้างเคส normal ใหม่เมื่อ assessment สำเร็จ
  -> ถ้า force_new_case=False
       -> ใช้ related_case เดิมถ้ายัง active
       -> ไม่เจอให้หา unresolved report CA เดียวกันที่มี active case
       -> ไม่เจอให้หา active case ที่ affected_ca_numbers มี CA นี้
       -> ไม่เจอจึงสร้าง normal case ใหม่
```

### Closed-loop response handling

```text
มี pending closed_loop_prompt ล่าสุดใน chat history
  -> ลูกค้าตอบกลุ่มไฟมาแล้ว
       -> record_closed_loop_response("resolved")
       -> mark report เดิมเป็น resolved
       -> จบ flow
  -> ลูกค้าตอบกลุ่มยังไม่มีไฟ
       -> record_closed_loop_response("still_out")
       -> mark report เดิมเป็น resolved
       -> Check_Outage_Tool(..., force_new_case=True)
       -> เปิด normal case ใหม่
  -> ลูกค้าตอบอย่างอื่น
       -> ให้ Agent สนทนาต่อหรือถาม clarification ตาม prompt logic
```

### Event type reference

| `event_type` | ผู้สร้าง | ความหมาย |
| --- | --- | --- |
| `new_event` | `/api/reports/sync/` | เปิดเคสใหม่สำเร็จจาก chat |
| `existing_ca_case` | `/api/reports/sync/` | CA นี้มี normal case active อยู่แล้ว |
| `mass_outage` | `/api/reports/sync/`, OMS callback | CA นี้อยู่ใน mass outage |
| `ca_not_found` | `/api/reports/sync/` | ไม่พบ CA ใน `CustomerLocation` |
| `assessment_error` | `/api/reports/sync/` | assessment ล้มเหลว จึงไม่ควรเปิดเคสอัตโนมัติ |
| `eta_timeout` | Celery task | ถึงเวลา ETA แล้ว แจ้งความคืบหน้า |
| `etr_update` | signal หลัง OMS ETR เปลี่ยน | OMS ปรับเวลาไฟกลับ |
| `etr_timeout_sla` | Celery task หรือ sync response | เกิน ETR แล้ว แจ้ง deadline ตาม SLA target |
| `closed_loop_prompt` | signal หรือ SLA task | ถามยืนยันว่าไฟกลับหรือยัง |

### Status transition สำคัญ

```text
normal reported/investigating/repairing
  -> restored
       -> revoke timers
       -> mark reports resolved
       -> send closed_loop_prompt

normal reported/investigating/repairing
  -> merged
       -> set merged_into
       -> revoke timers
       -> inactive, ไม่ใช้รับ report ใหม่

mass_outage reported/investigating/repairing
  -> restored
       -> revoke timers
       -> close reports ทั้ง anchor และ merged children
       -> send closed_loop_prompt
```

### หลักการ dedupe proactive

- Proactive alert จาก timer หรือ OMS update ส่งให้ unresolved reports ที่เกี่ยวข้อง
- ระบบ dedupe ด้วย `session_id` เพื่อไม่ยิงข้อความซ้ำหลายครั้งใน session เดียวกัน
- หาก session เดียวมีหลาย report ในเคสเดียวกัน ระบบเลือกส่งครั้งเดียว

### กรณีที่ไม่ควรสร้างเคสใหม่

- CA เดิมแจ้งซ้ำระหว่าง normal case ยัง active และไม่ใช่ closed-loop still-out
- CA อยู่ใน active mass outage จาก OMS
- assessment error ทำให้ข้อมูลประเมินไม่พอสำหรับเปิดเคสอัตโนมัติ
- ไม่มี PDPA consent หรือ CA format ไม่ถูกต้อง

### กรณีที่ต้องสร้างเคสใหม่

- CA ไม่มี active case ใด ๆ และ assessment สำเร็จ
- เคสเดิมเป็น `restored` แล้ว และลูกค้าแจ้งเหตุใหม่ตามปกติ
- ลูกค้าตอบ still-out หลัง `closed_loop_prompt` ไม่ว่าต้นทาง prompt จะมาจาก OMS close หรือ SLA expired
- ลูกค้าตอบ still-out หลัง mass outage ปิดแล้ว โดยระบบจะสร้างเคสใหม่เป็น `normal`

## End-to-end Examples

### Example 1: Single CA ครั้งแรก

```text
ลูกค้า CA A login
  -> แจ้งไฟดับ
  -> Agent ถามยืนยันโดยยังไม่เปิดใบงาน
  -> ลูกค้าตอบยืนยันใน turn ถัดไป
  -> ไม่พบ active case
  -> assessment สำเร็จ
  -> create normal case N1
  -> response new_event
  -> schedule ETA + SLA
```

### Example 2: CA เดิมแจ้งซ้ำก่อนเคสปิด

```text
ลูกค้า CA A แจ้งซ้ำ
  -> พบ active case N1 จาก report เดิมหรือ affected_ca_numbers
  -> attach เข้า N1
  -> response existing_ca_case
  -> ไม่สร้างเคสใหม่
```

### Example 3: SLA หมดแล้วยังไม่มีไฟ

```text
case N1 ถึง sla_target_time และยัง active
  -> send closed_loop_prompt kind=sla_expired
  -> ลูกค้าตอบ ยังไม่มีไฟ
  -> mark report เดิม resolved
  -> sync report ด้วย force_new_case=True
  -> create normal case N2
```

### Example 4: OMS เปิด mass outage ทับ normal cases

```text
มี normal case N1 ของ CA A และ N2 ของ CA B
  -> OMS ส่ง case_opened affected_ca_numbers=[A, B, C]
  -> create mass outage M1
  -> merge N1 และ N2 เข้า M1
  -> attach unresolved reports ของ A/B/C เข้า M1
  -> notify active sessions แบบ mass_outage
```

### Example 5: OMS ปิด mass outage แล้วลูกค้ายังไม่มีไฟ

```text
OMS close M1 เป็น restored
  -> revoke timers
  -> mark reports ของ M1 และ merged children resolved
  -> send closed_loop_prompt
  -> ลูกค้า CA A ตอบ ยังไม่มีไฟ
  -> force_new_case=True
  -> create normal case N3
```

## Acceptance Checklist

- Chat-created case เป็น `normal` เสมอ
- ข้อความแรกที่แจ้งหรืออาจหมายถึงไฟดับต้องถามยืนยัน และยังไม่สร้าง/ผูกเคส
- `Check_Outage_Tool` เปิดทางให้ sync ได้หลัง confirmation state หรือ closed-loop prompt เท่านั้น
- Mass/group case สร้างจาก OMS เท่านั้น
- CA ซ้ำ attach เข้า active case เดิมถ้าไม่ใช่ closed-loop still-out
- `restored` และ `merged` ไม่ถูกใช้เป็น active case สำหรับ report ใหม่
- OMS mass outage สามารถ merge normal active cases ที่ถูกครอบคลุมได้
- Timer ของเคสที่ restored/merged ถูก revoke
- SLA expired ส่ง `closed_loop_prompt` แบบ `closed_loop_kind="sla_expired"`
- Still-out หลัง closed-loop เปิดเคสใหม่ด้วย `force_new_case=True`
- Proactive alert dedupe ต่อ session
