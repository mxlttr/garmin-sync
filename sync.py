#!/usr/bin/env python3
"""Garmin Connect Personal API -> OpenAthlete FIT sync."""
import argparse
import io
import logging
import os
import sqlite3
import sys
import time
import zipfile
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path

import requests
from garmin_personal_api import GarminPersonalApiClient, PersonalApiAuthenticator, resolve_api_key

LOG = logging.getLogger('garmin-sync')
DB_PATH = Path(os.getenv('STATE_DB', '/data/sync.sqlite3'))
BASE = os.getenv('OPENATHLETE_URL', '').rstrip('/')
PAGE_SIZE = int(os.getenv('PAGE_SIZE', '50'))
INTERVAL = int(os.getenv('SYNC_INTERVAL_MINUTES', '30'))
LOOKBACK = int(os.getenv('SYNC_LOOKBACK_DAYS', '14'))
PAUSE = float(os.getenv('GARMIN_REQUEST_PAUSE_SECONDS', '1'))


def require_settings():
    if not BASE.startswith('https://') and not (os.getenv('ALLOW_HTTP') == 'true' and BASE.startswith('http://')):
        raise ValueError('OPENATHLETE_URL must use HTTPS (or explicitly ALLOW_HTTP=true)')
    if not os.getenv('OPENATHLETE_EMAIL') or not os.getenv('OPENATHLETE_PASSWORD'):
        raise ValueError('OPENATHLETE_EMAIL and OPENATHLETE_PASSWORD required')
    if PAGE_SIZE < 1 or PAGE_SIZE > 100 or INTERVAL < 1 or LOOKBACK < 1:
        raise ValueError('Invalid PAGE_SIZE, SYNC_INTERVAL_MINUTES, or SYNC_LOOKBACK_DAYS')


@contextmanager
def database():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as db:
        db.execute('''CREATE TABLE IF NOT EXISTS activities (
            garmin_id INTEGER PRIMARY KEY, status TEXT NOT NULL,
            openathlete_event_id INTEGER, updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            error TEXT)''')
        yield db


class OpenAthlete:
    def __init__(self):
        self.session = requests.Session()
        self.token = None

    def login(self):
        response = self.session.post(f'{BASE}/api/auth/login', json={
            'email': os.environ['OPENATHLETE_EMAIL'],
            'password': os.environ['OPENATHLETE_PASSWORD'],
        }, timeout=30)
        response.raise_for_status()
        self.token = response.json()['accessToken']

    def upload(self, name, fit_bytes, filename):
        if not self.token:
            self.login()
        for attempt in range(2):
            response = self.session.post(
                f'{BASE}/api/activity-import/fit',
                headers={'Authorization': f'Bearer {self.token}'},
                data={'name': name},
                files={'file': (filename, fit_bytes, 'application/octet-stream')},
                timeout=120,
            )
            if response.status_code == 401 and attempt == 0:
                self.login()
                continue
            if response.status_code in (200, 201):
                return response.json()
            # 409 means a same-time activity already exists; treat as duplicate
            if response.status_code == 409:
                return {'alreadyImported': True, 'eventId': None}
            raise RuntimeError(
                f"OpenAthlete HTTP {response.status_code}: {response.text[:1000]}"
            )
        raise RuntimeError('OpenAthlete authentication failed')


def extract_fit(blob):
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        files = [x for x in archive.infolist() if not x.is_dir() and x.filename.lower().endswith('.fit')]
        if not files:
            raise ValueError('No FIT file in Garmin ZIP')
        # Some Garmin archives contain extra FIT files; pick the largest activity FIT.
        selected = max(files, key=lambda x: x.file_size)
        if selected.file_size > 100 * 1024 * 1024:
            raise ValueError('FIT file exceeds 100 MB limit')
        return Path(selected.filename).name, archive.read(selected)


def save(db, activity_id, status, event_id=None, error=None):
    db.execute('''INSERT INTO activities (garmin_id,status,openathlete_event_id,error)
        VALUES (?,?,?,?) ON CONFLICT(garmin_id) DO UPDATE SET
        status=excluded.status, openathlete_event_id=excluded.openathlete_event_id,
        error=excluded.error, updated_at=CURRENT_TIMESTAMP''',
        (activity_id, status, event_id, error))
    db.commit()


def processed(db, activity_id):
    row = db.execute('SELECT status FROM activities WHERE garmin_id=?', (activity_id,)).fetchone()
    return bool(row and row[0] == 'done')


def activity_id_of(item):
    value = item.get('activityId') or item.get('activity_id')
    if value is None:
        raise ValueError(f'Activity has no activityId; keys: {list(item)}')
    return int(value)


def activity_name_of(item, activity_id):
    return str(item.get('activityName') or item.get('name') or f'Garmin Activity {activity_id}')


def run(mode):
    require_settings()
    start_date = None if mode == 'backfill' else (date.today() - timedelta(days=LOOKBACK)).isoformat()
    # Garmin's client caches its bearer token; each run obtains a valid token.
    with PersonalApiAuthenticator() as auth:
        token = auth.obtain_token(resolve_api_key())
    with GarminPersonalApiClient(token.access_token) as garmin, database() as db:
        oa = OpenAthlete()
        offset = 0
        count = {'seen': 0, 'imported': 0, 'skipped': 0, 'failed': 0}
        while True:
            page = garmin.search_activities(start=offset, limit=PAGE_SIZE, start_date=start_date)
            if not isinstance(page, list):
                raise TypeError(f'Unexpected Garmin activities response type: {type(page).__name__}')
            if not page:
                break
            for item in page:
                activity_id = activity_id_of(item)
                count['seen'] += 1
                if processed(db, activity_id):
                    count['skipped'] += 1
                    continue
                try:
                    filename, fit = extract_fit(garmin.download_activity(activity_id))
                    result = oa.upload(activity_name_of(item, activity_id), fit, filename)
                    save(db, activity_id, 'done', result.get('eventId'))
                    count['imported'] += 1
                    LOG.info('OK %s event=%s duplicate=%s', activity_id, result.get('eventId'), result.get('alreadyImported'))
                except Exception as exc:
                    # Failed activities are retried next run; never mark as completed.
                    count['failed'] += 1
                    save(db, activity_id, 'failed', error=str(exc)[:500])
                    LOG.error('FAILED %s: %s', activity_id, exc)
                time.sleep(PAUSE)
            offset += len(page)
            LOG.info('Page complete: offset=%s stats=%s', offset, count)
            if len(page) < PAGE_SIZE:
                break
        LOG.info('Finished %s: %s', mode, count)
        if count['failed']:
            raise RuntimeError(f"{count['failed']} activities failed; retry with same command")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['backfill', 'sync', 'serve'])
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    if args.command != 'serve':
        run(args.command)
        return
    while True:
        try:
            run('sync')
        except Exception:
            LOG.exception('Sync cycle failed; will retry')
        time.sleep(INTERVAL * 60)


if __name__ == '__main__':
    main()
