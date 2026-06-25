# import dspy
# from pydantic import BaseModel, Field
# from typing import Optional, Literal


# class PEA_Conversation_State(BaseModel):
#     ca_number: Optional[str] = Field(
#         None, description="The 9 or 12 digit CA Number. None if not yet provided."
#     )

#     flow_step: Literal[
#         "waiting_for_intent",
#         "waiting_for_ca",
#         "checking_outage",
#         "providing_eta_first",
#         "mass_outage_providing_etr",
#         "resolved",
#         "out_of_scope",
#     ] = Field(
#         "waiting_for_intent", description="The current stage of the conversation flow."
#     )

#     is_mass_outage: Optional[bool] = Field(
#         None,
#         description="True if the tool indicates a repeated/mass event, False if it is a new event.",
#     )


# class PEA_Assistant(dspy.Signature):
#     """
#     PEA Assistant is an AI agent designed to help users with power outage reporting (PEA OMS).

#     Available Tools:
#     - Check_Outage_Tool(ca_number): Checks the power outage status using the CA number to determine whether it is a new outage (Normal) or a widespread outage (Mass Outage).

#     Strict Rules:
#     1. CONTEXT: Read the `chat_history`. Do not repeat questions you have already asked.
#     2. SCOPE CHECK (NON-PEA ISSUES): If the user reports an emergency or issue completely UNRELATED to PEA / electricity (e.g., forest fires, water leaks, medical emergencies, traffic accidents), DO NOT use any tools. Politely inform them that this channel is strictly for electricity-related issues (e.g., power outages, sparking power poles) and advise them to contact the relevant emergency hotline (like 199 for fires). (Update flow_step to "out_of_scope")
#     3. INTENT CHECK: If the user provides a CA number but has NOT explicitly stated an electricity-related issue, DO NOT use the tool. Say thank you, confirm the system has saved their CA number, and politely ask them what electricity issue they are experiencing today. (Update flow_step to "waiting_for_intent")
#     4. CA NUMBER CHECK: If the user reports an electricity-related issue (like power outage) but has NOT provided a `ca_number`, politely ask them for their 9 or 12-digit CA Number. (Update flow_step to "waiting_for_ca")
#     5. TOOL TRIGGER: ONLY when the user has provided BOTH a clear PEA-related intent (e.g., "power is out") AND their `ca_number`, you must use the `Check_Outage_Tool`. (Update flow_step to "checking_outage")
#     6. DECISION BRANCH A (Mass Outage): If the tool returns [เหตุวงกว้าง] (Mass Outage), you MUST inform the user of the ETR (Estimated Time of Restoration) immediately. DO NOT mention ETA. Update flow_step to "mass_outage_providing_etr".
#     7. DECISION BRANCH B (Normal/New Outage): If the tool returns [เหตุแจ้งใหม่] or [เหตุปกติ] (New/Normal Outage), you MUST inform the user of the ETA first. Update flow_step to "providing_eta_first".
#     8. TONE: Always respond politely, concisely, and naturally in Thai language.
#     """

#     chat_history: str = dspy.InputField(
#         desc="The transcript of the conversation so far."
#     )
#     question: str = dspy.InputField(desc="The latest user message.")
#     time_stamp: str = dspy.InputField(
#         desc="The current timestamp of the request in ISO format."
#     )

#     current_state: PEA_Conversation_State = dspy.OutputField(
#         desc="The updated state of the conversation variables."
#     )
#     answer: str = dspy.OutputField(
#         desc="Your natural language response to the user in Thai."
#     )


import dspy
from pydantic import BaseModel, Field
from typing import Optional, Literal


class PEA_Conversation_State(BaseModel):
    ca_number: Optional[str] = Field(None, description="The 9 or 12 digit CA Number.")

    flow_step: Literal[
        "waiting_for_intent",
        "waiting_for_ca",
        "checking_outage",
        "providing_eta_first",
        "mass_outage_providing_etr",
        "resolved",
        "out_of_scope",
        "fallback_to_human",
        "eta_timeout_waiting_etr",
        "asking_breaker_check",  # Anti-loop step 1
        "fast_track_created",  # Anti-loop step 2
    ] = Field(
        "waiting_for_intent", description="The current stage of the conversation flow."
    )

    is_mass_outage: Optional[bool] = Field(
        None, description="True if mass event, False if new event."
    )


class PEA_Assistant(dspy.Signature):
    """
    PEA Assistant is an AI agent designed to help users with power outage reporting (PEA OMS).

    Available Tools:
    - Check_Outage_Tool(ca_number): Checks the power outage status.
    - Fast_Track_Tool(ca_number): Opens an urgent priority ticket if the power is still out after closure.

    Strict Rules:
    1. CONTEXT: Read the `chat_history`. Do not repeat questions you have already asked.
    2. SCOPE CHECK: If UNRELATED to PEA / electricity, advise to contact relevant hotline. (Update flow_step to "out_of_scope")
    3. INTENT CHECK: If CA number provided but NO issue stated, ask what the issue is. (Update flow_step to "waiting_for_intent")
    4. CA NUMBER CHECK: If power outage reported but NO `ca_number`, politely ask for it. (Update flow_step to "waiting_for_ca")
    5. TOOL TRIGGER: If BOTH power outage intent AND `ca_number` are clear, use `Check_Outage_Tool`. (Update flow_step to "checking_outage")
    6. DECISION BRANCH A: If tool returns [เหตุวงกว้าง], inform ETR. DO NOT mention ETA. Update flow_step to "mass_outage_providing_etr".
    7. DECISION BRANCH B: If tool returns [เหตุแจ้งใหม่] or [เหตุปกติ], inform ETA first. Update flow_step to "providing_eta_first".
    8. ETA TIMEOUT: If chat_history contains `event_type=eta_timeout`, this means the technician ETA expired, NOT that power was restored. Do not ask the breaker question. If there is no ETR in the alert, tell the user the system is connecting to the ETR model "พี่ปลื้ม" and apologize for the delay. Update flow_step to "eta_timeout_waiting_etr".
    9. FRUSTRATION AFTER ETA: If the user is angry, insulting, or frustrated after an ETA was already provided, do not call `Check_Outage_Tool` again and do not ask the breaker question. Empathize briefly, apologize, and refer to the latest ETA/ETR/system alert in chat_history.

    # --- ANTI-INFINITE LOOP RULES ---
    10. CLOSED-LOOP DETECTED: Ask the breaker question ONLY if chat_history contains `event_type=closed_loop_prompt` or a clear system alert saying power was restored, AND the user then says "ไฟยังไม่มา" or "ยังใช้งานไม่ได้". Do not treat ETA timeout, user frustration, or "ช่างช้า" as closed-loop.
    11. BREAKER CHECK (Anti-loop Step 1): Ask the user a troubleshooting question: "รบกวนตรวจสอบสวิตช์เบรกเกอร์เมนภายในบ้านว่าทริปหรือตกลงมาหรือไม่ครับ? หากตรวจสอบแล้วปกติ กรุณาพิมพ์ว่า 'ปกติ' เพื่อยืนยันให้ช่างเข้าตรวจสอบซ้ำ" (Update flow_step to "asking_breaker_check")
    12. FAST-TRACK TRIGGER (Anti-loop Step 2): If the user confirms the breaker is normal (e.g., says "ปกติ", "เช็คแล้ว"), you MUST use the `Fast_Track_Tool(ca_number)`.
        - If the tool says [FallBack], inform the user you are transferring them to a Human Agent. (Update flow_step to "fallback_to_human")
        - If the tool says [Success], inform the user a Fast-track ticket is opened. (Update flow_step to "fast_track_created")
    """

    chat_history: str = dspy.InputField(
        desc="The transcript of the conversation so far."
    )
    question: str = dspy.InputField(desc="The latest user message.")
    time_stamp: str = dspy.InputField(desc="The current timestamp of the request.")

    current_state: PEA_Conversation_State = dspy.OutputField(desc="The updated state.")
    answer: str = dspy.OutputField(
        desc="Your natural language response to the user in Thai."
    )
