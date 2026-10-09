"""Small, bounded Sarvam requests for the local browser voice lab.

The browser never receives the provider key. Audio is processed in memory and
is not added to the call transcript; only accepted text turns are persisted.
"""

import base64

import httpx
from fastapi import HTTPException

from .config import settings


SARVAM_URL = "https://api.sarvam.ai"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, connect=10.0),
        headers={"api-subscription-key": settings.sarvam_api_key},
    )


def _check_response(response: httpx.Response) -> None:
    if response.status_code == 401 or response.status_code == 403:
        raise HTTPException(502, "Sarvam rejected the configured API key")
    if response.status_code == 402:
        raise HTTPException(502, "Sarvam credits are exhausted")
    if response.status_code == 429:
        raise HTTPException(503, "Sarvam rate limit reached; please retry shortly")
    if response.is_error:
        raise HTTPException(502, f"Sarvam request failed (HTTP {response.status_code})")


async def synthesize(text: str, language: str) -> bytes:
    if not settings.sarvam_api_key:
        raise HTTPException(503, "Add SARVAM_API_KEY to enable voice playback")
    try:
        async with _client() as client:
            response = await client.post(
                f"{SARVAM_URL}/text-to-speech",
                json={"text": text, "language_code": language, "model": "bulbul:v3", "speaker": "priya"},
            )
        _check_response(response)
        encoded = response.json()["audios"][0]
        audio = base64.b64decode(encoded, validate=True)
        if not audio or len(audio) > 5_000_000:
            raise ValueError("Invalid audio size")
        return audio
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError, KeyError, IndexError) as exc:
        raise HTTPException(502, "Sarvam voice playback is unavailable") from exc


async def transcribe(audio: bytes, content_type: str) -> str:
    if not settings.sarvam_api_key:
        raise HTTPException(503, "Add SARVAM_API_KEY to enable microphone transcription")
    extension = {"audio/webm": "webm", "audio/ogg": "ogg", "audio/mp4": "m4a", "audio/wav": "wav"}.get(content_type)
    if not extension:
        raise HTTPException(415, "Use WebM, Ogg, MP4, or WAV audio")
    if not audio or len(audio) > 2_000_000:
        raise HTTPException(413, "Record a shorter clip (maximum 2 MB)")
    try:
        async with _client() as client:
            response = await client.post(
                f"{SARVAM_URL}/speech-to-text",
                files={"file": (f"voice-lab.{extension}", audio, content_type)},
                data={"model": "saaras:v4", "mode": "codemix"},
            )
        _check_response(response)
        return str(response.json().get("transcript") or "").strip()
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(502, "Sarvam transcription is unavailable; type your reply instead") from exc
