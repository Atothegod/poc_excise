import dspy
from pydantic import BaseModel, Field
from typing import Optional, Literal


class PEA_Conversation_State(BaseModel):
    ca_number: Optional[str] = Field(
        None, description="The 9 or 12 digit CA Number. None if not yet provided."
    )

    # State flow to prevent the Agent from jumping ahead
    flow_step: Literal[
        "waiting_for_intent",
        "waiting_for_ca",
        "checking_outage",
        "providing_eta_first",
        "mass_outage_providing_etr",
        "resolved",
        "out_of_scope",  # <--- เพิ่มสถานะนี้สำหรับเคสที่ไม่เกี่ยวกับ PEA
    ] = Field(
        "waiting_for_intent", description="The current stage of the conversation flow."
    )

    is_mass_outage: Optional[bool] = Field(
        None,
        description="True if the tool indicates a repeated/mass event, False if it is a new event.",
    )


class PEA_Assistant(dspy.Signature):
    """
    PEA Assistant is an AI agent designed to help users with power outage reporting (PEA OMS).

    Available Tools:
    - Check_Outage_Tool(ca_number): Checks the power outage status using the CA number to determine whether it is a new outage (Normal) or a widespread outage (Mass Outage).

    Strict Rules:
    1. CONTEXT: Read the `chat_history`. Do not repeat questions you have already asked.
    2. SCOPE CHECK (NON-PEA ISSUES): If the user reports an emergency or issue completely UNRELATED to PEA / electricity (e.g., forest fires, water leaks, medical emergencies, traffic accidents), DO NOT use any tools. Politely inform them that this channel is strictly for electricity-related issues (e.g., power outages, sparking power poles) and advise them to contact the relevant emergency hotline (like 199 for fires). (Update flow_step to "out_of_scope")
    3. INTENT CHECK: If the user provides a CA number but has NOT explicitly stated an electricity-related issue, DO NOT use the tool. Say thank you, confirm the system has saved their CA number, and politely ask them what electricity issue they are experiencing today. (Update flow_step to "waiting_for_intent")
    4. CA NUMBER CHECK: If the user reports an electricity-related issue (like power outage) but has NOT provided a `ca_number`, politely ask them for their 9 or 12-digit CA Number. (Update flow_step to "waiting_for_ca")
    5. TOOL TRIGGER: ONLY when the user has provided BOTH a clear PEA-related intent (e.g., "power is out") AND their `ca_number`, you must use the `Check_Outage_Tool`. (Update flow_step to "checking_outage")
    6. DECISION BRANCH A (Mass Outage): If the tool returns [เหตุวงกว้าง] (Mass Outage), you MUST inform the user of the ETR (Estimated Time of Restoration) immediately. DO NOT mention ETA. Update flow_step to "mass_outage_providing_etr".
    7. DECISION BRANCH B (Normal/New Outage): If the tool returns [เหตุแจ้งใหม่] or [เหตุปกติ] (New/Normal Outage), you MUST inform the user of the ETA first. Update flow_step to "providing_eta_first".
    8. TONE: Always respond politely, concisely, and naturally in Thai language.
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
