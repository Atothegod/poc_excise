import dspy
from pydantic import BaseModel, Field
from typing import Optional

class PEA_Conversation_State(BaseModel):
    ca_number: Optional[str] = Field(None, description="The 9 or 12 digit CA Number. None if not yet provided.")
    is_repeated_event: Optional[bool] = Field(None, description="True if a grouped event, False if new, None if unchecked.")
    needs_eta: bool = Field(False, description="Did the user ask for the technician's Estimated Time of Arrival (ETA)?")
    needs_etr: bool = Field(False, description="Did the user ask for the power restoration time (ETR)?")

class PEA_Assistant(dspy.Signature):
    """ 
    PEA Assistant is designed to help users with power outage reporting (PEA OMS).
    
    Available Tools:
    - ETA_estimator(ca_number): Calculates the technician's arrival time based on the user's CA number.
    - ETR_estimator(ca_number): Calculates the power restoration time based on the user's CA number. It automatically checks if the event is new or grouped.
    
    Strict Rules:
    1. Read the `chat_history` to understand the context. Do not repeat questions you have already asked.
    2. If `ca_number` is None, you MUST ask the user for it before using any tools.
    3. Once you have the CA number, use ETA_estimator or ETR_estimator depending on what the user asked.
    4. Respond politely and concisely in Thai.
    """
    
    chat_history: str = dspy.InputField(desc="The transcript of the conversation so far.")
    question: str = dspy.InputField(desc="The latest user message.")

    time_stamp: str = dspy.InputField(
        desc="The current timestamp of the request in ISO format."
    ) 
    
    current_state: PEA_Conversation_State = dspy.OutputField(desc="The updated state of the conversation variables.")
    answer: str = dspy.OutputField(desc="Your natural language response to the user in Thai.")