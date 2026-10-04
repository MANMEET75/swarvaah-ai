# Production gates

This repository is a runnable local proof and a phone-pilot integration, not a 50,000-attempt/day deployment. These are the concrete gates before using real customer data or scaling traffic:

1. **Identity and authorization:** OIDC login, server-side session, role checks on every control action, CSRF controls, tenant isolation decision, and audit actor identity. The current demo UI has no login; the non-demo API has only one administrative key.
2. **Data protection:** encryption at rest and in transit, India-only contractual processing by every provider, KMS key policy, retention and deletion jobs, access/export workflow, recording approval, and PII redaction in traces.
3. **Calling compliance:** verify service-vs-promotional classification, sender registration, permitted route, DND/consent requirements, approved calling hours, and opt-out handling with carrier and counsel. Test with approved team numbers first.
4. **Carrier readiness:** Exotel's written API and capacity confirmation, outbound and stream quotas, callback signature or network authentication, caller ID, recorded failure handling, idempotent retry semantics, and failover design.
5. **Voice quality:** measure real phone audio in Hindi, English, and natural code-switching; names, numbers, noise, overlap, interruption, silence; median and p95 end-of-turn to first audible response. The current Pipecat integration has not been phone-tested.
6. **Reliable work dispatch:** move from a database-polled outbox to a durable delivery queue with explicit acknowledgement, visibility timeout, dead-letter queue, and replay controls. SQS is planned but not wired into this revision.
7. **Budget and billing:** enforce an estimated reservation before dial, reconcile actual Exotel/STT/TTS/model/compute cost, stop a campaign at its cap, and report cost per successful reminder. The current budget field is informational.
8. **High availability:** tested migration process, multi-AZ database and worker deployment, autoscaling, backups with restore drills, tracing, SLO alerts, load/soak/failure tests, and a kill switch exercised under load.

## Phone pilot checklist

- Keep `LIVE_DIAL_ENABLED=false` until team numbers, provider account, and real-time service quotas are approved.
- Confirm the exact Exotel Connect Voice AI request and callback fields on the account. Provider payloads can vary by product provisioning.
- Deploy `app.voice_agent` separately behind public India-hosted TLS/WSS and protect it from arbitrary client connections.
- Set restrictive `MAX_DIALS_PER_MINUTE` and `MAX_GLOBAL_CONCURRENT`, monitor logs, and place one call at a time at first.
- Verify contact suppression and failure reasons after disconnects and duplicate callbacks.

## Inbound extension

`CallSession.direction` and the voice pipeline are direction-neutral. Inbound needs number inventory, signed incoming webhooks, routing rules, consent/notice policy, agent queues, and a corresponding UI. Those features are not implemented in v0.1.
