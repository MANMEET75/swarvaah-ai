"""Exotel Connect Voice AI API and callback normalization."""
import json
from urllib.parse import quote
from xml.etree import ElementTree

import httpx

from .config import settings
from .voice_agent import stream_signature


class ExotelError(RuntimeError):
    pass


def extract_call_sid(body: str) -> str:
    """Accept the XML or JSON Call envelope returned by Exotel."""
    try:
        root = ElementTree.fromstring(body)
        sid = root.findtext(".//Sid") or root.findtext(".//CallSid")
        if root.tag in {"Sid", "CallSid"}:
            sid = sid or root.text
    except ElementTree.ParseError:
        try:
            payload = json.loads(body)
        except ValueError as exc:
            raise ExotelError("Exotel returned an unreadable response") from exc
        call = payload.get("Call", payload) if isinstance(payload, dict) else {}
        sid = call.get("Sid") or call.get("CallSid") or call.get("call_sid")
    if not sid:
        raise ExotelError("Exotel response did not contain a call SID")
    return str(sid)


def place_call(phone: str, attempt_id: str) -> str:
    if settings.call_mode != "exotel" or not settings.live_dial_enabled:
        raise ExotelError("Live dialing is disabled")
    url = f"https://api.in.exotel.com/v1/Accounts/{quote(settings.exotel_account_sid)}/Calls/connect"
    callback = f"{settings.public_base_url.rstrip('/')}/v1/webhooks/exotel/{quote(settings.exotel_callback_secret)}"
    stream_url = settings.exotel_stream_url + ("&" if "?" in settings.exotel_stream_url else "?") + f"attempt_id={quote(attempt_id)}&sig={stream_signature(attempt_id)}"
    data = {
        "StreamType": "bidirectional",
        "StreamUrl": stream_url,
        "From": phone,
        "CallerId": settings.exotel_caller_id,
        "Record": "false",
        "StatusCallback": callback,
        "StatusCallbackEvents[]": "terminal",
    }
    try:
        with httpx.Client(timeout=10) as client:
            response = client.post(url, data=data, auth=(settings.exotel_api_key, settings.exotel_api_token))
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise ExotelError(f"Exotel dial failed: {type(exc).__name__}") from exc
    return extract_call_sid(response.text)


def normalize_status(raw: str) -> str:
    value = raw.lower().replace("_", "-")
    if value in {"completed", "finished", "answered"}:
        return "completed"
    if value in {"busy", "no-answer", "failed", "canceled", "cancelled"}:
        return "failed"
    return "dialing"
