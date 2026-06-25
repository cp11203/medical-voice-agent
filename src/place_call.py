import os
import sys
import urllib.parse

from dotenv import load_dotenv
from twilio.rest import Client

import persona

load_dotenv()

persona_name = sys.argv[1] if len(sys.argv) > 1 else persona.DEFAULT_PERSONA
available = persona.list_personas()
if persona_name not in available:
    print(f"Unknown persona '{persona_name}'. Available personas: {', '.join(available)}")
    sys.exit(1)

client = Client(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])

base_url = os.environ["PUBLIC_BASE_URL"]
twiml_url = base_url + "/twiml?" + urllib.parse.urlencode({"persona_name": persona_name})

call = client.calls.create(
    to=os.environ["TARGET_PHONE_NUMBER"],
    from_=os.environ["TWILIO_PHONE_NUMBER"],
    url=twiml_url,
    record=True,
    recording_status_callback=base_url + "/recording-status",
    recording_status_callback_event=["completed"],
)

print(f"Call placed with persona '{persona_name}', SID:", call.sid)
