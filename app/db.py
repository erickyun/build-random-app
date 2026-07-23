from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

DATA_DIR = Path(os.getenv('DATA_DIR', '/data')).resolve()
DB_PATH = DATA_DIR / 'jobs.sqlite3'
JOBS_DIR = DATA_DIR / 'jobs'

_lock = threading.Lock()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_data_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    JOBS_DIR.mkdir(parents=True, exist_ok=True)


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    ensure_data_dirs()
    connection = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA journal_mode=WAL')
    connection.execute('PRAGMA synchronous=NORMAL')
    connection.execute('PRAGMA foreign_keys=ON')
    try:
        yield connection
    finally:
        connection.close()


def init_db() -> None:
    ensure_data_dirs()
    with connect() as connection:
        connection.executescript(
            '''
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                source_type TEXT NOT NULL,
                source_name TEXT,
                source_url TEXT,
                input_path TEXT,
                subtitle_path TEXT,
                output_path TEXT,
                output_name TEXT NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                output_format TEXT NOT NULL,
                preset TEXT NOT NULL,
                crf INTEGER NOT NULL,
                audio_mode TEXT NOT NULL,
                anime_filter INTEGER NOT NULL DEFAULT 0,
                progress REAL NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                error TEXT,
                command_json TEXT,
                log_path TEXT NOT NULL,
                pid INTEGER,
                file_size INTEGER,
                duration_seconds REAL
            );
            CREATE INDEX IF NOT EXISTS idx_jobs_status_created
                ON jobs(status, created_at);
            '''
        )


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    if data.get('command_json'):
        try:
            data['commands'] = json.loads(data['command_json'])
        except json.JSONDecodeError:
            data['commands'] = []
    else:
        data['commands'] = []
    data.pop('command_json', None)
    return data


def insert_job(record: dict[str, Any]) -> None:
    columns = ', '.join(record.keys())
    placeholders = ', '.join('?' for _ in record)
    values = list(record.values())
    with connect() as connection:
        connection.execute(
            f'INSERT INTO jobs ({columns}) VALUES ({placeholders})',
            values,
        )


def get_job(job_id: str) -> dict[str, Any] | None:
    with connect() as connection:
        row = connection.execute('SELECT * FROM jobs WHERE id = ?', (job_id,)).fetchone()
    return row_to_dict(row)


def list_jobs(limit: int = 50) -> list[dict[str, Any]]:
    with connect() as connection:
        rows = connection.execute(
            'SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?',
            (max(1, min(limit, 200)),),
        ).fetchall()
    return [row_to_dict(row) or {} for row in rows]


def update_job(job_id: str, **changes: Any) -> None:
    if not changes:
        return
    assignments = ', '.join(f'{key} = ?' for key in changes)
    values = list(changes.values()) + [job_id]
    with connect() as connection:
        connection.execute(
            f'UPDATE jobs SET {assignments} WHERE id = ?',
            values,
        )


def claim_next_job() -> dict[str, Any] | None:
    with _lock:
        with connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute(
                "SELECT * FROM jobs WHERE status = 'queued' ORDER BY created_at ASC LIMIT 1"
            ).fetchone()
            if row is None:
                connection.execute('COMMIT')
                return None
            started_at = utc_now()
            connection.execute(
                "UPDATE jobs SET status = 'preparing', started_at = ?, error = NULL, progress = 0 WHERE id = ?",
                (started_at, row['id']),
            )
            connection.execute('COMMIT')
            claimed = dict(row)
            claimed['status'] = 'preparing'
            claimed['started_at'] = started_at
            return claimed


def recover_interrupted_jobs() -> int:
    init_db()
    with connect() as connection:
        cursor = connection.execute(
            """
            UPDATE jobs
            SET status = 'queued', pid = NULL, progress = 0,
                error = CASE
                    WHEN error IS NULL OR error = '' THEN 'Recovered after service restart.'
                    ELSE error || '\nRecovered after service restart.'
                END
            WHERE status IN ('preparing', 'downloading', 'running')
            """
        )
        return cursor.rowcount


def delete_job(job_id: str) -> dict[str, Any] | None:
    job = get_job(job_id)
    if job is None:
        return None
    with connect() as connection:
        connection.execute('DELETE FROM jobs WHERE id = ?', (job_id,))
    return job
