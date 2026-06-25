# Medical Voice Agent

A Python voice bot that calls Pretty Good AI's test line (+1-805-439-8008) and holds real, multi-turn phone conversations as a simulated patient. Built for the PGAI AI Engineering Challenge to surface bugs in their AI scheduling agent.

The bot placed **15 calls** across 15 distinct patient personas, producing verified MP3 recordings and transcripts for each. Analysis uncovered **6 confirmed bugs** ranging from a critical patient-safety triage failure to a systemic controlled-substance compliance gap.

Calling number (E.164): **+14243533188**

---

## Architecture

The system is built around Twilio Voice + Media Streams as the telephony layer, with a FastAPI server (`src/server.py`) as the single integration point. When a call is placed via `src/place_call.py`, Twilio dials the target number and fetches TwiML from the server's `/twiml` endpoint. That TwiML response opens a bidirectional WebSocket Media Stream back to `/media-stream`, giving the server real-time access to the raw audio on both sides of the call.

Inside `/media-stream`, three components run concurrently per call. **Deepgram** (`stt.py`) receives the incoming mulaw 8 kHz audio stream and emits turn-final transcripts using `speech_final` and `UtteranceEnd` events — this drives natural turn-taking without a fixed timer. Each transcript triggers **Claude Haiku** (`llm.py`), which is prompted with a patient persona and maintains conversational state; when the model appends the sentinel `ENDCALL_NOW` to a reply it means the conversation is naturally complete. The reply text is streamed to **ElevenLabs** (`tts.py`) which returns mulaw audio that is chunked and sent back into the Twilio stream. Twilio echoes a `mark` event when it finishes playing each chunk, letting the server wait for true playback completion before listening for the next patient utterance. Calls end either when the LLM signals the conversation is over (2.5-second grace period then REST hangup) or when PGAI's agent hangs up first. Recordings are downloaded from Twilio and transcripts are written to `calls/` at end of call.

Key design choices: mulaw 8 kHz throughout (fixed by Twilio, passed unchanged to Deepgram); streaming TTS to minimize latency; no hard turn or time caps so conversations end naturally; per-persona `voice_id` via ElevenLabs for distinct caller characters; `call_sid_to_persona` map so the async recording webhook can file MP3s under the right persona name.

---

## Setup

**Prerequisites:** Python 3.11+, a Twilio account, Deepgram API key, Anthropic API key, ElevenLabs API key, and a public HTTPS tunnel (ngrok recommended).

```bash
# 1. Clone and install
git clone <repo-url>
cd medical-voice-agent
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# Fill in all values in .env

# 3. Start your tunnel (keep this running)
ngrok http --domain=<your-domain> 8080

# 4. Start the media server (keep this running)
python src/server.py

# 5. Place a call (in a third terminal)
python src/place_call.py <persona_name>
```

Available personas: `simple_scheduling`, `emergency_severity_caller`, `controlled_substance_refill`, `controlled_substance_refill_v2`, `medication_refill`, `garbled_medication_name_caller`, `parent_calling_for_child`, `authorization_boundary_caller`, `inconsistent_identity_caller`, `weekend_boundary_seeker`, `weekend_boundary_seeker_v2`, `double_booking_conflict_caller`, `contradiction_catcher_caller`, `multi_intent_caller`, `hours_insurance`

Recordings and transcripts land in `calls/` as `<CallSID>__<persona>.mp3` and `.txt`.

---

## Bug Report

See [`bug_report.md`](bug_report.md) for the full findings with verified timestamps.
