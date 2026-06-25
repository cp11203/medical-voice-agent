import json
import os

import websockets

DEEPGRAM_API_KEY = os.environ["DEEPGRAM_API_KEY"]

DEEPGRAM_URL = (
    "wss://api.deepgram.com/v1/listen"
    "?encoding=mulaw&sample_rate=8000&channels=1"
    "&punctuate=true&interim_results=true&endpointing=500"
    "&utterance_end_ms=1200&vad_events=true"
)


class DeepgramStream:
    """Wraps a Deepgram live-transcription websocket connection for one call.

    `is_final` on a Results message only means that segment's text won't be
    revised further — it does NOT mean the speaker finished their turn,
    so it fires throughout a long utterance. Turn-end is signaled by either
    `speech_final` (VAD detected silence) or an `UtteranceEnd` message
    (word-timing-based fallback for when VAD doesn't trigger), so `on_turn_complete`
    is the callback to use for "the agent stopped talking, respond now".
    """

    def __init__(self, on_transcript, on_turn_complete):
        self._on_transcript = on_transcript
        self._on_turn_complete = on_turn_complete
        self._ws = None
        self._committed = ""
        self._pending = ""

    async def connect(self):
        self._ws = await websockets.connect(
            DEEPGRAM_URL,
            additional_headers={"Authorization": f"Token {DEEPGRAM_API_KEY}"},
        )

    async def send_audio(self, mulaw_bytes: bytes):
        if self._ws is not None:
            await self._ws.send(mulaw_bytes)

    async def _complete_turn(self):
        utterance = f"{self._committed} {self._pending}".strip()
        self._committed = ""
        self._pending = ""
        if utterance:
            await self._on_turn_complete(utterance)

    async def listen(self):
        async for message in self._ws:
            data = json.loads(message)
            msg_type = data.get("type")

            if msg_type == "UtteranceEnd":
                await self._complete_turn()
                continue

            if msg_type != "Results":
                continue

            alt = data["channel"]["alternatives"][0]
            transcript = alt.get("transcript", "")
            is_final = data.get("is_final", False)
            speech_final = data.get("speech_final", False)

            if is_final:
                if transcript:
                    self._committed = f"{self._committed} {transcript}".strip()
                self._pending = ""
            else:
                self._pending = transcript

            if transcript:
                await self._on_transcript(transcript, is_final)

            if speech_final:
                await self._complete_turn()

    async def close(self):
        if self._ws is not None:
            await self._ws.close()
