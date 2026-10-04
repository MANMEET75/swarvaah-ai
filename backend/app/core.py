import csv
import io
import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import AuditEvent, CallAttempt, CallEvent, CallSession, Campaign, Contact, OutboxEvent
from .workflow import greeting, respond


REQUIRED_CSV = {"external_id", "name", "phone", "reminder_at", "reminder_label", "consent_source", "consent_at"}
INDIAN_PHONE = re.compile(r"^\+91[6-9]\d{9}$")


def masked_phone(phone: str) -> str:
    return phone[:3] + "•••••••" + phone[-2:] if len(phone) > 5 else "••••"


def import_csv(db: Session, raw: bytes) -> dict:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("CSV must be UTF-8") from exc
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or not REQUIRED_CSV.issubset(set(reader.fieldnames)):
        missing = REQUIRED_CSV - set(reader.fieldnames or [])
        raise ValueError(f"Missing columns: {', '.join(sorted(missing))}")
    accepted = 0
    updated = 0
    errors = []
    seen = set()
    for number, row in enumerate(reader, start=2):
        if number > 100_001:
            errors.append({"row": number, "error": "Upload limited to 100,000 rows"})
            break
        try:
            external_id = (row.get("external_id") or "").strip()
            phone = (row.get("phone") or "").strip()
            if not external_id or external_id in seen:
                raise ValueError("Empty or duplicate external_id in upload")
            seen.add(external_id)
            if not INDIAN_PHONE.fullmatch(phone):
                raise ValueError("Phone must use +91 followed by a valid 10-digit mobile number")
            consent_source = (row.get("consent_source") or "").strip()
            if not consent_source:
                raise ValueError("consent_source is required")
            consent_at = (row.get("consent_at") or "").strip()
            reminder_at = (row.get("reminder_at") or "").strip()
            datetime.fromisoformat(consent_at.replace("Z", "+00:00"))
            datetime.fromisoformat(reminder_at.replace("Z", "+00:00"))
            language = (row.get("language") or "hi-IN").strip()
            if language not in {"hi-IN", "en-IN"}:
                raise ValueError("language must be hi-IN or en-IN")
            name = (row.get("name") or "").strip()
            label = (row.get("reminder_label") or "").strip()
            if not name or not label:
                raise ValueError("name and reminder_label are required")
            contact = db.scalar(select(Contact).where(Contact.external_id == external_id))
            if contact:
                updated += 1
            else:
                contact = Contact(external_id=external_id)
                db.add(contact)
                accepted += 1
            contact.name = name
            contact.phone = phone
            contact.reminder_at = reminder_at
            contact.reminder_label = label
            contact.language = language
            contact.consent_source = consent_source
            contact.consent_at = consent_at
            contact.suppressed = contact.suppressed or (row.get("suppressed", "").lower() in {"true", "1", "yes"})
        except (ValueError, TypeError) as exc:
            errors.append({"row": number, "error": str(exc)})
    db.add(AuditEvent(action="contacts.import", target_id="csv", detail=json.dumps({"accepted": accepted, "updated": updated, "errors": len(errors)})))
    db.commit()
    return {"accepted": accepted, "updated": updated, "errors": errors[:100], "error_count": len(errors)}


def launch_campaign(db: Session, campaign: Campaign) -> dict:
    if campaign.status not in {"draft", "paused"}:
        raise ValueError("Only draft or paused campaigns can be launched")
    eligible = db.scalars(select(Contact).where(Contact.suppressed.is_(False))).all()
    queued = 0
    for contact in eligible:
        key = f"{campaign.id}:{contact.id}:1"
        if db.scalar(select(CallAttempt.id).where(CallAttempt.idempotency_key == key)):
            continue
        attempt = CallAttempt(campaign_id=campaign.id, contact_id=contact.id, idempotency_key=key)
        db.add(attempt)
        db.flush()
        db.add(OutboxEvent(kind="dial_requested", payload=json.dumps({"attempt_id": attempt.id})))
        queued += 1
    if queued == 0 and campaign.status == "draft":
        raise ValueError("No eligible contacts found")
    campaign.status = "active"
    db.add(AuditEvent(action="campaign.launch", target_id=campaign.id, detail=f"queued={queued}"))
    db.commit()
    return {"queued": queued, "status": campaign.status}


def start_session(db: Session, contact: Contact, attempt: CallAttempt | None = None) -> CallSession:
    session = CallSession(attempt_id=attempt.id if attempt else None, contact_id=contact.id,
                          direction="outbound", language=contact.language)
    db.add(session)
    db.flush()
    db.add(CallEvent(session_id=session.id, kind="assistant", text=greeting(contact.name, contact.reminder_label, contact.reminder_at, contact.language)))
    if attempt:
        attempt.status = "connected"
    db.commit()
    return session


def add_turn(db: Session, session: CallSession, contact: Contact, text: str) -> dict:
    if session.status == "ended":
        raise ValueError("Call already ended")
    turn = respond(session.workflow_state, text, session.language)
    db.add(CallEvent(session_id=session.id, kind="caller", text=text))
    db.add(CallEvent(session_id=session.id, kind="assistant", text=turn.reply))
    if turn.action:
        db.add(CallEvent(session_id=session.id, kind="action", text=turn.action))
    session.workflow_state = turn.state
    if turn.outcome:
        session.outcome = turn.outcome
        session.status = "ended"
        session.ended_at = datetime.now(ZoneInfo("UTC"))
        if turn.outcome == "opted_out":
            contact.suppressed = True
            db.add(AuditEvent(action="contact.opt_out", target_id=contact.id, detail="voice session"))
        if session.attempt_id:
            attempt = db.get(CallAttempt, session.attempt_id)
            if attempt:
                attempt.status = "completed"
    db.commit()
    return {"reply": turn.reply, "state": turn.state, "outcome": turn.outcome, "action": turn.action}


def metrics(db: Session) -> dict:
    counts = dict(db.execute(select(CallAttempt.status, func.count()).group_by(CallAttempt.status)).all())
    outcomes = dict(db.execute(select(CallSession.outcome, func.count()).where(CallSession.outcome.is_not(None)).group_by(CallSession.outcome)).all())
    return {
        "contacts": db.scalar(select(func.count()).select_from(Contact)) or 0,
        "suppressed": db.scalar(select(func.count()).select_from(Contact).where(Contact.suppressed.is_(True))) or 0,
        "campaigns": db.scalar(select(func.count()).select_from(Campaign)) or 0,
        "attempts": sum(counts.values()),
        "active_calls": counts.get("connected", 0) + counts.get("dialing", 0),
        "attempt_status": counts,
        "outcomes": outcomes,
        "spend_inr": round(db.scalar(select(func.sum(CallAttempt.cost_inr))) or 0, 2),
    }
