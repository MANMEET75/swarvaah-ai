import hmac
import json
from datetime import datetime
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
import httpx
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select, text
from sqlalchemy.orm import Session

from .config import settings
from .core import import_csv, launch_campaign, masked_phone, metrics, start_session
from .conversation import add_conversation_turn
from .db import Base, engine, get_db
from .models import AuditEvent, CallAttempt, CallEvent, CallSession, Campaign, Contact, OutboxEvent
from .pilot_context import FESTIVAL_SALE_DEMO, SAVED_REMINDER
from .actnoww import SCENARIO as ACTNOWW_SUBSCRIPTION, SCRIPT as ACTNOWW_SCRIPT, CAMPAIGN_NAME as ACTNOWW_CAMPAIGN_NAME
from .exotel import normalize_status
from .twilio_provider import normalize_status as normalize_twilio_status, signed_request as twilio_signed_request
from .voice_lab import synthesize, transcribe


@asynccontextmanager
async def lifespan(_app: FastAPI):
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(title="Swarvaah AI API", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH"],
    allow_headers=["Content-Type", "X-API-Key"],
)


def authorize(x_api_key: str | None = Header(default=None)) -> None:
    if settings.mode != "demo" and not hmac.compare_digest(x_api_key or "", settings.admin_api_key):
        raise HTTPException(401, "Invalid API key")


class CampaignIn(BaseModel):
    name: str = Field(min_length=3, max_length=160)
    language: str = "hi-IN"
    voice: str = "priya"
    script: str = "service_reminder_v1"
    start_hour: int = Field(default=9, ge=0, le=23)
    end_hour: int = Field(default=18, ge=1, le=24)
    max_concurrent: int = Field(default=5, ge=1, le=10000)
    retry_limit: int = Field(default=0, ge=0, le=3)
    budget_inr: float = Field(default=100, gt=0)


class TestCallIn(BaseModel):
    language: str = "hi-IN"
    name: str = "Ananya"
    reminder_label: str = "your appointment"
    reminder_at: str = "tomorrow at 10:00 AM"


class PilotCallIn(BaseModel):
    scenario: Literal["saved_reminder", "festival_sale_demo", "actnoww_subscription"] = SAVED_REMINDER
    promotional_consent_confirmed: bool = False


class TurnIn(BaseModel):
    text: str = Field(min_length=1, max_length=1000)
    conversation_mode: Literal["sarvam", "deterministic"] | None = None


class SpeakIn(BaseModel):
    text: str = Field(min_length=1, max_length=1000)
    language: str = Field(pattern="^(hi-IN|en-IN)$")


class OutcomeCorrection(BaseModel):
    outcome: str = Field(pattern="^(confirmed|declined|reschedule_requested|human_callback|opted_out|unknown)$")
    reason: str = Field(min_length=5, max_length=500)


def campaign_json(c: Campaign) -> dict:
    return {"id": c.id, "name": c.name, "status": c.status, "language": c.language,
            "voice": c.voice, "script": c.script, "script_version": c.script_version,
            "start_hour": c.start_hour, "end_hour": c.end_hour,
            "max_concurrent": c.max_concurrent, "retry_limit": c.retry_limit,
            "budget_inr": c.budget_inr, "created_at": c.created_at.isoformat()}


@app.get("/health")
def health() -> dict:
    return {"ok": True, "mode": settings.mode, "call_mode": settings.call_mode,
            "conversation_mode": settings.conversation_mode,
            "conversation_model": settings.sarvam_chat_model if settings.conversation_mode == "sarvam" else None}


@app.get("/v1/overview", dependencies=[Depends(authorize)])
def overview(db: Session = Depends(get_db)) -> dict:
    return metrics(db)


@app.post("/v1/imports", dependencies=[Depends(authorize)])
async def upload_contacts(file: UploadFile = File(...), db: Session = Depends(get_db)) -> dict:
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(400, "Upload a CSV file")
    raw = await file.read(10_000_001)
    if len(raw) > 10_000_000:
        raise HTTPException(413, "CSV exceeds 10 MB")
    try:
        return import_csv(db, raw)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/v1/contacts", dependencies=[Depends(authorize)])
def contacts(page: int = Query(1, ge=1), size: int = Query(25, ge=1, le=100), db: Session = Depends(get_db)) -> dict:
    rows = db.scalars(select(Contact).order_by(desc(Contact.created_at)).offset((page - 1) * size).limit(size)).all()
    return {"items": [{"id": c.id, "external_id": c.external_id, "name": c.name,
                       "phone": masked_phone(c.phone), "reminder_label": c.reminder_label,
                       "reminder_at": c.reminder_at, "language": c.language,
                       "consent_source": c.consent_source, "suppressed": c.suppressed,
                       "pilot_self_test": settings.mode == "pilot" and c.phone == settings.pilot_self_test_phone}
                      for c in rows],
            "page": page, "size": size}


@app.post("/v1/contacts/{contact_id}/suppress", dependencies=[Depends(authorize)])
def suppress(contact_id: str, db: Session = Depends(get_db)) -> dict:
    contact = db.get(Contact, contact_id)
    if not contact:
        raise HTTPException(404, "Contact not found")
    contact.suppressed = True
    db.add(AuditEvent(action="contact.suppress", target_id=contact.id))
    db.commit()
    return {"suppressed": True}


@app.post("/v1/contacts/{contact_id}/restore-self-test", dependencies=[Depends(authorize)])
def restore_self_test(contact_id: str, db: Session = Depends(get_db)) -> dict:
    """Undo a manual pilot suppression of the operator's own test number only."""
    contact = db.get(Contact, contact_id)
    if not contact:
        raise HTTPException(404, "Contact not found")
    if (settings.mode != "pilot" or not settings.pilot_self_test_phone or
        contact.phone != settings.pilot_self_test_phone):
        raise HTTPException(403, "Only the configured self-test number can be restored here")
    if not contact.consent_source or not contact.consent_at:
        raise HTTPException(409, "Recorded test-call consent is required")
    last_suppression = db.scalar(select(AuditEvent).where(
        AuditEvent.target_id == contact.id,
        AuditEvent.action.in_(["contact.suppress", "contact.opt_out"])
    ).order_by(desc(AuditEvent.created_at), desc(AuditEvent.id)).limit(1))
    if last_suppression and last_suppression.action == "contact.opt_out":
        raise HTTPException(409, "A caller opt-out requires new documented consent")
    contact.suppressed = False
    db.add(AuditEvent(action="contact.restore_self_test", target_id=contact.id,
                      detail="Operator restored own synthetic test number"))
    db.commit()
    return {"suppressed": False}


@app.post("/v1/contacts/{contact_id}/pilot-call", dependencies=[Depends(authorize)])
def pilot_call(contact_id: str, data: PilotCallIn | None = None, db: Session = Depends(get_db)) -> dict:
    """Queue one contact for a controlled local phone pilot; never launch a whole audience."""
    if settings.mode != "pilot" or settings.call_mode != "exotel" or not settings.live_dial_enabled:
        raise HTTPException(403, "Single-contact pilot calls are unavailable in this environment")
    data = data or PilotCallIn()
    from datetime import timezone
    from zoneinfo import ZoneInfo

    now_utc = datetime.now(timezone.utc)
    hour = now_utc.astimezone(ZoneInfo("Asia/Kolkata")).hour
    if not 9 <= hour < 21:
        raise HTTPException(409, "Pilot calls are permitted only from 09:00 to 21:00 IST")
    try:
        response = httpx.get(f"{settings.public_base_url.rstrip('/')}/health", timeout=5)
        response.raise_for_status()
        if response.json().get("mode") != "pilot":
            raise ValueError("Public route does not reach the pilot API")
    except (httpx.HTTPError, ValueError):
        raise HTTPException(503, "Public phone gateway is unavailable") from None
    # Serialize the single-call admission check across concurrent UI requests.
    if db.bind and db.bind.dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(76294701)"))
    contact = db.get(Contact, contact_id)
    if not contact:
        raise HTTPException(404, "Contact not found")
    if contact.suppressed or not contact.consent_source or not contact.consent_at:
        raise HTTPException(409, "Contact is suppressed or lacks recorded consent")
    if data.scenario == FESTIVAL_SALE_DEMO and contact.phone != settings.pilot_self_test_phone:
        raise HTTPException(403, "Synthetic brand scenario is restricted to the configured self-test phone")
    if data.scenario == ACTNOWW_SUBSCRIPTION:
        if contact.phone != settings.pilot_self_test_phone:
            raise HTTPException(403, "Actnoww pilot calls are restricted to the configured self-test phone")
        if not data.promotional_consent_confirmed:
            raise HTTPException(409, "Confirm this number opted in to the Actnoww promotional call")
    active = db.scalar(select(func.count()).select_from(CallAttempt).where(
        CallAttempt.status.in_(["queued", "dialing", "connected"]))) or 0
    if active:
        raise HTTPException(409, "Another call is queued or active; finish it before starting a pilot call")
    if data.scenario == ACTNOWW_SUBSCRIPTION:
        campaign = db.scalar(select(Campaign).where(Campaign.name == ACTNOWW_CAMPAIGN_NAME,
                                                    Campaign.script == ACTNOWW_SCRIPT,
                                                    Campaign.status == "draft"))
        if not campaign:
            raise HTTPException(409, "Create the Actnoww campaign draft before calling")
        next_number = (db.scalar(select(func.max(CallAttempt.attempt_number)).where(
            CallAttempt.campaign_id == campaign.id, CallAttempt.contact_id == contact.id)) or 0) + 1
    else:
        campaign = Campaign(name=f"Pilot call · {contact.external_id}", status="active",
                            language=contact.language, max_concurrent=1,
                            start_hour=9, end_hour=21, retry_limit=0, budget_inr=100.0)
        db.add(campaign)
        db.flush()
        next_number = 1
    attempt = CallAttempt(campaign_id=campaign.id, contact_id=contact.id,
                          attempt_number=next_number,
                          idempotency_key=f"pilot:{campaign.id}:{contact.id}:{next_number}")
    db.add(attempt)
    db.flush()
    db.add(OutboxEvent(kind="dial_requested", attempt_id=attempt.id,
                       payload=json.dumps({"attempt_id": attempt.id, "scenario": data.scenario})))
    db.add(AuditEvent(action="pilot.call_queued", target_id=attempt.id,
                      detail=f"contact_id={contact.id};scenario={data.scenario}"))
    if data.scenario == ACTNOWW_SUBSCRIPTION:
        db.add(AuditEvent(action="consent.promotional_self_test", target_id=contact.id,
                          detail=f"Actnoww operator confirmation;attempt_id={attempt.id}"))
    db.commit()
    return {"attempt_id": attempt.id, "status": "queued"}


@app.post("/v1/campaigns", dependencies=[Depends(authorize)])
def create_campaign(data: CampaignIn, db: Session = Depends(get_db)) -> dict:
    if data.language not in {"hi-IN", "en-IN"} or data.end_hour <= data.start_hour:
        raise HTTPException(400, "Check language and calling hours")
    campaign = Campaign(**data.model_dump())
    db.add(campaign)
    db.flush()
    db.add(AuditEvent(action="campaign.create", target_id=campaign.id))
    db.commit()
    return campaign_json(campaign)


@app.post("/v1/campaigns/actnoww-draft", dependencies=[Depends(authorize)])
def create_actnoww_draft(db: Session = Depends(get_db)) -> dict:
    """Create the named campaign without launching a marketing audience."""
    existing = db.scalar(select(Campaign).where(Campaign.name == ACTNOWW_CAMPAIGN_NAME,
                                                 Campaign.script == ACTNOWW_SCRIPT,
                                                 Campaign.status == "draft"))
    if existing:
        return campaign_json(existing)
    campaign = Campaign(name=ACTNOWW_CAMPAIGN_NAME, script=ACTNOWW_SCRIPT, status="draft",
                        language="en-IN", voice="priya", start_hour=9, end_hour=21,
                        max_concurrent=1, retry_limit=0, budget_inr=100)
    db.add(campaign)
    db.flush()
    db.add(AuditEvent(action="campaign.create", target_id=campaign.id,
                      detail="Actnoww single-contact promotional pilot; no broad launch"))
    db.commit()
    return campaign_json(campaign)


@app.get("/v1/campaigns", dependencies=[Depends(authorize)])
def campaigns(db: Session = Depends(get_db)) -> dict:
    rows = db.scalars(select(Campaign).order_by(desc(Campaign.created_at)).limit(100)).all()
    return {"items": [campaign_json(c) for c in rows]}


@app.get("/v1/campaigns/{campaign_id}/metrics", dependencies=[Depends(authorize)])
def campaign_metrics(campaign_id: str, db: Session = Depends(get_db)) -> dict:
    campaign = db.get(Campaign, campaign_id)
    if not campaign:
        raise HTTPException(404, "Campaign not found")
    attempts = db.scalars(select(CallAttempt).where(CallAttempt.campaign_id == campaign_id)).all()
    sessions = db.scalars(select(CallSession).where(
        CallSession.attempt_id.in_([a.id for a in attempts]))).all() if attempts else []
    connected = len(sessions)
    counts: dict[str, int] = {}
    for session in sessions:
        if session.outcome:
            counts[session.outcome] = counts.get(session.outcome, 0) + 1
    counts["no_answer"] = sum(1 for a in attempts if a.reason and "no-answer" in a.reason.lower())
    return {
        "campaign_id": campaign_id, "attempts": len(attempts), "connected": connected,
        "outcomes": counts,
        "interested_parent_rate": (counts.get("interested", 0) + counts.get("subscription_requested", 0)) / connected if connected else None,
        "qualified_subscription_intent_rate": counts.get("subscription_requested", 0) / connected if connected else None,
        "opt_out_rate": counts.get("do_not_call", 0) / connected if connected else None,
        "completed_subscriptions": None,
        "callback_conversion_rate": None,
        "note": "Purchase completion and callback conversion require verified external events."
    }


@app.post("/v1/campaigns/{campaign_id}/launch", dependencies=[Depends(authorize)])
def launch(campaign_id: str, db: Session = Depends(get_db)) -> dict:
    campaign = db.get(Campaign, campaign_id)
    if not campaign:
        raise HTTPException(404, "Campaign not found")
    if campaign.script == ACTNOWW_SCRIPT:
        raise HTTPException(403, "Actnoww is limited to a consent-confirmed self-test call")
    try:
        return launch_campaign(db, campaign)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/v1/campaigns/{campaign_id}/pause", dependencies=[Depends(authorize)])
def pause(campaign_id: str, db: Session = Depends(get_db)) -> dict:
    campaign = db.get(Campaign, campaign_id)
    if not campaign:
        raise HTTPException(404, "Campaign not found")
    campaign.status = "paused"
    db.add(AuditEvent(action="campaign.pause", target_id=campaign.id))
    db.commit()
    return {"status": "paused"}


@app.post("/v1/campaigns/{campaign_id}/stop", dependencies=[Depends(authorize)])
def stop(campaign_id: str, db: Session = Depends(get_db)) -> dict:
    campaign = db.get(Campaign, campaign_id)
    if not campaign:
        raise HTTPException(404, "Campaign not found")
    campaign.status = "stopped"
    db.add(AuditEvent(action="campaign.stop", target_id=campaign.id))
    db.commit()
    return {"status": "stopped"}


@app.post("/v1/test-calls", dependencies=[Depends(authorize)])
def test_call(data: TestCallIn, db: Session = Depends(get_db)) -> dict:
    if settings.mode != "demo":
        raise HTTPException(403, "Browser simulation is enabled only in demo mode")
    if data.language not in {"hi-IN", "en-IN"}:
        raise HTTPException(400, "Unsupported language")
    contact = Contact(external_id=f"test-{datetime.now().timestamp()}", name=data.name, phone="+919999999999",
                      reminder_at=data.reminder_at, reminder_label=data.reminder_label,
                      language=data.language, consent_source="synthetic browser test", consent_at=datetime.now().isoformat())
    db.add(contact)
    db.commit()
    session = start_session(db, contact)
    return call_json(db, session)


@app.post("/v1/voice-lab/speak", dependencies=[Depends(authorize)])
async def voice_lab_speak(data: SpeakIn) -> Response:
    if settings.mode != "demo":
        raise HTTPException(403, "Voice lab is enabled only in demo mode")
    audio = await synthesize(data.text, data.language)
    return Response(audio, media_type="audio/wav", headers={"Cache-Control": "no-store"})


@app.post("/v1/voice-lab/transcribe", dependencies=[Depends(authorize)])
async def voice_lab_transcribe(file: UploadFile = File(...)) -> dict:
    if settings.mode != "demo":
        raise HTTPException(403, "Voice lab is enabled only in demo mode")
    audio = await file.read(2_000_001)
    text = await transcribe(audio, (file.content_type or "").split(";")[0])
    return {"text": text}


def call_json(db: Session, call: CallSession) -> dict:
    events = db.scalars(select(CallEvent).where(CallEvent.session_id == call.id).order_by(CallEvent.created_at, CallEvent.id)).all()
    attempt = db.get(CallAttempt, call.attempt_id) if call.attempt_id else None
    provider = "twilio" if attempt and (attempt.provider_sid or "").startswith("twilio:") else "exotel" if attempt else "browser"
    return {"id": call.id, "direction": call.direction, "status": call.status,
            "provider": provider,
            "state": call.workflow_state, "outcome": call.outcome, "language": call.language,
            "attempt_id": call.attempt_id, "created_at": call.created_at.isoformat(),
            "events": [{"kind": event.kind, "text": event.text,
                        "latency_ms": event.latency_ms, "created_at": event.created_at.isoformat()} for event in events]}


@app.post("/v1/test-calls/{session_id}/turn", dependencies=[Depends(authorize)])
async def test_turn(session_id: str, data: TurnIn, db: Session = Depends(get_db)) -> dict:
    if settings.mode != "demo":
        raise HTTPException(403, "Browser simulation is enabled only in demo mode")
    call = db.get(CallSession, session_id)
    if not call or call.attempt_id:
        raise HTTPException(404, "Test session not found")
    contact = db.get(Contact, call.contact_id)
    if not contact:
        raise HTTPException(404, "Test contact not found")
    try:
        result = await add_conversation_turn(db, call, contact, data.text, data.conversation_mode)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {**result, "call": call_json(db, call)}


@app.get("/v1/calls", dependencies=[Depends(authorize)])
def calls(page: int = Query(1, ge=1), size: int = Query(25, ge=1, le=100), db: Session = Depends(get_db)) -> dict:
    rows = db.scalars(select(CallSession).order_by(desc(CallSession.created_at)).offset((page - 1) * size).limit(size)).all()
    return {"items": [{"id": c.id, "direction": c.direction, "status": c.status,
                       "outcome": c.outcome, "language": c.language,
                       "created_at": c.created_at.isoformat()} for c in rows], "page": page, "size": size}


@app.get("/v1/calls/{session_id}", dependencies=[Depends(authorize)])
def call_detail(session_id: str, db: Session = Depends(get_db)) -> dict:
    call = db.get(CallSession, session_id)
    if not call:
        raise HTTPException(404, "Call not found")
    return call_json(db, call)


@app.post("/v1/calls/{session_id}/correct-outcome", dependencies=[Depends(authorize)])
def correct_outcome(session_id: str, data: OutcomeCorrection, db: Session = Depends(get_db)) -> dict:
    call = db.get(CallSession, session_id)
    if not call or call.status != "ended":
        raise HTTPException(400, "Only ended calls can be corrected")
    old = call.outcome
    call.outcome = data.outcome
    db.add(AuditEvent(action="call.correct_outcome", target_id=call.id,
                      detail=json.dumps({"previous": old, "new": data.outcome, "reason": data.reason})))
    db.commit()
    return {"outcome": call.outcome}


@app.get("/v1/audit", dependencies=[Depends(authorize)])
def audit(db: Session = Depends(get_db)) -> dict:
    rows = db.scalars(select(AuditEvent).order_by(desc(AuditEvent.created_at)).limit(100)).all()
    return {"items": [{"action": x.action, "target_id": x.target_id, "detail": x.detail,
                       "created_at": x.created_at.isoformat()} for x in rows]}


@app.post("/v1/webhooks/exotel/{secret}")
async def exotel_callback(secret: str, request: Request, db: Session = Depends(get_db)) -> dict:
    if not settings.exotel_callback_secret or not hmac.compare_digest(secret, settings.exotel_callback_secret):
        raise HTTPException(401, "Invalid callback")
    if "application/json" in request.headers.get("content-type", ""):
        data = await request.json()
    else:
        data = dict(await request.form())
    sid = data.get("CallSid") or data.get("Sid") or data.get("call_sid")
    if not sid:
        raise HTTPException(400, "Missing call SID")
    attempt = db.scalar(select(CallAttempt).where(CallAttempt.provider_sid == str(sid)))
    if not attempt:
        raise HTTPException(404, "Unknown call SID")
    attempt.status = normalize_status(str(data.get("Status") or data.get("CallStatus") or ""))
    attempt.reason = str(data.get("FailureReason") or data.get("Status") or data.get("CallStatus") or "")[:240] or None
    db.commit()
    return {"accepted": True}


@app.post("/v1/webhooks/twilio")
async def twilio_callback(request: Request, db: Session = Depends(get_db)) -> dict:
    form = {key: str(value) for key, value in (await request.form()).items()}
    public_url = f"{settings.public_base_url.rstrip('/')}{request.url.path}"
    if not twilio_signed_request(public_url, form, request.headers.get("X-Twilio-Signature", "")):
        raise HTTPException(401, "Invalid Twilio signature")
    if form.get("AccountSid") != settings.twilio_account_sid:
        raise HTTPException(403, "Unknown Twilio account")
    sid = form.get("CallSid", "")
    if not sid:
        raise HTTPException(400, "Missing call SID")
    attempt = db.scalar(select(CallAttempt).where(CallAttempt.provider_sid == f"twilio:{sid}"))
    if not attempt:
        raise HTTPException(404, "Unknown call SID")
    status = normalize_twilio_status(form.get("CallStatus", ""))
    if attempt.status not in {"completed", "failed", "cancelled"}:
        attempt.status = status
        if status == "failed":
            attempt.reason = "Twilio call did not complete"
        db.commit()
    return {"accepted": True}


@app.post("/v1/demo/seed", dependencies=[Depends(authorize)])
def seed_demo(db: Session = Depends(get_db)) -> dict:
    if settings.mode != "demo":
        raise HTTPException(403, "Demo only")
    sample = "external_id,name,phone,reminder_at,reminder_label,language,consent_source,consent_at\n" \
             "DEMO-001,Ananya Sharma,+919876543210,2026-10-08T10:00:00+05:30,annual check-in,hi-IN,appointment form,2026-10-01T12:00:00+05:30\n" \
             "DEMO-002,Rohan Mehta,+919876543211,2026-10-08T11:30:00+05:30,consultation,en-IN,booking form,2026-10-01T12:00:00+05:30\n" \
             "DEMO-003,Meera Iyer,+919876543212,2026-10-09T15:00:00+05:30,service appointment,hi-IN,booking form,2026-10-01T12:00:00+05:30\n"
    result = import_csv(db, sample.encode())
    if not db.scalar(select(Campaign.id).where(Campaign.name == "October service reminders")):
        db.add(Campaign(name="October service reminders", budget_inr=200, max_concurrent=3))
        db.commit()
    return result
