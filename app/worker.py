from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import httpx

from .db import JOBS_DIR, claim_next_job, get_job, init_db, recover_interrupted_jobs, update_job, utc_now
from .media import (
    MediaError,
    TEXT_SUBTITLE_CODECS,
    build_commands,
    command_for_display,
    ensure_output_name,
    get_duration,
    probe_media,
    safe_filename,
    trim_external_subtitle,
    validate_range,
    validate_url,
)

POLL_SECONDS = float(os.getenv('WORKER_POLL_SECONDS', '1.5'))
MAX_URL_BYTES = int(os.getenv('MAX_URL_BYTES', str(20 * 1024 * 1024 * 1024)))
CHUNK_SIZE = 1024 * 1024
_STOP = False


def stop_worker(*_: object) -> None:
    global _STOP
    _STOP = True


def append_log(log_path: Path, message: str) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
    with log_path.open('a', encoding='utf-8', errors='replace') as handle:
        handle.write(f'[{timestamp}] {message.rstrip()}\n')


def _filename_from_response(response: httpx.Response, url: str) -> str:
    disposition = response.headers.get('content-disposition', '')
    if 'filename=' in disposition:
        candidate = disposition.split('filename=', 1)[1].strip().strip('"\'')
        if candidate:
            return safe_filename(unquote(candidate), 'downloaded-media')
    candidate = unquote(Path(urlparse(url).path).name)
    return safe_filename(candidate, 'downloaded-media')


def download_url(url: str, job_dir: Path, log_path: Path, job_id: str) -> Path:
    current_url = validate_url(url)
    headers = {'User-Agent': 'FFmpeg-Trim-Studio/1.0'}
    with httpx.Client(timeout=httpx.Timeout(30.0, read=None), follow_redirects=False, headers=headers) as client:
        response: httpx.Response | None = None
        for redirect_count in range(6):
            append_log(log_path, f'Downloading URL: {current_url}')
            request = client.build_request('GET', current_url)
            response = client.send(request, stream=True)
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get('location')
                response.close()
                if not location:
                    raise MediaError('Download redirect did not contain a destination.')
                if redirect_count == 5:
                    raise MediaError('Too many URL redirects.')
                current_url = validate_url(urljoin(current_url, location))
                continue
            response.raise_for_status()
            break
        if response is None:
            raise MediaError('Could not start URL download.')

        content_length = response.headers.get('content-length')
        if content_length and int(content_length) > MAX_URL_BYTES:
            response.close()
            raise MediaError('Remote file exceeds the configured maximum size.')

        filename = _filename_from_response(response, current_url)
        destination = job_dir / f'input-{filename}'
        downloaded = 0
        with destination.open('wb') as output:
            for chunk in response.iter_bytes(CHUNK_SIZE):
                if not chunk:
                    continue
                downloaded += len(chunk)
                if downloaded > MAX_URL_BYTES:
                    response.close()
                    destination.unlink(missing_ok=True)
                    raise MediaError('Remote file exceeded the configured maximum size while downloading.')
                output.write(chunk)
                if content_length:
                    progress = min(10.0, downloaded / int(content_length) * 10.0)
                    update_job(job_id, progress=round(progress, 2))
        response.close()
        append_log(log_path, f'Download complete: {downloaded / (1024 * 1024):.2f} MiB')
        return destination


def extract_and_trim_embedded_subtitles(
    input_path: Path,
    probe: dict[str, object],
    job_dir: Path,
    log_path: Path,
    start_seconds: float,
    end_seconds: float,
) -> list[Path]:
    trimmed_paths: list[Path] = []
    streams = probe.get('streams', [])
    if not isinstance(streams, list):
        return trimmed_paths
    for stream in streams:
        if not isinstance(stream, dict) or stream.get('codec_type') != 'subtitle':
            continue
        codec_name = str(stream.get('codec_name') or '').lower()
        if codec_name not in TEXT_SUBTITLE_CODECS:
            append_log(log_path, f"Preserving bitmap/unsupported subtitle stream {stream.get('index')} ({codec_name or 'unknown'}) without text trimming.")
            continue
        try:
            stream_index = int(stream['index'])
        except (KeyError, TypeError, ValueError) as exc:
            raise MediaError('Embedded subtitle stream is missing a valid index.') from exc
        if codec_name in {'ass', 'ssa'}:
            suffix, encoder = '.ass', 'ass'
        elif codec_name == 'webvtt':
            suffix, encoder = '.vtt', 'webvtt'
        else:
            suffix, encoder = '.srt', 'srt'
        extracted = job_dir / f'embedded-subtitle-{stream_index}-source{suffix}'
        trimmed = job_dir / f'embedded-subtitle-{stream_index}-trimmed{suffix}'
        command = [
            'ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
            '-i', str(input_path), '-map', f'0:{stream_index}',
            '-c:s', encoder, str(extracted),
        ]
        append_log(log_path, f'Extracting embedded text subtitle stream {stream_index}: {command_for_display(command)}')
        result = subprocess.run(command, capture_output=True, text=True, timeout=600, check=False)
        if result.returncode != 0 or not extracted.exists():
            raise MediaError(result.stderr.strip() or f'Could not extract embedded subtitle stream {stream_index}.')
        trim_external_subtitle(extracted, trimmed, start_seconds, end_seconds)
        trimmed_paths.append(trimmed)
        append_log(log_path, f'Trimmed embedded subtitle stream {stream_index} to {trimmed.name}.')
    return trimmed_paths


def extract_attachments(
    input_path: Path,
    probe: dict[str, object],
    job_dir: Path,
    log_path: Path,
) -> list[dict[str, str]]:
    attachments: list[dict[str, str]] = []
    streams = probe.get('streams', [])
    if not isinstance(streams, list):
        return attachments
    for stream in streams:
        if not isinstance(stream, dict) or stream.get('codec_type') != 'attachment':
            continue
        try:
            stream_index = int(stream['index'])
        except (KeyError, TypeError, ValueError) as exc:
            raise MediaError('Attachment stream is missing a valid index.') from exc
        tags = stream.get('tags') if isinstance(stream.get('tags'), dict) else {}
        filename = safe_filename(str(tags.get('filename') or f'attachment-{stream_index}.bin'), f'attachment-{stream_index}.bin')
        mimetype = str(tags.get('mimetype') or 'application/octet-stream').replace('\r', '').replace('\n', '')
        target = job_dir / f'attachment-{stream_index}-{filename}'
        command = [
            'ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
            f'-dump_attachment:{stream_index}', str(target),
            '-i', str(input_path), '-f', 'null', '-',
        ]
        append_log(log_path, f'Extracting attachment stream {stream_index}: {command_for_display(command)}')
        result = subprocess.run(command, capture_output=True, text=True, timeout=600, check=False)
        if result.returncode != 0 or not target.exists():
            raise MediaError(result.stderr.strip() or f'Could not extract attachment stream {stream_index}.')
        attachments.append({'path': str(target), 'filename': filename, 'mimetype': mimetype})
        append_log(log_path, f'Preserved attachment stream {stream_index} as {filename}.')
    return attachments


def _parse_progress(line: str) -> float | None:
    if line.startswith('out_time_ms='):
        try:
            return float(line.split('=', 1)[1]) / 1_000_000
        except ValueError:
            return None
    if line.startswith('out_time='):
        raw = line.split('=', 1)[1]
        try:
            hours, minutes, seconds = raw.split(':')
            return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
        except (ValueError, TypeError):
            return None
    return None


def run_ffmpeg_command(
    job_id: str,
    command: list[str],
    log_path: Path,
    duration: float,
    pass_index: int,
    pass_count: int,
) -> None:
    append_log(log_path, f'FFmpeg command {pass_index}/{pass_count}: {command_for_display(command)}')
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        universal_newlines=True,
    )
    update_job(job_id, pid=process.pid, status='running')
    assert process.stdout is not None
    for raw_line in process.stdout:
        line = raw_line.rstrip('\r\n')
        if line:
            append_log(log_path, line)
        seconds = _parse_progress(line)
        if seconds is not None and duration > 0:
            within_pass = min(max(seconds / duration, 0.0), 1.0)
            overall = ((pass_index - 1) + within_pass) / pass_count
            update_job(job_id, progress=round(10 + overall * 88, 2))
    return_code = process.wait()
    update_job(job_id, pid=None)
    if return_code != 0:
        raise MediaError(f'FFmpeg exited with code {return_code}. See the process log for details.')


def process_job(job: dict[str, object]) -> None:
    job_id = str(job['id'])
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    log_path = Path(str(job['log_path']))
    append_log(log_path, 'Job claimed by background worker.')

    try:
        start_seconds, end_seconds = validate_range(str(job['start_time']), str(job['end_time']))
        duration = end_seconds - start_seconds
        update_job(job_id, duration_seconds=duration)

        input_path_raw = job.get('input_path')
        if str(job['source_type']) == 'url':
            update_job(job_id, status='downloading', progress=1)
            input_path = download_url(str(job['source_url']), job_dir, log_path, job_id)
            update_job(job_id, input_path=str(input_path), source_name=input_path.name)
        elif input_path_raw:
            input_path = Path(str(input_path_raw))
        else:
            raise MediaError('The uploaded media file is missing.')

        if not input_path.exists() or input_path.stat().st_size == 0:
            raise MediaError('The input media file is empty or unavailable.')

        append_log(log_path, 'Probing media streams and attachments with ffprobe.')
        probe = probe_media(input_path)
        source_duration = get_duration(probe)
        if source_duration is not None and end_seconds > source_duration + 0.25:
            raise MediaError(f'End time exceeds source duration ({source_duration:.3f} seconds).')

        output_format = str(job['output_format'])
        attachments = extract_attachments(input_path, probe, job_dir, log_path) if output_format == 'mkv' else []
        subtitle_paths = extract_and_trim_embedded_subtitles(
            input_path, probe, job_dir, log_path, start_seconds, end_seconds
        )
        original_subtitle = job.get('subtitle_path')
        if original_subtitle:
            source_subtitle = Path(str(original_subtitle))
            if not source_subtitle.exists():
                raise MediaError('The external subtitle file is missing.')
            subtitle_path = job_dir / f'subtitle-trimmed{source_subtitle.suffix.lower()}'
            append_log(log_path, 'Trimming and rebasing external subtitle cues to start at 00:00:00.')
            trim_external_subtitle(source_subtitle, subtitle_path, start_seconds, end_seconds)
            subtitle_paths.append(subtitle_path)

        output_name = ensure_output_name(str(job['output_name']), output_format)
        output_path = job_dir / output_name
        passlog_path = job_dir / 'ffmpeg-pass'

        commands = build_commands(
            input_path=input_path,
            output_path=output_path,
            subtitle_paths=subtitle_paths,
            attachments=attachments,
            start_time=str(job['start_time']),
            end_time=str(job['end_time']),
            output_format=output_format,
            preset=str(job['preset']),
            crf=int(job['crf']),
            audio_mode=str(job['audio_mode']),
            anime_filter=bool(job['anime_filter']),
            probe=probe,
            passlog_path=passlog_path,
        )
        command_strings = [command_for_display(command) for command in commands]
        update_job(job_id, command_json=json.dumps(command_strings), output_path=str(output_path), progress=10)

        for pass_index, command in enumerate(commands, start=1):
            run_ffmpeg_command(job_id, command, log_path, duration, pass_index, len(commands))

        if not output_path.exists() or output_path.stat().st_size == 0:
            raise MediaError('FFmpeg completed without producing a usable output file.')
        update_job(
            job_id,
            status='completed',
            progress=100,
            finished_at=utc_now(),
            file_size=output_path.stat().st_size,
            error=None,
            pid=None,
        )
        append_log(log_path, f'Completed: {output_path.name} ({output_path.stat().st_size / (1024 * 1024):.2f} MiB)')
    except Exception as exc:
        message = str(exc) or exc.__class__.__name__
        append_log(log_path, f'ERROR: {message}')
        update_job(
            job_id,
            status='failed',
            finished_at=utc_now(),
            error=message,
            pid=None,
        )
    finally:
        for pass_file in job_dir.glob('ffmpeg-pass*'):
            pass_file.unlink(missing_ok=True)


def main() -> None:
    signal.signal(signal.SIGTERM, stop_worker)
    signal.signal(signal.SIGINT, stop_worker)
    init_db()
    recovered = recover_interrupted_jobs()
    if recovered:
        print(f'Recovered {recovered} interrupted job(s).', flush=True)
    while not _STOP:
        job = claim_next_job()
        if job is None:
            time.sleep(POLL_SECONDS)
            continue
        fresh_job = get_job(str(job['id']))
        if fresh_job is not None:
            process_job(fresh_job)
    print('Worker stopped.', flush=True)


if __name__ == '__main__':
    main()
