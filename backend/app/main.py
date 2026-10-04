import hmac
import json
from datetime import datetime
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from .config import settings
from .core import add_turn, import_csv, launch_campaign, masked_phone, metrics, start_session
from .db import Base, engine, get_db
from .models import AuditEvent, CallAttempt, CallEvent, CallSession, Campaign, Contact
from .exotel import normalize_status
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


class TurnIn(BaseModel):
    text: str = Field(min_length=1, max_length=1000)


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
    return {"ok": True, "mode": settings.mode, "call_mode": settings.call_mode}


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
                       "consent_source": c.consent_source, "suppressed": c.suppressed} for c in rows],
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


@app.get("/v1/campaigns", dependencies=[Depends(authorize)])
def campaigns(db: Session = Depends(get_db)) -> dict:
    rows = db.scalars(select(Campaign).order_by(desc(Campaign.created_at)).limit(100)).all()
    return {"items": [campaign_json(c) for c in rows]}


@app.post("/v1/campaigns/{campaign_id}/launch", dependencies=[Depends(authorize)])
def launch(campaign_id: str, db: Session = Depends(get_db)) -> dict:
    campaign = db.get(Campaign, campaign_id)
    if not campaign:
        raise HTTPException(404, "Campaign not found")
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
    return {"id": call.id, "direction": call.direction, "status": call.status,
            "state": call.workflow_state, "outcome": call.outcome, "language": call.language,
            "attempt_id": call.attempt_id, "created_at": call.created_at.isoformat(),
            "events": [{"kind": event.kind, "text": event.text,
                        "latency_ms": event.latency_ms, "created_at": event.created_at.isoformat()} for event in events]}


@app.post("/v1/test-calls/{session_id}/turn", dependencies=[Depends(authorize)])
def test_turn(session_id: str, data: TurnIn, db: Session = Depends(get_db)) -> dict:
    if settings.mode != "demo":
        raise HTTPException(403, "Browser simulation is enabled only in demo mode")
    call = db.get(CallSession, session_id)
    if not call or call.attempt_id:
        raise HTTPException(404, "Test session not found")
    contact = db.get(Contact, call.contact_id)
    if not contact:
        raise HTTPException(404, "Test contact not found")
    try:
        result = add_turn(db, call, contact, data.text)
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
    attempt.reason = str(data.get("FailureReason") or "")[:240] or None
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
