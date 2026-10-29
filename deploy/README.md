# Running zdrowskit in Docker (Linux host)

`compose.yaml` runs two containers:

- **`zdrowskit`:** the daemon, from `Dockerfile`. Its state (`ZDROWSKIT_HOME`)
  is a **host folder** bind-mounted at `/data`, so you can edit context files
  with any editor, version them privately and back them up directly.
- **`tailscale`:** a sidecar that owns the network namespace and publishes the
  daemon's loopback receiver (`127.0.0.1:8787`) through Funnel using
  `funnel.json`. The receiver never binds a LAN interface.

The images are multi-arch (amd64 and arm64). Run every command below from the
repository root.

## 1. State folder and secrets

Create the state folder outside this repository. The container runs as uid
1000, which must own it:

```bash
mkdir -p ~/zdrowskit-home
sudo chown 1000:1000 ~/zdrowskit-home   # only if your user is not uid 1000
```

**`deploy/.env`** is read by compose:

```dotenv
ZDROWSKIT_HOME_DIR=/home/you/zdrowskit-home
TS_AUTHKEY=tskey-auth-...
```

Create the Tailscale key in the admin console. Funnel must be allowed for the
node by the tailnet policy (`funnel` node attribute). See Tailscale's Funnel
docs.

**`.env` in the repository root** is passed to the daemon. Start from
`.env_example`:

- `TELEGRAM_BOT_TOKEN` is optional. Without it the daemon logs that Telegram is
  disabled and keeps importing.
- Provider API keys are optional. See ingestion-only mode below.

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

## Context files

A profile's context files are plain files on the host:
`$ZDROWSKIT_HOME_DIR/profiles/NAME/ContextFiles/` (`me.md`, `strategy.md`,
`log.md`, …). Edit them in place; the daemon watches that folder. See
`docs/context-files.md` for what each file is for.

## Ingestion-only mode (no LLM provider key)

The daemon imports with no provider key, but scheduled reports, coach reviews
and nudges call an LLM and fail. Disable them for the profile until a responder
exists by writing `$ZDROWSKIT_HOME_DIR/profiles/NAME/notification_prefs.json`:

```json
{"version": 1, "overrides": {
  "nudges": {"enabled": false},
  "weekly_insights": {"enabled": false},
  "weekly_coach": {"enabled": false}}}
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

## Versioning and backups

Keep the text configuration in a **private** git repository inside the state
folder: profiles, context files and preferences. Never push it to a public
remote; it is personal health information.

```bash
cd ~/zdrowskit-home && git init
cat > .gitignore <<'EOF'
# Track text configuration only: profiles.toml, ContextFiles, *_prefs.json.
*.db
*.db-*
profiles/*/Imports/
profiles/*/Reports/
profiles/*/Nudges/
profiles/*/daemon_state.json
/ingest_tokens.json
/.daemon.lock
EOF
```

`ingest setup` also ensures `/ingest_tokens.json` is ignored. Every coach edit
you approve then shows up as a diff to commit.

Back up each `health.db` separately with SQLite's online backup, which is safe
while the daemon runs:

```bash
sqlite3 ~/zdrowskit-home/profiles/NAME/health.db \
  ".backup '/path/to/backups/health-$(date +%F).db'"
```
