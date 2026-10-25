# Garmin Personal API → OpenAthlete

Read Garmin activities with Garmin's official Personal API Python client, download ZIP/FIT, and import into OpenAthlete. Successful Garmin IDs are persisted in SQLite. Supports resumable backfill and 30-minute incremental sync.

## Local setup

Requires Python 3.12+ and a Garmin Personal API key.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env; do not commit it.
set -a; source .env; set +a
export STATE_DB="$PWD/sync.sqlite3"
python sync.py backfill
python sync.py sync
```

If your Garmin API key is already stored at `~/.config/garmin/connect-personal-api-key`, you can omit `GARMIN_PERSONAL_API_KEY` locally (but set it in Coolify).

## Coolify / Docker Compose

Create a Docker Compose service using this repository. Configure the four required environment variables in Coolify, and deploy. A named Docker volume stores SQLite state and Garmin's bearer-token cache.

Run the one-time backfill **inside the running service**, so it shares the same persistent state:

```bash
docker compose exec garmin-sync python sync.py backfill
```

The container defaults to `serve`, which runs a sync immediately and repeats every 30 minutes. For a large initial backfill, run it before enabling `serve`, or temporarily stop the periodic worker to avoid concurrent writes/imports.

## Operational notes

- The incremental job rechecks the previous 14 days to catch delayed Garmin uploads.
- OpenAthlete 201 responses may include `alreadyImported: true`; these are recorded as done.
- A 409 same-start-time conflict is treated as an existing activity; check manually if the conflict is unexpected.
- Failed imports are recorded as failed and retried on the next run.
- Some Garmin activity archives might not contain FIT files; these remain failed and appear in logs.
- OpenAthlete access tokens are renewed via `/api/auth/login` at the beginning of each worker lifetime and again after a 401.
- Garmin bearer-token caching is handled by Garmin's official client.
- Backfill lists all available Garmin activities in pages of 50. It can take time and API throttling may apply.
- No network ports need to be exposed for this worker.
