# Running zdrowskit in Docker (Linux host)

`compose.yaml` runs two containers:

- **`zdrowskit`:** the daemon, from `Dockerfile`. State lives in the
  `zdrowskit-home` volume at `/data` (`ZDROWSKIT_HOME`).
- **`tailscale`:** a sidecar that owns the network namespace and publishes the
  daemon's loopback receiver (`127.0.0.1:8787`) through Funnel using
  `funnel.json`. The receiver never binds a LAN interface.

The images are multi-arch (amd64 and arm64). Run every command below from the
repository root.

## 1. Secrets

**`.env` in the repository root** is passed to the daemon. Start from
`.env_example`:

- `TELEGRAM_BOT_TOKEN` is optional. Without it the daemon logs that Telegram is
  disabled and keeps importing.
- Provider API keys are optional. See ingestion-only mode below.

**`deploy/.env`** is read by compose for the sidecar only:

```dotenv
TS_AUTHKEY=tskey-auth-...
```

Create the key in the Tailscale admin console. Funnel must be allowed for the
node by the tailnet policy (`funnel` node attribute). See Tailscale's Funnel
docs.

## 2. Build and one-time setup

```bash
docker compose -f deploy/compose.yaml build
alias zk='docker compose -f deploy/compose.yaml run --rm zdrowskit uv run --no-sync python main.py'

zk setup --skip-env
zk profile add NAME --telegram-id ID --operator --units imperial
zk ingest setup        # prints the upload token ONCE: put it into Auto Export now
```

`ingest setup` runs in the daemon container, which has no Tailscale CLI, so it
cannot print the public URL or start Funnel. The sidecar does both: once it is
up (step 3), read the URL with

```bash
docker compose -f deploy/compose.yaml exec tailscale tailscale funnel status
```

In Auto Export, both automations (Metrics and Workouts) use that URL plus the
paths and settings from `docs/http-ingest.md` and `docs/apple-health.md`.

## 3. Run

```bash
docker compose -f deploy/compose.yaml up -d
docker compose -f deploy/compose.yaml logs -f zdrowskit
zk ingest status
```

`restart: unless-stopped` brings both back after a reboot once Docker starts.

## Ingestion-only mode (no LLM provider key)

The daemon imports with no provider key, but scheduled reports, coach reviews
and nudges call an LLM and fail. Disable them for the profile until a responder
exists:

```bash
docker compose -f deploy/compose.yaml run --rm zdrowskit sh -c \
  'cat > /data/profiles/NAME/notification_prefs.json <<EOF
{"version": 1, "overrides": {
  "nudges": {"enabled": false},
  "weekly_insights": {"enabled": false},
  "weekly_coach": {"enabled": false}}}
EOF'
```

`zk notify --profile NAME show` confirms it. Data-health alerts stay on; they
need Telegram, not an LLM.

## Backfill

HTTP uploads are capped per request (`docs/http-ingest.md`), so import years of
history from Manual Export JSON files instead ("Historical Backfill" in
`docs/apple-health.md`). Put them in a host folder laid out as Auto Export
writes them, `Metrics/*.json` and `Workouts/*.json`, then import it once:

```bash
docker compose -f deploy/compose.yaml run --rm -v /path/to/export:/backfill:ro \
  zdrowskit uv run --no-sync python main.py import --source local --data-dir /backfill
```

The import is idempotent, so re-running it does not duplicate data.

## Data and backups

Everything zdrowskit owns is in the `zdrowskit-home` volume: `profiles.toml`,
each profile's `health.db`, context files, the raw-payload archive, and
`ingest_tokens.json` (token hashes only). Back up the volume, e.g.
`docker run --rm -v zdrowskit_zdrowskit-home:/data -v "$PWD":/out busybox tar czf /out/zdrowskit-home.tgz -C /data .`.
