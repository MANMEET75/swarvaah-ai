# Stable public gateway for a one-number phone test

The localhost.run SSH tunnel is useful for a quick demonstration, but its free hostname changed and returned 502 during this pilot. A dedicated host makes the HTTPS and WSS address persistent and removes the local laptop and SSH tunnel from the media path. This single-host Compose setup is for a controlled test with a synthetic reminder, not a production capacity claim.

## Host prerequisites

- An approved Linux host with a static public IP, Docker Engine and Compose, and enough memory for PostgreSQL, Redis, Pipecat, Silero VAD, and the API. Place it in an approved processing region. For real Indian customer audio, confirm every provider's processing location before use.
- A domain you control with an A record pointing to the host. Open inbound TCP ports 80 and 443. Caddy obtains and renews the TLS certificate. Permit outbound HTTPS/WSS to Exotel and Sarvam.
- A private copy of this repository on the host. Do not copy local `.env`, `.env.pilot`, `.pilot/`, credentials, phone transcripts, or the pilot database.

## Configure and start

1. Copy `infra/gateway/env.example` to `.env.gateway` on the host. Fill the account values there, set `GATEWAY_DOMAIN` to the DNS name, and use the same name in `PUBLIC_BASE_URL` and `EXOTEL_STREAM_URL`. Generate independent, random values for `ADMIN_API_KEY`, `EXOTEL_CALLBACK_SECRET`, and `SWARVAAH_DB_PASSWORD`. The database password must use URL-safe characters because Compose places it in a database URL. Keep `.env.gateway` readable only by the operator.
2. Start only the data stores, control API, media gateway, and TLS edge:

   ```bash
   docker compose -f compose.gateway.yaml --env-file .env.gateway up -d --build db redis api gateway edge
   ```

3. Check `https://YOUR_DOMAIN/health` from outside the host. The public edge permits only health, signed Exotel callbacks, and signed Exotel WebSocket paths. Confirm the root URL and `/v1/contacts` return 404. Do not start the continuous dial worker for a one-number test.
4. Before dialing, make a synthetic WebSocket media probe and require at least one outbound `media` packet:

   ```bash
   docker compose -f compose.gateway.yaml --env-file .env.gateway exec -T api python -m app.pilot_probe --output /tmp/phone-path-baseline.wav
   ```

   The probe creates a stopped campaign and no outbox event, so it cannot place a phone call. Copy the WAV out of the container if you want to listen to the 8 kHz result. Check that the gateway can reach Sarvam chat and TTS. Verify the one approved recipient, active-call count zero, and exactly one queued outbox event. Invoke `process_one()` once; do not start `python -m app.worker` in a loop for this test. The call must occur inside its configured India calling window.
5. Review the call outcome, transcript, provider status, and `Exotel outbound media timing` log entry. It reports packet count, speech bursts, packets sent over 80 ms late, maximum excess interpacket gap, and maximum local WebSocket send duration. It does **not** measure delivery or playback at Exotel or the mobile network.

The worker is defined behind Compose's `dial` profile and does not start with step 2. Keep `MAX_GLOBAL_CONCURRENT=1`, `MAX_DIALS_PER_MINUTE=1`, and Twilio fallback disabled during a single-number pilot.

## Compare audio paths

Listen to the same synthetic greeting in Voice Lab, in the gateway's 8 kHz output, and on the phone. The Voice Lab REST playback is 22.05 kHz and bypasses Exotel; the gateway sample is 8 kHz PCM and bypasses the carrier. A clean Voice Lab sample with choppy gateway output points to the streaming or transcoding path. Clean gateway output with choppy phone playback points downstream of the gateway, such as WebSocket transport, Exotel playback, or the mobile network. Use packet timing to narrow that further. An 80 ms late-packet threshold is diagnostic, not a production SLO.

## Stop and remove

Stop the host with `docker compose -f compose.gateway.yaml --env-file .env.gateway down`. The PostgreSQL and Caddy volumes remain for review. Delete them only after preserving any needed call record and revoking the pilot credentials. Shut down the host and DNS when testing is complete so it does not keep incurring charges.
