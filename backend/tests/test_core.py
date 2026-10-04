import io

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.models import CallAttempt, Contact, OutboxEvent
from app.voice_agent import stream_signature
from app.exotel import ExotelError, extract_call_sid
from app import worker


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
