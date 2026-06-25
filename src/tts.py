import os

from elevenlabs.client import AsyncElevenLabs

ELEVENLABS_API_KEY = os.environ["ELEVENLABS_API_KEY"]
ELEVENLABS_VOICE_ID = os.environ["ELEVENLABS_VOICE_ID"]
ELEVENLABS_MODEL = os.environ.get("ELEVENLABS_MODEL", "eleven_flash_v2_5")

_client = AsyncElevenLabs(api_key=ELEVENLABS_API_KEY)


class TextToSpeech:
    """Synthesizes speech already encoded the way Twilio's media stream needs it."""

    def __init__(self, voice_id: str = None):
        self._voice_id = voice_id or ELEVENLABS_VOICE_ID

    async def synthesize_stream(self, text: str):
        """Yields raw ulaw_8000 audio as it arrives, instead of waiting for the
        full utterance -- this is what lets playback start while the tail of
        a reply is still being synthesized."""
        chunks = _client.text_to_speech.stream(
            voice_id=self._voice_id,
            text=text,
            model_id=ELEVENLABS_MODEL,
            output_format="ulaw_8000",
        )
        async for chunk in chunks:
            yield chunk
