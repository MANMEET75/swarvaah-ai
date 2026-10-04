# Local operations runbook

## Start, inspect, stop

Run `docker compose up --build`, open `http://localhost:5173`, and check `http://localhost:8000/health`. View API and worker logs with `docker compose logs -f api worker`. Stop with `docker compose down`.

## A queued attempt does not progress

Check that the worker is running. Confirm the campaign is active, contact is not suppressed, and the queue has capacity. The worker leaves an attempt queued outside its IST calling window or when at a capacity limit. It writes a visible reason for suppression or provider failure. In demo simulation, the calling-window restriction is skipped for reproducible testing.

## Pause or stop traffic

Use the **Campaigns** controls. Pause prevents new outbox events from being dialed; stop is permanent for the campaign. Existing connected calls are not forcibly terminated in this revision. For an emergency phone pilot shutdown, also set `LIVE_DIAL_ENABLED=false` and stop the worker service.

## Backup and recovery

Local Compose state lives in `postgres_data`. `docker compose down -v` deletes it. For real deployment, use encrypted managed PostgreSQL with automated backups, PITR, and periodic restore tests. Keep recordings off by default unless explicitly approved.
