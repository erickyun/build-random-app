from __future__ import annotations

import hmac
import os
import secrets
import shutil
import time
import uuid
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from .db import JOBS_DIR, delete_job, get_job, init_db, insert_job, list_jobs, utc_now
from .media import (
    MediaError,
    ensure_output_name,
    safe_filename,
    validate_preset,
    validate_range,
    validate_url,
)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / 'static'
APP_PASSWORD = os.getenv('APP_PASSWORD', 'change-me-before-public-use')
SESSION_SECRET = os.getenv('SESSION_SECRET', 'change-this-session-secret-before-public-use')
COOKIE_SECURE = os.getenv('COOKIE_SECURE', 'false').lower() in {'1', 'true', 'yes', 'on'}
MAX_UPLOAD_BYTES = int(os.getenv('MAX_UPLOAD_BYTES', str(20 * 1024 * 1024 * 1024)))
ALLOWED_SUBTITLE_SUFFIXES = {'.ass', '.ssa', '.srt', '.vtt'}
LOGIN_WINDOW_SECONDS = 600
LOGIN_MAX_FAILURES = 8
_login_failures: dict[str, list[float]] = {}

app = FastAPI(title='FFmpeg Trim Studio', docs_url=None, redoc_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    session_cookie='ffmpeg_trim_session',
    max_age=60 * 60 * 24 * 7,
    same_site='lax',
    https_only=COOKIE_SECURE,
)
app.mount('/static', StaticFiles(directory=STATIC_DIR), name='static')


@app.on_event('startup')
def startup() -> None:
    init_db()


def _client_key(request: Request) -> str:
    forwarded = request.headers.get('x-forwarded-for', '').split(',')[0].strip()
    return forwarded or (request.client.host if request.client else 'unknown')


def _prune_failures(key: str) -> list[float]:
    cutoff = time.time() - LOGIN_WINDOW_SECONDS
    recent = [timestamp for timestamp in _login_failures.get(key, []) if timestamp >= cutoff]
    _login_failures[key] = recent
    return recent


def require_login(request: Request) -> None:
    if request.session.get('authenticated') is not True:
        raise HTTPException(status_code=401, detail='Authentication required.')


def require_csrf(request: Request, _: None = Depends(require_login)) -> None:
    expected = request.session.get('csrf')
    provided = request.headers.get('x-csrf-token')
    if not expected or not provided or not hmac.compare_digest(str(expected), provided):
        raise HTTPException(status_code=403, detail='Invalid CSRF token.')


async def save_upload(upload: UploadFile, destination: Path) -> int:
    destination.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    try:
        with destination.open('wb') as output:
            while chunk := await upload.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    raise MediaError('Uploaded file exceeds the configured maximum size.')
                output.write(chunk)
    finally:
        await upload.close()
    if total == 0:
        destination.unlink(missing_ok=True)
        raise MediaError('Uploaded file is empty.')
    return total


@app.get('/')
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / 'index.html')


@app.get('/api/health')
def health() -> dict[str, str]:
    return {'status': 'ok'}


@app.get('/api/session')
def session_status(request: Request) -> dict[str, object]:
    authenticated = request.session.get('authenticated') is True
    return {
        'authenticated': authenticated,
        'csrf': request.session.get('csrf') if authenticated else None,
    }


@app.post('/api/login')
def login(request: Request, payload: dict[str, str]) -> JSONResponse:
    key = _client_key(request)
    failures = _prune_failures(key)
    if len(failures) >= LOGIN_MAX_FAILURES:
        raise HTTPException(status_code=429, detail='Too many login attempts. Try again later.')
    supplied = payload.get('password', '')
    if not hmac.compare_digest(supplied, APP_PASSWORD):
        failures.append(time.time())
        _login_failures[key] = failures
        raise HTTPException(status_code=401, detail='Incorrect password.')
    _login_failures.pop(key, None)
    request.session.clear()
    request.session['authenticated'] = True
    request.session['csrf'] = secrets.token_urlsafe(32)
    return JSONResponse({'ok': True, 'csrf': request.session['csrf']})


@app.post('/api/logout')
def logout(request: Request, _: None = Depends(require_csrf)) -> dict[str, bool]:
    request.session.clear()
    return {'ok': True}


@app.get('/api/jobs')
def jobs(_: None = Depends(require_login)) -> dict[str, object]:
    return {'jobs': list_jobs()}


@app.get('/api/jobs/{job_id}')
def job_details(job_id: str, _: None = Depends(require_login)) -> dict[str, object]:
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail='Job not found.')
    return {'job': job}


@app.post('/api/jobs')
async def create_job(
    request: Request,
    source_type: Annotated[str, Form()],
    source_url: Annotated[str | None, Form()] = None,
    start_time: Annotated[str, Form()] = '00:00:00.000',
    end_time: Annotated[str, Form()] = '00:00:10.000',
    output_name: Annotated[str, Form()] = 'trimmed',
    output_format: Annotated[str, Form()] = 'mkv',
    preset: Annotated[str, Form()] = 'x264-medium',
    crf: Annotated[int, Form()] = 18,
    audio_mode: Annotated[str, Form()] = 'copy',
    anime_filter: Annotated[bool, Form()] = False,
    media_file: UploadFile | None = File(default=None),
    subtitle_file: UploadFile | None = File(default=None),
    _: None = Depends(require_csrf),
) -> dict[str, object]:
    del request
    job_id = uuid.uuid4().hex
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=False)
    log_path = job_dir / 'process.log'
    input_path: Path | None = None
    subtitle_path: Path | None = None
    source_name: str | None = None

    try:
        validate_range(start_time, end_time)
        validate_preset(preset)
        final_output_name = ensure_output_name(output_name, output_format)
        if not 0 <= crf <= 51:
            raise MediaError('CRF must be between 0 and 51.')
        if audio_mode not in {'copy', 'encode'}:
            raise MediaError('Audio mode must be copy or encode.')
        if source_type not in {'upload', 'url'}:
            raise MediaError('Source type must be upload or URL.')
        validated_url: str | None = None

        if source_type == 'upload':
            if media_file is None or not media_file.filename:
                raise MediaError('Choose a media file to upload.')
            source_name = safe_filename(media_file.filename, 'input-media')
            input_path = job_dir / f'input-{source_name}'
            await save_upload(media_file, input_path)
        else:
            if not source_url:
                raise MediaError('Enter a direct media URL.')
            validated_url = validate_url(source_url)
            source_name = safe_filename(Path(validated_url.split('?', 1)[0]).name, 'remote-media')

        if subtitle_file is not None and subtitle_file.filename:
            suffix = Path(subtitle_file.filename).suffix.lower()
            if suffix not in ALLOWED_SUBTITLE_SUFFIXES:
                raise MediaError('Subtitle must be ASS, SSA, SRT, or VTT.')
            subtitle_name = safe_filename(subtitle_file.filename, f'subtitle{suffix}')
            subtitle_path = job_dir / f'subtitle-source-{subtitle_name}'
            await save_upload(subtitle_file, subtitle_path)

        insert_job(
            {
                'id': job_id,
                'status': 'queued',
                'source_type': source_type,
                'source_name': source_name,
                'source_url': validated_url,
                'input_path': str(input_path) if input_path else None,
                'subtitle_path': str(subtitle_path) if subtitle_path else None,
                'output_path': None,
                'output_name': final_output_name,
                'start_time': start_time,
                'end_time': end_time,
                'output_format': output_format,
                'preset': preset,
                'crf': crf,
                'audio_mode': audio_mode,
                'anime_filter': int(anime_filter),
                'progress': 0,
                'created_at': utc_now(),
                'started_at': None,
                'finished_at': None,
                'error': None,
                'command_json': None,
                'log_path': str(log_path),
                'pid': None,
                'file_size': None,
                'duration_seconds': None,
            }
        )
        return {'ok': True, 'job': get_job(job_id)}
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise


@app.get('/api/jobs/{job_id}/log')
def get_log(job_id: str, _: None = Depends(require_login)) -> dict[str, object]:
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail='Job not found.')
    log_path = Path(job['log_path'])
    if not log_path.exists():
        return {'log': '', 'status': job['status']}
    max_bytes = 512 * 1024
    with log_path.open('rb') as handle:
        size = log_path.stat().st_size
        if size > max_bytes:
            handle.seek(-max_bytes, 2)
            handle.readline()
        content = handle.read().decode('utf-8', errors='replace')
    return {'log': content, 'status': job['status']}


@app.post('/api/jobs/{job_id}/log/clear')
def clear_log(job_id: str, _: None = Depends(require_csrf)) -> dict[str, bool]:
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail='Job not found.')
    log_path = Path(job['log_path'])
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text('', encoding='utf-8')
    return {'ok': True}


@app.get('/api/jobs/{job_id}/download')
def download_output(job_id: str, _: None = Depends(require_login)) -> FileResponse:
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail='Job not found.')
    if job['status'] != 'completed' or not job.get('output_path'):
        raise HTTPException(status_code=409, detail='Output is not ready.')
    output_path = Path(job['output_path'])
    if not output_path.exists():
        raise HTTPException(status_code=404, detail='Output file no longer exists.')
    return FileResponse(output_path, filename=output_path.name, media_type='application/octet-stream')


@app.delete('/api/jobs/{job_id}')
def remove_job(job_id: str, _: None = Depends(require_csrf)) -> dict[str, bool]:
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail='Job not found.')
    if job['status'] in {'preparing', 'downloading', 'running'}:
        raise HTTPException(status_code=409, detail='Running jobs cannot be deleted.')
    delete_job(job_id)
    shutil.rmtree(JOBS_DIR / job_id, ignore_errors=True)
    return {'ok': True}


@app.exception_handler(MediaError)
def media_error_handler(_: Request, exc: MediaError) -> JSONResponse:
    return JSONResponse(status_code=400, content={'detail': str(exc)})
