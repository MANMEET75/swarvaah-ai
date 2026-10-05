"""Twilio outbound fallback with signed callbacks and bidirectional Media Streams."""

import base64
import hashlib
import hmac
from urllib.parse import quote
from xml.etree import ElementTree

import httpx

from .config import settings


class TwilioError(RuntimeError):
    pass


def signed_request(url: str, params: dict[str, str], signature: str) -> bool:
    """Verify Twilio's HMAC-SHA1 signature using the exact public URL."""
    if not settings.twilio_auth_token or not signature:
        return False
    payload = url + "".join(key + params[key] for key in sorted(params))
    digest = hmac.new(settings.twilio_auth_token.encode(), payload.encode(), hashlib.sha1).digest()
    expected = base64.b64encode(digest).decode()
    return hmac.compare_digest(expected, signature)


def stream_twiml(attempt_id: str) -> str:
    if not settings.twilio_stream_url.startswith("wss://") or "?" in settings.twilio_stream_url:
        raise TwilioError("Twilio stream URL must be a public WSS URL without query parameters")
    response = ElementTree.Element("Response")
    connect = ElementTree.SubElement(response, "Connect")
    stream = ElementTree.SubElement(connect, "Stream", url=settings.twilio_stream_url)
    ElementTree.SubElement(stream, "Parameter", name="attempt_id", value=attempt_id)
    return ElementTree.tostring(response, encoding="unicode")


def place_call(phone: str, attempt_id: str) -> str:
    if not settings.twilio_fallback_enabled or not settings.live_dial_enabled:
        raise TwilioError("Twilio fallback is disabled")
    url = f"https://api.twilio.com/2010-04-01/Accounts/{quote(settings.twilio_account_sid)}/Calls.json"
    callback = f"{settings.public_base_url.rstrip('/')}/v1/webhooks/twilio"
    data = {
        "To": phone,
        "From": settings.twilio_caller_id,
        "Twiml": stream_twiml(attempt_id),
        "StatusCallback": callback,
        "StatusCallbackMethod": "POST",
        "StatusCallbackEvent": "completed",
        "Record": "false",
    }
    try:
        with httpx.Client(timeout=10) as client:
            result = client.post(url, data=data, auth=(settings.twilio_account_sid, settings.twilio_auth_token))
            result.raise_for_status()
        sid = result.json().get("sid")
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        raise TwilioError(f"Twilio dial outcome uncertain: {type(exc).__name__}") from exc
    if not isinstance(sid, str) or not sid.startswith("CA"):
        raise TwilioError("Twilio returned no valid call SID; outcome uncertain")
    return sid


def normalize_status(raw: str) -> str:
    value = raw.lower().replace("_", "-")
    if value == "completed":
        return "completed"
    if value in {"failed", "busy", "no-answer", "canceled"}:
        return "failed"
    if value == "in-progress":
        return "connected"
    return "dialing"
