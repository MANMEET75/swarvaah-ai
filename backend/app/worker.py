"""Capacity-aware dial worker. Simulation is the default and never places a phone call."""
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from .config import settings
from .core import add_turn, start_session
from .db import Base, SessionLocal, engine
from .exotel import ExotelError, place_call
from .models import AuditEvent, CallAttempt, Campaign, Contact, OutboxEvent
from .twilio_provider import TwilioError, place_call as place_twilio_call


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("swarvaah.worker")


def _allow_rate() -> bool:
    try:
        import redis
        client = redis.Redis.from_url(settings.redis_url, socket_timeout=0.2)
        key = "swarvaah:dial:" + datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y%m%d%H%M")
        current = client.incr(key)
        if current == 1:
            client.expire(key, 90)
        return current <= settings.max_dials_per_minute
    except Exception:
        # Fail closed for live calls; local simulation can run without Redis.
        return settings.call_mode == "simulation"


def process_one() -> bool:
    with SessionLocal() as db:
        event = db.scalar(select(OutboxEvent)
                          .join(CallAttempt, OutboxEvent.attempt_id == CallAttempt.id)
                          .join(Campaign, CallAttempt.campaign_id == Campaign.id)
                          .where(OutboxEvent.sent_at.is_(None), Campaign.status != "paused")
                          .order_by(OutboxEvent.created_at)
                          .with_for_update(of=OutboxEvent, skip_locked=True))
        if not event:
            return False
        attempt = db.get(CallAttempt, event.attempt_id)
        if not attempt or attempt.status != "queued":
            event.sent_at = datetime.now(ZoneInfo("UTC"))
            db.commit()
            return True
        campaign = db.get(Campaign, attempt.campaign_id)
        contact = db.get(Contact, attempt.contact_id)
        if not campaign or not contact or campaign.status == "stopped":
            attempt.status = "cancelled"
            attempt.reason = "Campaign stopped"
            event.sent_at = datetime.now(ZoneInfo("UTC"))
            db.commit()
            return True
        if contact.suppressed or not contact.consent_source:
            attempt.status = "suppressed"
            attempt.reason = "Suppressed or missing consent"
            event.sent_at = datetime.now(ZoneInfo("UTC"))
            db.commit()
            return True
        hour = datetime.now(ZoneInfo("Asia/Kolkata")).hour
        if not campaign.start_hour <= hour < campaign.end_hour and settings.call_mode == "exotel":
            return False
        active_global = db.scalar(select(func.count()).select_from(CallAttempt).where(CallAttempt.status.in_(["dialing", "connected"]))) or 0
        active_campaign = db.scalar(select(func.count()).select_from(CallAttempt).where(CallAttempt.campaign_id == campaign.id, CallAttempt.status.in_(["dialing", "connected"]))) or 0
        if active_global >= settings.max_global_concurrent or active_campaign >= campaign.max_concurrent:
            return False
        if not _allow_rate():
            return False
        attempt.status = "dialing"
        db.commit()
        try:
            if settings.call_mode == "simulation":
                session = start_session(db, contact, attempt)
                # Deterministic simulation gives a useful dashboard without external calls.
                add_turn(db, session, contact, "yes" if contact.language == "en-IN" else "हाँ")
            else:
                try:
                    attempt.provider_sid = place_call(contact.phone, attempt.id)
                except ExotelError as exc:
                    if not (exc.safe_to_fallback and settings.twilio_fallback_enabled):
                        raise
                    attempt.provider_sid = "twilio:" + place_twilio_call(contact.phone, attempt.id)
                    attempt.reason = "Exotel rejected dial; Twilio fallback selected"
                    db.add(AuditEvent(action="dial.fallback_twilio", target_id=attempt.id,
                                      detail="Exotel explicitly rejected the request"))
                db.commit()
            event.sent_at = datetime.now(ZoneInfo("UTC"))
            db.commit()
        except ExotelError as exc:
            attempt.status = "failed" if exc.safe_to_fallback else "needs_review"
            attempt.reason = ("Exotel rejected dial" if exc.safe_to_fallback else "Exotel outcome uncertain; check provider before retry")
            event.sent_at = datetime.now(ZoneInfo("UTC"))
            db.commit()
            logger.error("Attempt %s: %s", attempt.id, attempt.reason)
        except TwilioError:
            attempt.status = "needs_review"
            attempt.reason = "Twilio fallback outcome uncertain; check provider before retry"
            event.sent_at = datetime.now(ZoneInfo("UTC"))
            db.commit()
            logger.error("Attempt %s: %s", attempt.id, attempt.reason)
        return True


def main() -> None:
    # Local Compose can start the worker before the API. Production uses migrations.
    Base.metadata.create_all(bind=engine)
    logger.info("Swarvaah dial worker started in %s mode", settings.call_mode)
    while True:
        did_work = process_one()
        if not did_work:
            time.sleep(2)


if __name__ == "__main__":
    main()
