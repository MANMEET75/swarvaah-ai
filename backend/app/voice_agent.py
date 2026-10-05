"""Exotel media gateway with a bounded reminder conversation.

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
from .core import start_session
from .conversation import add_conversation_turn
from .db import SessionLocal
from .models import CallAttempt, CallEvent, CallSession, Contact
from .twilio_provider import signed_request as twilio_signed_request


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


async def _run_pipeline(websocket: WebSocket, session_id: str, greeting_text: str,
                        language: str, serializer) -> None:
    from pipecat.audio.vad.silero import SileroVADAnalyzer
    from pipecat.audio.vad.vad_analyzer import VADParams
    from pipecat.frames.frames import TextFrame, TranscriptionFrame, TTSSpeakFrame
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.worker import PipelineParams, PipelineWorker
    from pipecat.processors.audio.vad_processor import VADProcessor
    from pipecat.processors.frame_processor import FrameProcessor
    from pipecat.services.sarvam.stt import SarvamRealtimeSTTService
    from pipecat.services.sarvam.tts import SarvamTTSService
    from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport
    from pipecat.transcriptions.language import Language
    from pipecat.turns.user_start.vad_user_turn_start_strategy import VADUserTurnStartStrategy
    from pipecat.turns.user_stop.speech_timeout_user_turn_stop_strategy import SpeechTimeoutUserTurnStopStrategy
    from pipecat.turns.user_turn_processor import UserTurnProcessor
    from pipecat.turns.user_turn_strategies import UserTurnStrategies
    from pipecat.workers.runner import WorkerRunner

    class ReminderProcessor(FrameProcessor):
        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)
            if isinstance(frame, TranscriptionFrame) and frame.text.strip():
                with SessionLocal() as db:
                    call = db.get(CallSession, session_id)
                    person = db.get(Contact, call.contact_id) if call else None
                    if call and person and call.status != "ended":
                        turn = await add_conversation_turn(db, call, person, frame.text)
                        await self.push_frame(TTSSpeakFrame(turn["reply"]))
                return
            if not isinstance(frame, TextFrame):
                await self.push_frame(frame, direction)

    transport = FastAPIWebsocketTransport(websocket, FastAPIWebsocketParams(
        serializer=serializer, audio_in_enabled=True, audio_out_enabled=True,
        audio_in_sample_rate=16000, audio_out_sample_rate=16000,
    ))
    vad = VADProcessor(vad_analyzer=SileroVADAnalyzer(
        params=VADParams(confidence=0.7, start_secs=0.2, stop_secs=0.6, min_volume=0.6)))
    stt = SarvamRealtimeSTTService(api_key=settings.sarvam_api_key,
        settings=SarvamRealtimeSTTService.Settings(language_code="auto", mode="codemix", stream_type="fast"))
    turns = UserTurnProcessor(user_turn_strategies=UserTurnStrategies(
        start=[VADUserTurnStartStrategy(enable_interruptions=True)],
        stop=[SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=0.6, wait_for_transcript=True)]))
    tts = SarvamTTSService(api_key=settings.sarvam_api_key,
        settings=SarvamTTSService.Settings(model="bulbul:v3", voice="priya",
            language=Language.HI if language == "hi-IN" else Language.EN, min_buffer_size=20))
    worker = PipelineWorker(Pipeline([transport.input(), vad, stt, turns, ReminderProcessor(), tts, transport.output()]),
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

        from pipecat.serializers.exotel import ExotelFrameSerializer
        serializer = ExotelFrameSerializer(stream_sid=stream_sid,
            params=ExotelFrameSerializer.InputParams(exotel_sample_rate=exotel_sample_rate))
        await _run_pipeline(websocket, session_id, greeting_text, language, serializer)
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("Media pipeline failed for attempt %s", attempt_id)
        raise
    finally:
        _complete_disconnected(session_id)


@app.websocket("/ws/twilio")
async def twilio_stream(websocket: WebSocket) -> None:
    if not settings.twilio_fallback_enabled:
        await websocket.close(code=1008)
        return
    signature = websocket.headers.get("X-Twilio-Signature", "")
    url = settings.twilio_stream_url
    if not (twilio_signed_request(url, {}, signature) or
            twilio_signed_request(url.rstrip("/") + "/", {}, signature)):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    session_id = None
    try:
        start = None
        for _ in range(3):
            message = json.loads(await websocket.receive_text())
            if message.get("event") == "start":
                start = message.get("start") or {}
                stream_sid = message.get("streamSid") or start.get("streamSid")
                break
        if not start or not stream_sid:
            await websocket.close(code=1008)
            return
        media = start.get("mediaFormat") or {}
        if (start.get("accountSid") != settings.twilio_account_sid or
            media.get("encoding") != "audio/x-mulaw" or int(media.get("sampleRate") or 0) != 8000):
            await websocket.close(code=1008)
            return
        attempt_id = (start.get("customParameters") or {}).get("attempt_id", "")
        call_sid = start.get("callSid", "")
        with SessionLocal() as db:
            attempt = db.get(CallAttempt, attempt_id)
            contact = db.get(Contact, attempt.contact_id) if attempt else None
            existing = db.scalar(select(CallSession).where(CallSession.attempt_id == attempt_id)) if attempt else None
            if (not attempt or not contact or contact.suppressed or existing or
                attempt.provider_sid != f"twilio:{call_sid}" or
                attempt.status not in {"dialing", "connected"}):
                await websocket.close(code=1008)
                return
            session = start_session(db, contact, attempt)
            session_id = session.id
            greeting = db.scalar(select(CallEvent.text).where(
                CallEvent.session_id == session_id, CallEvent.kind == "assistant"))
            language = contact.language

        from pipecat.serializers.twilio import TwilioFrameSerializer
        serializer = TwilioFrameSerializer(stream_sid=stream_sid, call_sid=call_sid,
            account_sid=settings.twilio_account_sid, auth_token=settings.twilio_auth_token)
        await _run_pipeline(websocket, session_id, greeting, language, serializer)
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("Twilio media pipeline failed")
        raise
    finally:
        if session_id:
            _complete_disconnected(session_id)
