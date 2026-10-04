"""Exotel Connect Voice AI API and callback normalization."""
from urllib.parse import quote

import httpx

from .config import settings
from .voice_agent import stream_signature


class ExotelError(RuntimeError):
    pass


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
    body = response.text
    # Exotel may return XML for this API; parse without logging credentials or phone numbers.
    from xml.etree import ElementTree
    try:
        root = ElementTree.fromstring(body)
        sid = root.findtext(".//Sid") or root.findtext(".//CallSid")
    except ElementTree.ParseError:
        try:
            payload = response.json()
            sid = payload.get("Sid") or payload.get("CallSid") or payload.get("call_sid")
        except ValueError:
            sid = None
    if not sid:
        raise ExotelError("Exotel response did not contain a call SID")
    return str(sid)


def normalize_status(raw: str) -> str:
    value = raw.lower().replace("_", "-")
    if value in {"completed", "finished", "answered"}:
        return "completed"
    if value in {"busy", "no-answer", "failed", "canceled", "cancelled"}:
        return "failed"
    return "dialing"
