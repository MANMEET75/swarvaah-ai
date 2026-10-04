"""Exotel media gateway with a bounded, deterministic reminder conversation.

Run as a separate ASGI service. Import Pipecat lazily so the control API remains
usable without the optional voice dependencies or external provider accounts.
"""
import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from sqlalchemy import select

from .config import settings
from .core import add_turn, start_session
from .db import SessionLocal
from .models import CallAttempt, CallEvent, CallSession, Contact


log = logging.getLogger("swarvaah.voice")
app = FastAPI(title="Swarvaah AI media gateway")


def stream_signature(attempt_id: str) -> str:
    return hmac.new(settings.exotel_callback_secret.encode(), attempt_id.encode(), hashlib.sha256).hexdigest()


def _complete_disconnected(session_id: str) -> None:
    with SessionLocal() as db:
        call = db.get(CallSession, session_id)
        if call and call.status != "ended":
            call.status = "ended"
            call.outcome = "disconnected"
            call.ended_at = datetime.now(timezone.utc)
            db.commit()


@app.websocket("/ws/exotel")
async def exotel_stream(websocket: WebSocket) -> None:
    attempt_id = websocket.query_params.get("attempt_id", "")
    signature = websocket.query_params.get("sig", "")
    if not settings.exotel_callback_secret or not hmac.compare_digest(signature, stream_signature(attempt_id)):
        await websocket.close(code=1008)
        return
    with SessionLocal() as db:
        attempt = db.get(CallAttempt, attempt_id)
        contact = db.get(Contact, attempt.contact_id) if attempt else None
        if not attempt or not contact or contact.suppressed or attempt.status not in {"dialing", "connected"}:
            await websocket.close(code=1008)
            return
        session = db.scalar(select(CallSession).where(CallSession.attempt_id == attempt_id))
        if session:
            await websocket.close(code=1008)
            return
        session = start_session(db, contact, attempt)
        session_id = session.id
        greeting_text = db.scalar(select(CallEvent.text).where(CallEvent.session_id == session_id, CallEvent.kind == "assistant"))
        language = contact.language

    await websocket.accept()
    try:
        # The first Exotel packets are metadata; Pipecat consumes subsequent media.
        stream_sid = None
        exotel_sample_rate = 8000
        for _ in range(3):
            message = json.loads(await websocket.receive_text())
            if message.get("event") == "start":
                start = message.get("start") or {}
                stream_sid = (message.get("stream_sid") or message.get("streamSid") or
                              start.get("stream_sid") or start.get("streamSid"))
                media_format = start.get("media_format") or {}
                exotel_sample_rate = int(media_format.get("sample_rate") or 8000)
                break
        if not stream_sid or exotel_sample_rate not in {8000, 16000, 24000}:
            await websocket.close(code=1008)
            return

        from pipecat.frames.frames import TextFrame, TranscriptionFrame, TTSSpeakFrame
        from pipecat.pipeline.pipeline import Pipeline
        from pipecat.pipeline.worker import PipelineParams, PipelineWorker
        from pipecat.processors.frame_processor import FrameProcessor
        from pipecat.serializers.exotel import ExotelFrameSerializer
        from pipecat.services.sarvam.stt import SarvamRealtimeSTTService
        from pipecat.services.sarvam.tts import SarvamTTSService
        from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport
        from pipecat.transcriptions.language import Language
        from pipecat.workers.runner import WorkerRunner

        class ReminderProcessor(FrameProcessor):
            async def process_frame(self, frame, direction):
                await super().process_frame(frame, direction)
                if isinstance(frame, TranscriptionFrame) and frame.text.strip():
                    with SessionLocal() as db:
                        call = db.get(CallSession, session_id)
                        person = db.get(Contact, call.contact_id) if call else None
                        if call and person and call.status != "ended":
                            turn = add_turn(db, call, person, frame.text)
                            await self.push_frame(TTSSpeakFrame(turn["reply"]))
                    return
                if not isinstance(frame, TextFrame):
                    await self.push_frame(frame, direction)

        transport = FastAPIWebsocketTransport(websocket, FastAPIWebsocketParams(
            serializer=ExotelFrameSerializer(stream_sid=stream_sid,
                params=ExotelFrameSerializer.InputParams(exotel_sample_rate=exotel_sample_rate)), audio_in_enabled=True,
            audio_out_enabled=True, audio_in_sample_rate=16000, audio_out_sample_rate=16000,
        ))
        stt = SarvamRealtimeSTTService(api_key=settings.sarvam_api_key,
            settings=SarvamRealtimeSTTService.Settings(language_code="auto", mode="codemix", stream_type="fast"))
        tts = SarvamTTSService(api_key=settings.sarvam_api_key,
            settings=SarvamTTSService.Settings(model="bulbul:v3", voice="priya", language=Language.HI if language == "hi-IN" else Language.EN,
                                              min_buffer_size=20))
        worker = PipelineWorker(Pipeline([transport.input(), stt, ReminderProcessor(), tts, transport.output()]),
                                params=PipelineParams(enable_metrics=True, enable_usage_metrics=True))
        runner = WorkerRunner()
        @transport.event_handler("on_client_connected")
        async def on_connected(_transport, _websocket):
            await worker.queue_frame(TTSSpeakFrame(greeting_text))

        @transport.event_handler("on_client_disconnected")
        async def on_disconnected(_transport, _websocket):
            await worker.cancel()

        await runner.add_workers(worker)
        await runner.run()
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("Media pipeline failed for attempt %s", attempt_id)
        raise
    finally:
        _complete_disconnected(session_id)
