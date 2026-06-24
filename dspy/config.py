from dotenv import load_dotenv
load_dotenv()

import litellm
import os
import dspy

lm = dspy.LM(
    model="gemini/gemini-2.5-flash",
    api_key=os.getenv("API_KEY_4"),
    temperature=0
)

dspy.settings.configure(lm=lm)