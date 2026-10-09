"""Synthetic Exotel-format audio probe. This module never calls a carrier."""

import argparse
import asyncio
import base64
import json
import os
import ssl
from uuid import uuid4
import wave

import websockets
from sqlalchemy import select

from .config import settings
from .db import SessionLocal
from .models import CallAttempt, CallSession, Campaign, Contact
from .voice_agent import stream_signature


async def _receive_audio(url: str, sample_rate: int) -> list[bytes]:
    tls = None
    if url.startswith("wss://"):
        tls = ssl.create_default_context(cafile=os.getenv("SSL_CERT_FILE") or None)
    chunks: list[bytes] = []
    async with websockets.connect(url, ssl=tls, open_timeout=8) as stream:
        await stream.send(json.dumps({
            "event": "start", "stream_sid": "synthetic-probe",
            "start": {"stream_sid": "synthetic-probe", "media_format": {"sample_rate": sample_rate}},
        }))
        for _ in range(1000):
            try:
                message = json.loads(await asyncio.wait_for(stream.recv(), timeout=3))
            except asyncio.TimeoutError:
                break
            if message.get("event") == "media":
                chunks.append(base64.b64decode(message["media"]["payload"], validate=True))
    return chunks


def main() -> None:
    parser = argparse.ArgumentParser(description="Probe gateway audio without dialing")
    parser.add_argument("--url", default=settings.exotel_stream_url,
                        help="Base ws:// or wss:// media URL, without attempt ID")
    parser.add_argument("--output", help="Optional output WAV path")
    args = parser.parse_args()
    if not args.url.startswith(("ws://", "wss://")):
        raise ValueError("A WebSocket media URL is required")

    with SessionLocal() as db:
        contacts = db.scalars(select(Contact).where(Contact.suppressed.is_(False))).all()
        if len(contacts) != 1:
            raise RuntimeError("Probe requires exactly one unsuppressed synthetic or approved contact")
        campaign = Campaign(name="Synthetic media probe", status="stopped", max_concurrent=1)
        db.add(campaign)
        db.flush()
        attempt = CallAttempt(campaign_id=campaign.id, contact_id=contacts[0].id,
                              idempotency_key="probe-" + uuid4().hex, status="dialing")
        db.add(attempt)
        db.commit()
        attempt_id = attempt.id

    try:
        url = f"{args.url.rstrip('/')}/{attempt_id}/{stream_signature(attempt_id)}"
        chunks = asyncio.run(_receive_audio(url, 8000))
        if not chunks:
            raise RuntimeError("Gateway produced no audio media packets")
        if args.output:
            with wave.open(args.output, "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(8000)
                output.writeframes(b"".join(chunks))
        print(f"media_packets={len(chunks)} audio_seconds={sum(map(len, chunks)) / 16000:.2f}")
    finally:
        with SessionLocal() as db:
            attempt = db.get(CallAttempt, attempt_id)
            attempt.status = "failed"
            attempt.reason = "Synthetic media probe; no phone call placed"
            session = db.scalar(select(CallSession).where(CallSession.attempt_id == attempt_id))
            if session:
                session.status = "ended"
                session.outcome = "synthetic_preflight"
            db.commit()


if __name__ == "__main__":
    main()
