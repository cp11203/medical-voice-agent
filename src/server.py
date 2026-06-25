import asyncio
import base64
import json
import os

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from twilio.rest import Client

load_dotenv()

import persona
from llm import ConversationAgent
from stt import DeepgramStream
from tts import TextToSpeech

app = FastAPI()

PUBLIC_BASE_URL = os.environ["PUBLIC_BASE_URL"]
TWILIO_ACCOUNT_SID = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN = os.environ["TWILIO_AUTH_TOKEN"]

CALLS_DIR = os.path.join(os.path.dirname(__file__), "..", "calls")
os.makedirs(CALLS_DIR, exist_ok=True)

# Populated when each call's media stream starts, read by the /recording-status
# webhook (a separate HTTP request that only carries the Twilio CallSid) so
# recordings can be filed under the same persona-tagged name as the transcript.
call_sid_to_persona = {}

# How long to wait for either side to say anything else after the bot signals
# the conversation is winding down, before actually cutting the line. Models
# a natural phone hangup -- both sides trail off, then a pause, then it ends.
GOODBYE_GRACE_SECONDS = 2.5

AUDIO_CHUNK_SIZE = 160  # 20ms of 8kHz mulaw, matches Twilio's media framing
SILENCE_PAD_SECONDS = 0.3  # trailing silence so the REST hangup doesn't clip the last reply mid-frame
TRAILING_SILENCE_SECONDS = 0.15  # appended to every reply to mask TTS-tail/jitter clipping

twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)


@app.post("/twiml")
async def twiml(persona_name: str = persona.DEFAULT_PERSONA):
    stream_url = PUBLIC_BASE_URL.replace("https://", "wss://").replace("http://", "ws://") + "/media-stream"
    twiml_response = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="{stream_url}">
            <Parameter name="persona" value="{persona_name}" />
        </Stream>
    </Connect>
</Response>"""
    return Response(content=twiml_response, media_type="application/xml")


@app.post("/recording-status")
async def recording_status(request: Request):
    form = await request.form()
    if form.get("RecordingStatus") != "completed":
        return Response(status_code=204)

    call_sid = form.get("CallSid")
    recording_url = form.get("RecordingUrl")
    persona_name = call_sid_to_persona.get(call_sid, "unknown_persona")
    dest_path = os.path.join(CALLS_DIR, f"{call_sid}__{persona_name}.mp3")

    def download():
        resp = requests.get(
            recording_url + ".mp3", auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        )
        resp.raise_for_status()
        with open(dest_path, "wb") as f:
            f.write(resp.content)

    try:
        await asyncio.to_thread(download)
        print(f"Saved recording for '{persona_name}' ({call_sid}) to {dest_path}")
    except Exception:
        import traceback

        print(f"[ERROR] failed to download recording for {call_sid}:")
        traceback.print_exc()

    return Response(status_code=204)


@app.websocket("/media-stream")
async def media_stream(websocket: WebSocket):
    await websocket.accept()
    print("Media stream connected")

    call = {
        "stream_sid": None,
        "call_sid": None,
        "persona_name": None,
        "speaking_task": None,
        "conversation": None,
        "tts": None,
        "playback_done": asyncio.Event(),
        "hangup_task": None,
    }

    async def send_clear():
        await websocket.send_text(json.dumps({"event": "clear", "streamSid": call["stream_sid"]}))

    async def speak(text: str):
        stream_sid = call["stream_sid"]
        buffer = bytearray()
        total_bytes = 0
        call["playback_done"].clear()

        async for audio_chunk in call["tts"].synthesize_stream(text):
            buffer.extend(audio_chunk)
            while len(buffer) >= AUDIO_CHUNK_SIZE:
                frame = bytes(buffer[:AUDIO_CHUNK_SIZE])
                del buffer[:AUDIO_CHUNK_SIZE]
                total_bytes += len(frame)
                payload = base64.b64encode(frame).decode("ascii")
                await websocket.send_text(
                    json.dumps({"event": "media", "streamSid": stream_sid, "media": {"payload": payload}})
                )
        if buffer:
            total_bytes += len(buffer)
            payload = base64.b64encode(bytes(buffer)).decode("ascii")
            await websocket.send_text(
                json.dumps({"event": "media", "streamSid": stream_sid, "media": {"payload": payload}})
            )

        # A trailing silence pad so a TTS-stream-end or network-jitter glitch
        # in the last real chunk doesn't land as audible clipping right at
        # the end of the reply.
        silence_frame = base64.b64encode(b"\xff" * AUDIO_CHUNK_SIZE).decode("ascii")
        pad_frames = int(TRAILING_SILENCE_SECONDS * 8000 / AUDIO_CHUNK_SIZE)
        for _ in range(pad_frames):
            total_bytes += AUDIO_CHUNK_SIZE
            await websocket.send_text(
                json.dumps({"event": "media", "streamSid": stream_sid, "media": {"payload": silence_frame}})
            )

        await websocket.send_text(
            json.dumps({"event": "mark", "streamSid": stream_sid, "mark": {"name": "reply-done"}})
        )

        # Sending these messages only queues them -- Twilio echoes the mark
        # back once it has actually finished playing the audio, so wait for
        # that instead of guessing how long the reply takes to speak.
        playback_seconds = total_bytes / 8000  # 8kHz mulaw = 8000 bytes/sec
        try:
            await asyncio.wait_for(call["playback_done"].wait(), timeout=playback_seconds + 2)
        except asyncio.TimeoutError:
            print("[WARN] Timed out waiting for Twilio's playback-done mark")

    async def terminate_call():
        # The REST API tears the call down immediately, which can clip the
        # tail of the last reply mid-frame and sound like a muffled click.
        # Padding with real silence first gives Twilio something clean to
        # finish playing before the line actually drops.
        silence_frame = base64.b64encode(b"\xff" * AUDIO_CHUNK_SIZE).decode("ascii")
        pad_frames = int(SILENCE_PAD_SECONDS * 8000 / AUDIO_CHUNK_SIZE)
        for _ in range(pad_frames):
            await websocket.send_text(
                json.dumps(
                    {"event": "media", "streamSid": call["stream_sid"], "media": {"payload": silence_frame}}
                )
            )
        await asyncio.sleep(SILENCE_PAD_SECONDS)
        await asyncio.to_thread(twilio_client.calls(call["call_sid"]).update, status="completed")

    def cancel_pending_hangup():
        hangup_task = call["hangup_task"]
        if hangup_task is not None and not hangup_task.done():
            hangup_task.cancel()
            print("New speech detected, call isn't actually over yet -- cancelling pending hangup")

    async def hangup_after_pause():
        try:
            await asyncio.sleep(GOODBYE_GRACE_SECONDS)
            print("No further response after goodbye, hanging up")
            await terminate_call()
        except asyncio.CancelledError:
            pass
        except Exception:
            import traceback

            print("[ERROR] delayed hangup failed:")
            traceback.print_exc()

    def schedule_hangup():
        cancel_pending_hangup()
        call["hangup_task"] = asyncio.create_task(hangup_after_pause())

    async def handle_turn(utterance: str):
        print(f"[turn] agent said: {utterance}")

        try:
            reply, call_is_over = await call["conversation"].respond(utterance)
            print(f"[bot reply] {reply}" + (" [winding down]" if call_is_over else ""))
            await speak(reply)

            if call_is_over:
                # Don't hang up immediately -- the other side might say one
                # more thing back. Only cut the line after a pause with no
                # further speech from either party.
                schedule_hangup()
        except asyncio.CancelledError:
            raise
        except Exception:
            import traceback

            print("[ERROR] handle_turn failed:")
            traceback.print_exc()

    async def cancel_bot_speech():
        speaking_task = call["speaking_task"]
        if speaking_task is not None and not speaking_task.done() and not speaking_task.cancelling():
            speaking_task.cancel()
            await send_clear()

    async def on_transcript(transcript: str, is_final: bool):
        tag = "final-segment" if is_final else "interim"
        print(f"[{tag}] agent said: {transcript}")
        # New speech arriving from the agent while we're still playing our own
        # reply means they talked over us (or we talked over them) -- bail out
        # of our own playback rather than continue speaking over each other.
        await cancel_bot_speech()
        cancel_pending_hangup()

    async def on_turn_complete(utterance: str):
        await cancel_bot_speech()
        call["speaking_task"] = asyncio.create_task(handle_turn(utterance))

    deepgram = DeepgramStream(on_transcript, on_turn_complete)
    await deepgram.connect()
    listen_task = asyncio.create_task(deepgram.listen())

    try:
        while True:
            message = await websocket.receive_text()
            data = json.loads(message)
            event = data.get("event")

            if event == "start":
                call["stream_sid"] = data["start"]["streamSid"]
                call["call_sid"] = data["start"]["callSid"]

                persona_name = data["start"].get("customParameters", {}).get("persona", persona.DEFAULT_PERSONA)
                persona_data = persona.load_persona(persona_name)
                call["persona_name"] = persona_name
                call["conversation"] = ConversationAgent(system_prompt=persona_data["system_prompt"])
                call["tts"] = TextToSpeech(voice_id=persona_data["voice_id"])
                call_sid_to_persona[call["call_sid"]] = persona_name

                print("Stream started:", call["stream_sid"], call["call_sid"], "persona:", persona_name)
            elif event == "media":
                payload = base64.b64decode(data["media"]["payload"])
                await deepgram.send_audio(payload)
            elif event == "mark":
                call["playback_done"].set()
            elif event == "stop":
                print("Stream stopped")
                break
    except WebSocketDisconnect:
        print("Media stream disconnected")
    except Exception:
        # A dropped Deepgram/TTS connection or similar shouldn't crash the
        # whole call ungracefully -- log it and still fall through to finally
        # so the transcript/recording get saved and the call ends cleanly.
        import traceback

        print("[ERROR] media_stream loop failed:")
        traceback.print_exc()
    finally:
        listen_task.cancel()
        await deepgram.close()
        if call["speaking_task"] is not None:
            call["speaking_task"].cancel()
        if call["hangup_task"] is not None:
            call["hangup_task"].cancel()

        if call["conversation"] is not None and call["call_sid"] is not None:
            transcript_path = os.path.join(
                CALLS_DIR, f"{call['call_sid']}__{call['persona_name']}.txt"
            )
            with open(transcript_path, "w") as f:
                f.write(call["conversation"].transcript())
            print(f"Saved transcript to {transcript_path}")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
