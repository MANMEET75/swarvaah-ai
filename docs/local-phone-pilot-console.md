# Local phone pilot through an SSH tunnel

This runbook reproduces the local console and one-at-a-time outbound phone test. The API, web console, worker, database, Redis, and media gateway run on your computer. An SSH tunnel exposes only Caddy's narrow callback/media edge so Exotel can reach it. Exotel and Sarvam credits may be consumed. Use only a number whose owner agreed to the call. A free tunnel is for short tests, not reliable voice hosting.

## 1. Prepare the local files

From the repository root, create an ignored `.env.pilot` from `.env.example`. Do not copy a filled environment file from another machine or commit it:

```bash
cp .env.example .env.pilot
```

Edit `.env.pilot` and set:

| Variable | Pilot value |
| --- | --- |
| `SWARVAAH_MODE`, `CALL_MODE`, `LIVE_DIAL_ENABLED` | `pilot`, `exotel`, `true` |
| `CONVERSATION_MODE`, `SARVAM_CHAT_MODEL` | `sarvam`, `sarvam-105b-conversations` |
| `SARVAM_API_KEY` | Your Sarvam key, kept only in `.env.pilot` |
| `EXOTEL_API_BASE_URL` | `https://api.in.exotel.com` for an India account or `https://api.exotel.com` for a Singapore account |
| `EXOTEL_ACCOUNT_SID`, `EXOTEL_API_KEY`, `EXOTEL_API_TOKEN`, `EXOTEL_CALLER_ID` | Values from the approved Exotel account |
| `ADMIN_API_KEY`, `EXOTEL_CALLBACK_SECRET` | Separate random secrets; never share them in a URL, screenshot, or chat |
| `PILOT_SELF_TEST_PHONE` | Your own opted-in number in `+91` format; required for the Actnoww and synthetic sale options |
| `MAX_GLOBAL_CONCURRENT`, `MAX_DIALS_PER_MINUTE`, `TWILIO_FALLBACK_ENABLED` | `1`, `1`, `false` for the first test |
| `PUBLIC_BASE_URL`, `EXOTEL_STREAM_URL` | Set after the tunnel supplies a hostname |

The pilot Compose file mounts an ignored CA bundle. On a new checkout, create it from your machine's trusted CA bundle before starting Compose:

```bash
mkdir -p .pilot
python3 - <<'PY'
import shutil, ssl
source = ssl.get_default_verify_paths().cafile
if not source:
    raise SystemExit('No system CA bundle found; provide a trusted PEM bundle at .pilot/ca-bundle.pem')
shutil.copyfile(source, '.pilot/ca-bundle.pem')
PY
```

If your organization uses a TLS inspection root, add that **trusted** certificate to the local bundle. Do not disable certificate verification. `.env.pilot` and `.pilot/` are ignored by Git.

## 2. Start the local stack

```bash
docker compose -f compose.pilot.yaml --profile dial up -d --build db redis api gateway edge web worker
docker compose -f compose.pilot.yaml --profile dial ps
curl -fsS http://127.0.0.1:18080/health
```

The console is at `http://localhost:5173`. The API is bound to `127.0.0.1:18000`, and Caddy's restricted public edge to `127.0.0.1:18080`. The browser talks to the local Vite proxy, which attaches `ADMIN_API_KEY` server-side. **Never expose port 5173 or 18000 through the tunnel.** Caddy forwards only `/health`, signed Exotel callbacks, and signed Exotel WebSocket sessions.

## 3. Open the SSH tunnel

In a **separate terminal**, keep this command running:

```bash
ssh -o ServerAliveInterval=30 -R 80:localhost:18080 nokey@localhost.run
```

The [localhost.run SSH guide](https://localhost.run/docs/cli/) documents this reverse-port format. Copy the assigned public **HTTPS hostname** from the SSH output. In `.env.pilot`, set both addresses to that same hostname:

```dotenv
PUBLIC_BASE_URL=https://YOUR_ASSIGNED_HOST
EXOTEL_STREAM_URL=wss://YOUR_ASSIGNED_HOST/ws/exotel
```

Compose `restart` does **not** reload changed environment variables. Recreate the services that use these URLs:

```bash
docker compose -f compose.pilot.yaml --profile dial up -d --force-recreate api gateway worker
curl -fsS https://YOUR_ASSIGNED_HOST/health
```

The public health response must show `mode: pilot`. If the SSH process exits, the hostname may change and queued calls must wait until you set the new URLs and recreate the services. We previously saw a free SSH tunnel return 502 during a pilot; do not treat it as a stable media route. A temporary [Cloudflare Quick Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/do-more-with-tunnels/trycloudflare/) can be substituted with `cloudflared tunnel --url http://127.0.0.1:18080`; it also needs the same URL update and is not a production endpoint. For repeatable audio measurements, use the [stable public gateway guide](stable-gateway-pilot.md).

## 4. Prepare one contact and choose the call

Open **Contacts** in `http://localhost:5173`. Import or review one opted-in contact and check the masked phone suffix. The current CSV schema requires reminder fields even when the selected Actnoww campaign does not use them. `consent_source` and `consent_at` must truthfully describe the permission for the intended call. Importing a CSV never clears suppression. If you manually suppressed your own test number, **Restore self-test** can undo only that manual suppression; a caller opt-out needs new documented permission.

- **Saved service reminder:** choose the saved reminder context, review its text and time, then click **Call once**.
- **Synthetic Vijay Sales sale:** choose the fictional demo context. It is limited to `PILOT_SELF_TEST_PHONE` and makes no real-brand offer claims.
- **Actnoww Kids Learning Subscription:** in **Campaigns**, click **Create Actnoww draft** once. In **Contacts**, choose the Actnoww context and click **Call once** on your own number. Confirm promotional consent in the second dialog. This is a real Actnoww conversation, but the only approved facts currently supplied are early ABC/concept learning activities and a ₹999 price for one course. The assistant records subscription interest; it cannot complete payment or sign-up without an approved checkout flow.

The call endpoint checks the 09:00–21:00 IST window, consent, suppression, public health route, and absence of another queued or active call. The pilot can queue only one selected contact; the broad campaign **Launch** control is unavailable. Creating the Actnoww draft itself places no call. Exotel may still reject a trial-account recipient before a media session starts.

Watch **Live calls** and **Review** in the console. For diagnostics, use `docker compose -f compose.pilot.yaml logs --tail=100 worker gateway api` and check the `Exotel outbound media timing` summary; it records packet timing, not caller audio. The first approved test number completed a conversational call on 9 October 2026. A second test number received Exotel HTTP 403 before a call was created; the specific account restriction was not confirmed.

## 5. Stop safely

Stop the worker first so no further queued attempt is dialed, then close the SSH tunnel:

```bash
docker compose -f compose.pilot.yaml stop worker
docker compose -f compose.pilot.yaml --profile dial down
```

Do not add `-v` unless you intend to delete local call records. After a restart, repeat the tunnel URL and public-health checks before queuing another call. For persistent HTTPS/WSS service and cleaner latency comparisons, move the gateway to an approved public host rather than leaving a laptop tunnel running.
