"""Capacity-aware dial worker. Simulation is the default and never places a phone call."""
import json
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from .config import settings
from .core import add_turn, start_session
from .db import Base, SessionLocal, engine
from .exotel import ExotelError, place_call
from .models import CallAttempt, Campaign, Contact, OutboxEvent


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
        event = db.scalar(select(OutboxEvent).where(OutboxEvent.sent_at.is_(None)).order_by(OutboxEvent.created_at).with_for_update(skip_locked=True))
        if not event:
            return False
        payload = json.loads(event.payload)
        attempt = db.get(CallAttempt, payload["attempt_id"])
        if not attempt or attempt.status != "queued":
            event.sent_at = datetime.now(ZoneInfo("UTC"))
            db.commit()
            return True
        campaign = db.get(Campaign, attempt.campaign_id)
        contact = db.get(Contact, attempt.contact_id)
        if not campaign or not contact or campaign.status in {"stopped", "paused"}:
            return False
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
                attempt.provider_sid = place_call(contact.phone, attempt.id)
                db.commit()
            event.sent_at = datetime.now(ZoneInfo("UTC"))
            db.commit()
        except ExotelError as exc:
            attempt.status = "failed"
            attempt.reason = str(exc)[:240]
            event.sent_at = datetime.now(ZoneInfo("UTC"))
            db.commit()
            logger.error("Attempt %s failed: %s", attempt.id, exc)
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
