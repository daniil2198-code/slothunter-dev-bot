"""Groq Whisper transcription for Telegram voice / audio messages.

Telegram voice notes are delivered as ``ogg/opus``; regular audio uploads
can be ``mp3``, ``m4a``, ``wav`` etc. Groq's Whisper endpoint accepts all
of these directly — no transcoding needed on our side.

Why Groq specifically:
  * ``whisper-large-v3-turbo`` runs ~100× realtime, so a 30-second voice
    note transcribes in well under a second of latency.
  * Russian-language quality is on par with the OpenAI Whisper API.
  * The free tier (as of 2025) covers personal usage of this bot
    comfortably — a paid plan kicks in only at sustained volume.

The Bot API does NOT expose Telegram Premium's built-in transcription,
so a server-side STT call is the only path for a Bot-API-based bridge.
(MTProto user-bots can call ``messages.transcribeAudio`` instead — see
notes/decisions if we ever switch transports.)
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.config import settings
from app.logging import get_logger

log = get_logger(__name__)

GROQ_TRANSCRIBE_URL = "https://api.groq.com/openai/v1/audio/transcriptions"


class TranscriptionError(RuntimeError):
    """Raised when Groq returns a non-2xx response or a malformed body."""


@dataclass
class Transcript:
    text: str
    duration_sec: float | None = None  # Groq returns this for verbose_json
    model: str = ""


async def transcribe_audio(
    audio_bytes: bytes,
    *,
    filename: str = "voice.ogg",
    language: str | None = "ru",
) -> Transcript:
    """Send an audio blob to Groq Whisper and return the transcript.

    Args:
        audio_bytes: raw bytes of the audio file (any Whisper-supported
            format — ogg, mp3, m4a, wav, webm, …).
        filename: filename hint for the multipart upload; Groq uses the
            extension to pick a decoder.
        language: ISO-639-1 hint. ``"ru"`` for Russian (the dominant
            case here). Pass ``None`` for autodetect — slightly slower
            and occasionally misidentifies short utterances.

    Returns:
        Transcript with ``.text`` ready to feed to Claude.

    Raises:
        TranscriptionError: on HTTP errors, timeouts, or malformed
            responses. Caller is expected to surface a friendly message
            to the user.
    """
    if not settings.groq_api_key:
        raise TranscriptionError("GROQ_API_KEY is not configured")

    # response_format=verbose_json gives us duration + per-segment data;
    # we only use ``text`` + ``duration`` but it's the same cost.
    files = {"file": (filename, audio_bytes, "application/octet-stream")}
    data: dict[str, str] = {
        "model": settings.groq_whisper_model,
        "response_format": "verbose_json",
        "temperature": "0",
    }
    if language:
        data["language"] = language

    headers = {"Authorization": f"Bearer {settings.groq_api_key}"}

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                GROQ_TRANSCRIBE_URL,
                headers=headers,
                files=files,
                data=data,
            )
    except httpx.HTTPError as e:
        log.warning("groq_transcribe_http_error", error=str(e))
        raise TranscriptionError(f"network error: {e}") from e

    if resp.status_code >= 400:
        # Groq returns ``{"error": {"message": "..."}}`` on failure.
        try:
            err = resp.json().get("error", {}).get("message") or resp.text
        except ValueError:
            err = resp.text
        log.warning(
            "groq_transcribe_failed",
            status=resp.status_code,
            error=err[:300],
        )
        raise TranscriptionError(f"groq {resp.status_code}: {err[:200]}")

    try:
        body = resp.json()
    except ValueError as e:
        raise TranscriptionError("malformed JSON from Groq") from e

    text = (body.get("text") or "").strip()
    if not text:
        raise TranscriptionError("empty transcript")

    return Transcript(
        text=text,
        duration_sec=body.get("duration"),
        model=settings.groq_whisper_model,
    )
