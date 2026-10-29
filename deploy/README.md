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
TS_AUTHKEY=tskey-auth-...   # optional
```

`TS_AUTHKEY` is optional. Without it, the sidecar logs a one-time login URL on
first start (`docker compose -f deploy/compose.yaml logs tailscale`); open it
to add the node to your tailnet, and the `tailscale-state` volume keeps the
login across restarts. Either way, Funnel must be allowed for the node by the
tailnet policy (`funnel` node attribute). See Tailscale's Funnel docs.

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

## LAN with your own certificate (instead of Tailscale)

If the phone can reach the host on your home network, and over a VPN such as
WireGuard when away, `compose.lan.yaml` replaces the Tailscale sidecar with a
Caddy sidecar. Caddy serves an existing certificate (a wildcard for your domain
works) and proxies to the daemon's loopback receiver. Nothing is exposed to the
internet.

1. **DNS:** an A record for the hostname, e.g. `zdrowskit.example.org`,
   pointing at the host's LAN IP, resolvable at home and through the VPN.
2. **`deploy/.env`** (alongside `ZDROWSKIT_HOME_DIR`; `TS_AUTHKEY` is unused):

   ```dotenv
   ZDROWSKIT_LAN_IP=192.168.1.20
   ZDROWSKIT_HOSTNAME=zdrowskit.example.org
   TLS_CERT_DIR=/path/to/cert/dir     # read-only mount
   TLS_CERT_FILE=fullchain.pem        # default
   TLS_KEY_FILE=privkey.pem           # default
   ```

3. Use `-f deploy/compose.lan.yaml` everywhere this README says
   `-f deploy/compose.yaml`, including the `zk` alias. Skip the Funnel steps.
   The upload URL is `https://<ZDROWSKIT_HOSTNAME>` plus the paths in
   `docs/http-ingest.md`. Check it from a device on the LAN with
   `curl https://<ZDROWSKIT_HOSTNAME>/healthz`.
4. **Certificate renewal:** Caddy reads the files at startup and on reload. After
   each renewal, run
   `docker compose -f deploy/compose.lan.yaml exec caddy caddy reload --config /etc/caddy/Caddyfile --force`,
   for example from your renewal tool's deploy hook.
5. **Phone away from home:** turn on the VPN's on-demand mode (in the WireGuard
   app: connect on cellular and on Wi-Fi other than home) and use your home DNS
   resolver in the tunnel, so background exports reach the host. A missed upload
   is refilled by the next rolling export; there is no server-side queue.

The daemon's Tailscale health checks report "unknown" without a Tailscale CLI;
that is expected here, not a fault.

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

HTTP uploads are capped per request (`docs/http-ingest.md`), and years of
history exceed the caps: the receiver answers `413` and imports nothing. An
automation's **Manual Export** sends to the automation's URL, so it cannot
carry a large backfill either. Export history to files instead:

1. In Auto Export's **Export** tab, export **Health Metrics** as JSON v2 at
   aggregation **Hours**, and **Workouts** as JSON v2 at **Minutes**, each
   saved to Files. One file cannot carry both: they need different aggregations
   ("Why the aggregation settings matter" in `docs/apple-health.md`).
2. Copy them to the host in the layout Auto Export writes, unzipping the
   Workouts archive if the app produced one. File names do not matter:

   ```text
   /path/to/export/Metrics/*.json
   /path/to/export/Workouts/*.json
   ```

3. Check that Metrics really are hourly. Dates with varying clock times mean
   `Hours`; every date at `00:00:00` means `Days`, so re-export:

   ```bash
   docker compose -f deploy/compose.yaml run --rm -v /path/to/export:/backfill:ro zdrowskit \
     uv run --no-sync python -c '
   import json, glob
   d = json.load(open(glob.glob("/backfill/Metrics/*.json")[0]))["data"]["metrics"]
   print([e["date"] for e in d[0]["data"][:4]])'
   ```

4. Import, then confirm the stored date range with `zk status`:

   ```bash
   docker compose -f deploy/compose.yaml run --rm -v /path/to/export:/backfill:ro \
     zdrowskit uv run --no-sync python main.py import --source local --data-dir /backfill
   ```

Use `-f deploy/compose.lan.yaml` with the LAN setup. Each route workout is
reverse-geocoded on first import, which is slow for years of outdoor workouts;
add `-e ZDROWSKIT_LOCATION_GEOCODER=off` after `--rm` to skip place names. The
import is idempotent, so re-running it does not duplicate data. Back up
`health.db` afterwards, and delete the export files: they are health data in
plain files.

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
