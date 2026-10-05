# Local operations runbook

## Start, inspect, stop

Run `docker compose up --build`, open `http://localhost:5173`, and check `http://localhost:8000/health`. View API and worker logs with `docker compose logs -f api worker`. Stop with `docker compose down`.

## A queued attempt does not progress

Check that the worker is running. Confirm the campaign is active, contact is not suppressed, and the queue has capacity. The worker leaves an attempt queued outside its IST calling window or when at a capacity limit. It writes a visible reason for suppression or provider failure. In demo simulation, the calling-window restriction is skipped for reproducible testing.

## Exotel fails or Twilio fallback is selected

Twilio is attempted only after Exotel explicitly rejects a dial request with HTTP 4xx and `TWILIO_FALLBACK_ENABLED=true`. An Exotel timeout or 5xx can represent an accepted call with a lost response; the worker marks the attempt `needs_review` and does not place a second call. Check the Exotel dashboard by contact, time, and attempt before any manual retry. If Twilio's create response is uncertain, also check Twilio before retrying. Successful Twilio attempts have `twilio:`-prefixed provider SIDs, a `dial.fallback_twilio` audit event, and a provider value on the call-review detail. Confirm the Twilio signed callback and WebSocket connection before counting a call as completed. To disable fallback immediately, set `TWILIO_FALLBACK_ENABLED=false` and restart the worker and gateway.

## Pause or stop traffic

Use the **Campaigns** controls. Pause prevents new outbox events from being dialed; stop is permanent for the campaign. Existing connected calls are not forcibly terminated in this revision. For an emergency phone pilot shutdown, also set `LIVE_DIAL_ENABLED=false` and stop the worker service. The Twilio fallback switch is separate and defaults to off.

## Backup and recovery

Local Compose state lives in `postgres_data`. `docker compose down -v` deletes it. For real deployment, use encrypted managed PostgreSQL with automated backups, PITR, and periodic restore tests. Keep recordings off by default unless explicitly approved.
