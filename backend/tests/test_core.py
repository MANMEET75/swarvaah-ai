import io
import base64
import hashlib
import hmac
import json
from types import SimpleNamespace
from uuid import uuid4
import httpx

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.models import AuditEvent, CallAttempt, Campaign, Contact, OutboxEvent
from app.voice_agent import stream_signature
from app.exotel import ExotelError, extract_call_sid
from app import worker
from app import conversation
from app import exotel
from app import main as main_module
from app import twilio_provider
from app import voice_agent
from app.workflow import respond


engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
Base.metadata.create_all(engine)


def session_override():
    with Session(engine) as db:
        yield db


app.dependency_overrides[get_db] = session_override
client = TestClient(app)


def test_import_launch_and_opt_out():
    csv_data = ("external_id,name,phone,reminder_at,reminder_label,language,consent_source,consent_at\n"
                "A1,Ananya,+919876543210,2026-10-08T10:00:00+05:30,check-in,hi-IN,booking form,2026-10-01T12:00:00+05:30\n")
    result = client.post("/v1/imports", files={"file": ("contacts.csv", io.BytesIO(csv_data.encode()), "text/csv")})
    assert result.status_code == 200
    assert result.json()["accepted"] == 1
    assert client.post("/v1/imports", files={"file": ("contacts.csv", csv_data, "text/csv")}).json()["updated"] == 1
    campaign = client.post("/v1/campaigns", json={"name": "Routine reminders"}).json()
    launched = client.post(f"/v1/campaigns/{campaign['id']}/launch")
    assert launched.json()["queued"] == 1
    assert client.post(f"/v1/campaigns/{campaign['id']}/launch").status_code == 400
    with Session(engine) as db:
        assert db.scalar(select(CallAttempt)) is not None
        assert db.scalar(select(OutboxEvent)) is not None
    call = client.post("/v1/test-calls", json={"language": "hi-IN"}).json()
    answer = client.post(f"/v1/test-calls/{call['id']}/turn", json={"text": "मुझे कॉल मत करना"}).json()
    assert answer["outcome"] == "opted_out"
    with Session(engine) as db:
        assert db.scalar(select(Contact).where(Contact.external_id.like("test-%"))).suppressed is True


def test_csv_rejects_invalid_phone_and_missing_columns():
    bad = client.post("/v1/imports", files={"file": ("bad.csv", "name,phone\nA,123\n", "text/csv")})
    assert bad.status_code == 400


def test_stream_signature_is_stable():
    assert stream_signature("abc") == stream_signature("abc")


def test_worker_simulates_queued_reminder(monkeypatch):
    monkeypatch.setattr(worker, "SessionLocal", lambda: Session(engine))
    assert worker.process_one() is True
    with Session(engine) as db:
        assert db.scalar(select(CallAttempt)).status == "completed"


def test_exotel_response_envelopes():
    assert extract_call_sid('{"Call":{"Sid":"call-json-1"}}') == "call-json-1"
    assert extract_call_sid('<Response><Call><Sid>call-xml-1</Sid></Call></Response>') == "call-xml-1"
    try:
        extract_call_sid('{"Call":{}}')
    except ExotelError:
        pass
    else:
        raise AssertionError("missing call SID must fail")


def test_exotel_only_marks_explicit_rejection_safe_for_fallback(monkeypatch):
    monkeypatch.setattr(exotel, "settings", SimpleNamespace(call_mode="exotel", live_dial_enabled=True,
        exotel_account_sid="synthetic", exotel_api_key="synthetic", exotel_api_token="synthetic",
        exotel_callback_secret="synthetic", exotel_stream_url="wss://example.test/ws/exotel",
        public_base_url="https://example.test", exotel_caller_id="synthetic"))
    responses = iter([httpx.Response(429, text="rejected"), httpx.Response(503, text="unavailable")])
    real_client = httpx.Client

    def fake_client(*_, **__):
        return real_client(transport=httpx.MockTransport(lambda request: next(responses)))

    monkeypatch.setattr(exotel.httpx, "Client", fake_client)
    for expected_safe in (True, False):
        try:
            exotel.place_call("+919876543210", "synthetic-attempt")
        except ExotelError as exc:
            assert exc.safe_to_fallback is expected_safe
        else:
            raise AssertionError("provider error should fail")


def test_voice_lab_keeps_provider_audio_out_of_call_history(monkeypatch):
    async def fake_synthesize(text, language):
        assert language == "en-IN"
        assert text == "Hello from the lab"
        return b"RIFFsynthetic"

    async def fake_transcribe(audio, content_type):
        assert audio == b"synthetic-audio"
        assert content_type == "audio/webm"
        return "Please reschedule"

    monkeypatch.setattr(main_module, "synthesize", fake_synthesize)
    monkeypatch.setattr(main_module, "transcribe", fake_transcribe)
    session = client.post("/v1/test-calls", json={"language": "en-IN"}).json()
    initial_events = len(session["events"])
    voice = client.post("/v1/voice-lab/speak", json={"text": "Hello from the lab", "language": "en-IN"})
    assert voice.status_code == 200
    assert voice.headers["content-type"] == "audio/wav"
    assert voice.content == b"RIFFsynthetic"
    transcript = client.post("/v1/voice-lab/transcribe", files={"file": ("reply.webm", b"synthetic-audio", "audio/webm")})
    assert transcript.json() == {"text": "Please reschedule"}
    call = client.get(f"/v1/calls/{session['id']}").json()
    assert len(call["events"]) == initial_events
    assert client.post("/v1/voice-lab/speak", json={"text": "x", "language": "fr-FR"}).status_code == 422


def test_ai_conversation_keeps_context_and_actions_in_workflow(monkeypatch):
    monkeypatch.setattr(conversation, "settings", SimpleNamespace(
        conversation_mode="sarvam", sarvam_chat_model="sarvam-105b-conversations", sarvam_api_key="test"
    ))
    prompts = []

    async def fake_model_reply(messages):
        prompts.append(messages)
        return "This is a reminder for your appointment. What would you like to know?"

    monkeypatch.setattr(conversation, "_model_reply", fake_model_reply)
    call = client.post("/v1/test-calls", json={"language": "en-IN"}).json()
    first = client.post(f"/v1/test-calls/{call['id']}/turn", json={"text": "What is this about?"}).json()
    assert first["model"] == "sarvam-105b-conversations"
    assert first["call"]["status"] == "connected"
    second = client.post(f"/v1/test-calls/{call['id']}/turn", json={"text": "Tell me more"}).json()
    assert any(message["content"] == "What is this about?" for message in prompts[-1])
    assert any(message["content"] == first["reply"] for message in prompts[-1])
    assert second["call"]["status"] == "connected"
    confirmed = client.post(f"/v1/test-calls/{call['id']}/turn", json={"text": "Yes, confirm it"}).json()
    assert confirmed["outcome"] == "confirmed"
    assert confirmed["model"] is None
    assert len(prompts) == 2
    assert respond("intro", "Yesterday I said yes").outcome is None
    assert respond("intro", "What does reschedule mean?").outcome is None
    assert respond("intro", "हाँ", "hi-IN").outcome == "confirmed"
    assert respond("intro", "Yeah I will watch serials at seven").outcome == "confirmed"


def test_ai_unavailable_uses_bounded_reply(monkeypatch):
    monkeypatch.setattr(conversation, "settings", SimpleNamespace(
        conversation_mode="sarvam", sarvam_chat_model="sarvam-105b-conversations", sarvam_api_key="test"
    ))

    async def unavailable(_messages):
        raise httpx.TimeoutException("synthetic timeout")

    monkeypatch.setattr(conversation, "_model_reply", unavailable)
    call = client.post("/v1/test-calls", json={"language": "en-IN"}).json()
    result = client.post(f"/v1/test-calls/{call['id']}/turn", json={"text": "Tell me more"}).json()
    assert result["model"] is None
    assert result["call"]["status"] == "connected"
    assert result["reply"].endswith("?")


def test_voice_lab_can_switch_ai_per_turn(monkeypatch):
    monkeypatch.setattr(conversation, "settings", SimpleNamespace(
        conversation_mode="sarvam", sarvam_chat_model="sarvam-105b-conversations", sarvam_api_key="test"
    ))
    prompts = []

    async def fake_model_reply(messages):
        prompts.append(messages)
        return "I can explain this reminder."

    monkeypatch.setattr(conversation, "_model_reply", fake_model_reply)
    call = client.post("/v1/test-calls", json={"language": "en-IN"}).json()
    url = f"/v1/test-calls/{call['id']}/turn"
    off = client.post(url, json={"text": "What is this about?", "conversation_mode": "deterministic"})
    assert off.status_code == 200
    assert off.json()["model"] is None
    assert not prompts
    on = client.post(url, json={"text": "Tell me more", "conversation_mode": "sarvam"})
    assert on.status_code == 200
    assert on.json()["model"] == "sarvam-105b-conversations"
    assert len(prompts) == 1
    assert any(message["content"] == "What is this about?" for message in prompts[0])
    assert client.post(url, json={"text": "hello", "conversation_mode": "invalid"}).status_code == 422


def _queued_attempt() -> str:
    with Session(engine) as db:
        contact = Contact(external_id=f"fallback-{uuid4()}", name="Synthetic",
                          phone="+919876543210", reminder_label="a check-in", reminder_at="tomorrow",
                          consent_source="synthetic test", consent_at="2026-10-01T12:00:00+05:30")
        campaign = Campaign(name="Fallback test", status="active", start_hour=0, end_hour=24)
        db.add_all([contact, campaign])
        db.flush()
        attempt = CallAttempt(contact_id=contact.id, campaign_id=campaign.id,
                              idempotency_key=f"fallback-{uuid4()}")
        db.add(attempt)
        db.flush()
        db.add(OutboxEvent(attempt_id=attempt.id, kind="dial", payload="{}"))
        db.commit()
        return attempt.id


def test_exotel_explicit_rejection_uses_twilio_once(monkeypatch):
    attempt_id = _queued_attempt()
    monkeypatch.setattr(worker, "SessionLocal", lambda: Session(engine))
    monkeypatch.setattr(worker, "settings", SimpleNamespace(call_mode="exotel", twilio_fallback_enabled=True,
        max_global_concurrent=20))
    monkeypatch.setattr(worker, "_allow_rate", lambda: True)
    monkeypatch.setattr(worker, "place_call", lambda *_: (_ for _ in ()).throw(
        ExotelError("explicit 429", safe_to_fallback=True)))
    twilio_calls = []
    monkeypatch.setattr(worker, "place_twilio_call", lambda phone, aid: twilio_calls.append((phone, aid)) or "CAfallback")
    assert worker.process_one() is True
    with Session(engine) as db:
        attempt = db.get(CallAttempt, attempt_id)
        assert attempt.provider_sid == "twilio:CAfallback"
        assert attempt.status == "dialing"
        assert db.scalar(select(AuditEvent).where(AuditEvent.target_id == attempt_id,
                                                  AuditEvent.action == "dial.fallback_twilio"))
    assert twilio_calls == [("+919876543210", attempt_id)]


def test_exotel_uncertain_result_never_dials_twilio(monkeypatch):
    attempt_id = _queued_attempt()
    monkeypatch.setattr(worker, "SessionLocal", lambda: Session(engine))
    monkeypatch.setattr(worker, "settings", SimpleNamespace(call_mode="exotel", twilio_fallback_enabled=True,
        max_global_concurrent=20))
    monkeypatch.setattr(worker, "_allow_rate", lambda: True)
    monkeypatch.setattr(worker, "place_call", lambda *_: (_ for _ in ()).throw(ExotelError("timeout")))
    twilio_calls = []
    monkeypatch.setattr(worker, "place_twilio_call", lambda *_: twilio_calls.append(True))
    assert worker.process_one() is True
    with Session(engine) as db:
        attempt = db.get(CallAttempt, attempt_id)
        assert attempt.status == "needs_review"
        assert attempt.provider_sid is None
    assert not twilio_calls


def test_twilio_twiml_and_signed_callback(monkeypatch):
    monkeypatch.setattr(twilio_provider, "settings", SimpleNamespace(
        twilio_stream_url="wss://example.test/ws/twilio", twilio_auth_token="synthetic-token"))
    xml = twilio_provider.stream_twiml("attempt-123")
    assert 'url="wss://example.test/ws/twilio"' in xml
    assert 'name="attempt_id" value="attempt-123"' in xml
    attempt_id = _queued_attempt()
    with Session(engine) as db:
        attempt = db.get(CallAttempt, attempt_id)
        attempt.provider_sid = "twilio:CAcallback"
        attempt.status = "dialing"
        db.commit()
    monkeypatch.setattr(main_module, "settings", SimpleNamespace(
        public_base_url="https://example.test", twilio_account_sid="ACsynthetic"))
    form = {"CallSid": "CAcallback", "AccountSid": "ACsynthetic", "CallStatus": "completed"}
    payload = "https://example.test/v1/webhooks/twilio" + "".join(key + form[key] for key in sorted(form))
    signature = base64.b64encode(hmac.new(b"synthetic-token", payload.encode(), hashlib.sha1).digest()).decode()
    assert client.post("/v1/webhooks/twilio", data=form).status_code == 401
    response = client.post("/v1/webhooks/twilio", data=form, headers={"X-Twilio-Signature": signature})
    assert response.status_code == 200
    with Session(engine) as db:
        assert db.get(CallAttempt, attempt_id).status == "completed"


def test_twilio_signed_media_stream_uses_shared_pipeline(monkeypatch):
    config = SimpleNamespace(twilio_fallback_enabled=True, twilio_stream_url="wss://example.test/ws/twilio",
                             twilio_auth_token="synthetic-token", twilio_account_sid="ACsynthetic")
    monkeypatch.setattr(voice_agent, "settings", config)
    monkeypatch.setattr(twilio_provider, "settings", config)
    monkeypatch.setattr(voice_agent, "SessionLocal", lambda: Session(engine))
    attempt_id = _queued_attempt()
    with Session(engine) as db:
        attempt = db.get(CallAttempt, attempt_id)
        attempt.provider_sid = "twilio:CAsynthetic"
        attempt.status = "dialing"
        db.commit()
    observed = []

    async def fake_pipeline(_websocket, session_id, greeting, language, serializer):
        observed.append((session_id, greeting, language, type(serializer).__name__))

    monkeypatch.setattr(voice_agent, "_run_pipeline", fake_pipeline)
    signature = base64.b64encode(hmac.new(b"synthetic-token", config.twilio_stream_url.encode(), hashlib.sha1).digest()).decode()
    with TestClient(voice_agent.app) as media_client:
        with media_client.websocket_connect("/ws/twilio", headers={"X-Twilio-Signature": signature}) as stream:
            stream.send_text(json.dumps({"event": "connected"}))
            stream.send_text(json.dumps({"event": "start", "streamSid": "MZsynthetic", "start": {
                "streamSid": "MZsynthetic", "callSid": "CAsynthetic", "accountSid": "ACsynthetic",
                "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000},
                "customParameters": {"attempt_id": attempt_id},
            }}))
    assert len(observed) == 1
    assert observed[0][1] and observed[0][2] == "hi-IN" and observed[0][3] == "TwilioFrameSerializer"


def test_pipecat_vad_and_turn_processors_construct():
    from pipecat.audio.vad.silero import SileroVADAnalyzer
    from pipecat.audio.vad.vad_analyzer import VADParams
    from pipecat.processors.audio.vad_processor import VADProcessor
    from pipecat.turns.user_start.vad_user_turn_start_strategy import VADUserTurnStartStrategy
    from pipecat.turns.user_stop.speech_timeout_user_turn_stop_strategy import SpeechTimeoutUserTurnStopStrategy
    from pipecat.turns.user_turn_processor import UserTurnProcessor
    from pipecat.turns.user_turn_strategies import UserTurnStrategies

    vad = VADProcessor(vad_analyzer=SileroVADAnalyzer(params=VADParams(
        confidence=0.7, start_secs=0.2, stop_secs=0.6, min_volume=0.6)))
    turns = UserTurnProcessor(user_turn_strategies=UserTurnStrategies(
        start=[VADUserTurnStartStrategy(enable_interruptions=True)],
        stop=[SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=0.6, wait_for_transcript=True)]))
    assert isinstance(vad, VADProcessor) and isinstance(turns, UserTurnProcessor)
