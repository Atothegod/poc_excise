import dspy
from pydantic import BaseModel, Field
from typing import Optional, Literal


class PEA_Conversation_State(BaseModel):
    ca_number: Optional[str] = Field(
        None, description="The 9 or 12 digit CA Number. None if not yet provided."
    )

    # ปรับ State เป็นลำดับขั้น (Sequence) เพื่อควบคุม Flow การตอบของ Agent
    flow_step: Literal[
        "waiting_for_ca",
        "checking_outage",
        "providing_eta_first",
        "mass_outage_providing_etr",
        "resolved",
    ] = Field(
        "waiting_for_ca", description="The current stage of the conversation flow."
    )

    is_mass_outage: Optional[bool] = Field(
        None,
        description="True if the tool indicates a repeated/mass event, False if it is a new event.",
    )


class PEA_Assistant(dspy.Signature):
    """
    PEA Assistant is designed to help users with power outage reporting (PEA OMS).

    Available Tools:
    - Check_Outage_Tool(ca_number): ตรวจสอบสถานะไฟดับด้วยหมายเลข CA ระบบจะแยกแยะให้ว่าเป็นเหตุใหม่ (Normal Outage) หรือเหตุไฟดับวงกว้าง (Mass Outage) พร้อมคืนค่าเวลาที่ต้องใช้ตอบลูกค้า

    Strict Rules:
    1. Read the `chat_history` to understand the context. Do not repeat questions you have already asked.
    2. If `ca_number` is None (or `flow_step` is "waiting_for_ca"), you MUST politely ask the user for their 9 or 12 digit CA Number before using any tools.
    3. Once you have the CA number, use `Check_Outage_Tool` ONLY.
    4. DECISION BRANCH (สาย A): If the tool returns [เหตุวงกว้าง], you must inform the user of the ETR immediately. DO NOT mention ETA. Update flow_step to "mass_outage_providing_etr".
    5. DECISION BRANCH (สาย B): If the tool returns [เหตุแจ้งใหม่] or [เหตุปกติ], you must inform the user of the ETA first. State that the ETR is being evaluated (if not yet available). Update flow_step to "providing_eta_first".
    6. Respond politely, concisely, and naturally in Thai.
    """

    chat_history: str = dspy.InputField(
        desc="The transcript of the conversation so far."
    )
    question: str = dspy.InputField(desc="The latest user message.")
    time_stamp: str = dspy.InputField(
        desc="The current timestamp of the request in ISO format."
    )

    current_state: PEA_Conversation_State = dspy.OutputField(
        desc="The updated state of the conversation variables."
    )
    answer: str = dspy.OutputField(
        desc="Your natural language response to the user in Thai."
    )
