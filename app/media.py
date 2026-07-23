from __future__ import annotations

import ipaddress
import json
import math
import os
import re
import shlex
import socket
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


TIME_RE = re.compile(r'^(?:(\d+):)?([0-5]?\d):([0-5]?\d(?:\.\d{1,3})?)$|^(\d+(?:\.\d{1,3})?)$')
SAFE_NAME_RE = re.compile(r'[^A-Za-z0-9._-]+')
TEXT_SUBTITLE_CODECS = {'ass', 'ssa', 'subrip', 'srt', 'webvtt', 'mov_text', 'text'}
SUPPORTED_FORMATS = {'mkv', 'mp4', 'webm'}
SUPPORTED_PRESETS = {
    'copy',
    'x264-medium',
    'x264-slow',
    'x265-medium',
    'x265-slow',
    'animethemes-vp9',
}


class MediaError(ValueError):
    pass


def parse_time(value: str) -> float:
    raw = value.strip()
    match = TIME_RE.fullmatch(raw)
    if not match:
        raise MediaError('Time must be seconds or HH:MM:SS.mmm.')
    if match.group(4) is not None:
        seconds = float(match.group(4))
    else:
        hours = int(match.group(1) or 0)
        minutes = int(match.group(2))
        seconds = hours * 3600 + minutes * 60 + float(match.group(3))
    if seconds < 0:
        raise MediaError('Time cannot be negative.')
    return seconds


def format_time(seconds: float) -> str:
    if not math.isfinite(seconds) or seconds < 0:
        raise MediaError('Invalid time value.')
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    return f'{hours:02d}:{minutes:02d}:{secs:06.3f}'


def validate_range(start: str, end: str) -> tuple[float, float]:
    start_seconds = parse_time(start)
    end_seconds = parse_time(end)
    if end_seconds <= start_seconds:
        raise MediaError('End time must be greater than start time.')
    if end_seconds - start_seconds > float(os.getenv('MAX_CLIP_SECONDS', '43200')):
        raise MediaError('Requested clip is longer than the configured maximum duration.')
    return start_seconds, end_seconds


def safe_filename(name: str, default: str = 'media') -> str:
    cleaned = Path(name).name.strip().replace('\x00', '')
    cleaned = SAFE_NAME_RE.sub('_', cleaned).strip('._')
    return cleaned[:180] or default


def ensure_output_name(name: str, output_format: str) -> str:
    if output_format not in SUPPORTED_FORMATS:
        raise MediaError('Unsupported output format.')
    stem = Path(safe_filename(name, 'trimmed')).stem
    return f'{stem}.{output_format}'


def validate_preset(preset: str) -> None:
    if preset not in SUPPORTED_PRESETS:
        raise MediaError('Unsupported encoding preset.')


def validate_url(raw_url: str) -> str:
    parsed = urlparse(raw_url.strip())
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname:
        raise MediaError('Only direct HTTP and HTTPS URLs are allowed.')
    if parsed.username or parsed.password:
        raise MediaError('URLs containing credentials are not allowed.')
    port = parsed.port
    if port is not None and port not in {80, 443, 8080, 8443}:
        raise MediaError('This URL port is not allowed.')
    try:
        addresses = socket.getaddrinfo(parsed.hostname, port or (443 if parsed.scheme == 'https' else 80))
    except socket.gaierror as exc:
        raise MediaError('URL hostname could not be resolved.') from exc
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise MediaError('Private, local, or reserved network addresses are blocked.')
    return parsed.geturl()


def probe_media(path: Path) -> dict[str, Any]:
    command = [
        'ffprobe',
        '-v',
        'error',
        '-show_streams',
        '-show_format',
        '-of',
        'json',
        str(path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)
    if result.returncode != 0:
        raise MediaError(result.stderr.strip() or 'ffprobe could not read the media file.')
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise MediaError('ffprobe returned invalid metadata.') from exc


def get_duration(probe: dict[str, Any]) -> float | None:
    raw = probe.get('format', {}).get('duration')
    try:
        value = float(raw)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def get_audio_bitrate(probe: dict[str, Any]) -> str:
    bitrates: list[int] = []
    for stream in probe.get('streams', []):
        if stream.get('codec_type') == 'audio':
            try:
                bitrates.append(int(stream.get('bit_rate') or 0))
            except (TypeError, ValueError):
                pass
    return '320k' if max(bitrates or [0]) > 320_000 else '192k'


def get_keyframe_interval(duration: float) -> int:
    if duration < 60:
        return 96
    if duration < 120:
        return 120
    return 240


def _read_subtitle_text(source: Path) -> str:
    raw = source.read_bytes()
    for encoding in ('utf-8-sig', 'utf-16', 'cp1252'):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise MediaError('Subtitle text encoding is not supported.')


def _clock_to_ms(value: str) -> int:
    normalized = value.strip().replace(',', '.')
    parts = normalized.split(':')
    if len(parts) != 3:
        raise MediaError(f'Invalid subtitle timestamp: {value}')
    try:
        hours, minutes = int(parts[0]), int(parts[1])
        seconds = float(parts[2])
    except ValueError as exc:
        raise MediaError(f'Invalid subtitle timestamp: {value}') from exc
    return round((hours * 3600 + minutes * 60 + seconds) * 1000)


def _format_ass_ms(milliseconds: int) -> str:
    milliseconds = max(0, milliseconds)
    total_centiseconds = round(milliseconds / 10)
    hours, remainder = divmod(total_centiseconds, 360000)
    minutes, remainder = divmod(remainder, 6000)
    seconds, centiseconds = divmod(remainder, 100)
    return f'{hours}:{minutes:02d}:{seconds:02d}.{centiseconds:02d}'


def _format_srt_ms(milliseconds: int, separator: str = ',') -> str:
    milliseconds = max(0, milliseconds)
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1000)
    return f'{hours:02d}:{minutes:02d}:{seconds:02d}{separator}{millis:03d}'


def _trim_ass_text(text: str, start_ms: int, end_ms: int) -> str:
    output: list[str] = []
    in_events = False
    event_fields: list[str] | None = None
    for line in text.replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        stripped = line.strip()
        if stripped.startswith('[') and stripped.endswith(']'):
            in_events = stripped.lower() == '[events]'
            event_fields = None if not in_events else event_fields
            output.append(line)
            continue
        if in_events and stripped.lower().startswith('format:'):
            event_fields = [field.strip().lower() for field in line.split(':', 1)[1].split(',')]
            output.append(line)
            continue
        if in_events and ':' in line and line.split(':', 1)[0].strip().lower() in {'dialogue', 'comment'}:
            prefix, payload = line.split(':', 1)
            fields = event_fields or ['layer', 'start', 'end', 'style', 'name', 'marginl', 'marginr', 'marginv', 'effect', 'text']
            parts = payload.lstrip().split(',', len(fields) - 1)
            if len(parts) < len(fields) or 'start' not in fields or 'end' not in fields:
                continue
            start_index, end_index = fields.index('start'), fields.index('end')
            cue_start = _clock_to_ms(parts[start_index])
            cue_end = _clock_to_ms(parts[end_index])
            if cue_end <= start_ms or cue_start >= end_ms:
                continue
            parts[start_index] = _format_ass_ms(max(cue_start, start_ms) - start_ms)
            parts[end_index] = _format_ass_ms(min(cue_end, end_ms) - start_ms)
            output.append(f'{prefix}: ' + ','.join(parts))
            continue
        output.append(line)
    return '\n'.join(output).rstrip() + '\n'


def _trim_srt_text(text: str, start_ms: int, end_ms: int) -> str:
    blocks = re.split(r'\n\s*\n', text.replace('\r\n', '\n').replace('\r', '\n').strip())
    output: list[str] = []
    cue_number = 1
    timing = re.compile(r'^(\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})(.*)$')
    for block in blocks:
        lines = block.split('\n')
        timing_index = next((index for index, line in enumerate(lines) if '-->' in line), None)
        if timing_index is None:
            continue
        match = timing.match(lines[timing_index].strip())
        if not match:
            continue
        cue_start, cue_end = _clock_to_ms(match.group(1)), _clock_to_ms(match.group(2))
        if cue_end <= start_ms or cue_start >= end_ms:
            continue
        lines[timing_index] = (
            f'{_format_srt_ms(max(cue_start, start_ms) - start_ms)} --> '
            f'{_format_srt_ms(min(cue_end, end_ms) - start_ms)}{match.group(3)}'
        )
        body = lines[timing_index + 1:]
        output.append(str(cue_number) + '\n' + lines[timing_index] + ('\n' + '\n'.join(body) if body else ''))
        cue_number += 1
    return '\n\n'.join(output).rstrip() + '\n'


def _trim_vtt_text(text: str, start_ms: int, end_ms: int) -> str:
    normalized = text.replace('\r\n', '\n').replace('\r', '\n').strip()
    blocks = re.split(r'\n\s*\n', normalized)
    output: list[str] = []
    timing = re.compile(r'^(?:(\d{1,2}):)?(\d{2}):(\d{2}[.]\d{1,3})\s*-->\s*(?:(\d{1,2}):)?(\d{2}):(\d{2}[.]\d{1,3})(.*)$')
    for index, block in enumerate(blocks):
        lines = block.split('\n')
        if index == 0 and lines and lines[0].lstrip('\ufeff').startswith('WEBVTT'):
            output.append(block.lstrip('\ufeff'))
            continue
        timing_index = next((i for i, line in enumerate(lines) if '-->' in line), None)
        if timing_index is None:
            if lines and lines[0].strip().upper().startswith(('NOTE', 'STYLE', 'REGION')):
                output.append(block)
            continue
        match = timing.match(lines[timing_index].strip())
        if not match:
            continue
        left = f'{match.group(1) or "0"}:{match.group(2)}:{match.group(3)}'
        right = f'{match.group(4) or "0"}:{match.group(5)}:{match.group(6)}'
        cue_start, cue_end = _clock_to_ms(left), _clock_to_ms(right)
        if cue_end <= start_ms or cue_start >= end_ms:
            continue
        lines[timing_index] = (
            f'{_format_srt_ms(max(cue_start, start_ms) - start_ms, ".")} --> '
            f'{_format_srt_ms(min(cue_end, end_ms) - start_ms, ".")}{match.group(7)}'
        )
        output.append('\n'.join(lines))
    if not output or not output[0].startswith('WEBVTT'):
        output.insert(0, 'WEBVTT')
    return '\n\n'.join(output).rstrip() + '\n'


def trim_external_subtitle(source: Path, target: Path, start_seconds: float, end_seconds: float) -> None:
    start_ms = round(start_seconds * 1000)
    end_ms = round(end_seconds * 1000)
    text = _read_subtitle_text(source)
    suffix = source.suffix.lower()
    if suffix in {'.ass', '.ssa'}:
        result = _trim_ass_text(text, start_ms, end_ms)
    elif suffix == '.srt':
        result = _trim_srt_text(text, start_ms, end_ms)
    elif suffix == '.vtt':
        result = _trim_vtt_text(text, start_ms, end_ms)
    else:
        raise MediaError('Subtitle must be ASS, SSA, SRT, or VTT.')
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(result, encoding='utf-8')


def _map_arguments(probe: dict[str, Any], output_format: str, subtitle_input_count: int) -> tuple[list[str], int]:
    args: list[str] = []
    subtitle_count = subtitle_input_count
    if output_format == 'mkv':
        args.extend(['-map', '0:v?', '-map', '0:a?'])
        # Text subtitle streams are extracted, trimmed, and re-added as separate inputs.
        # Bitmap subtitle streams cannot be text-trimmed, so preserve them from the source.
        for stream in probe.get('streams', []):
            if stream.get('codec_type') == 'subtitle' and stream.get('codec_name') not in TEXT_SUBTITLE_CODECS:
                args.extend(['-map', f"0:{stream.get('index')}"])
                subtitle_count += 1
        args.extend(['-map', '0:d?'])
    else:
        for stream in probe.get('streams', []):
            if stream.get('codec_type') in {'video', 'audio'}:
                args.extend(['-map', f"0:{stream.get('index')}"])
    for input_index in range(1, subtitle_input_count + 1):
        args.extend(['-map', f'{input_index}:0?'])
    return args, subtitle_count


def _video_filter(anime_filter: bool) -> str | None:
    if anime_filter:
        return 'hqdn3d=0:0:3:3,gradfun,unsharp'
    return None


def _common_output_args(output_format: str, subtitle_count: int) -> list[str]:
    args = ['-map_metadata', '0', '-map_chapters', '0', '-avoid_negative_ts', 'make_zero', '-fflags', '+genpts']
    if output_format == 'mkv':
        args.extend(['-c:s', 'copy', '-c:d', 'copy', '-f', 'matroska'])
    elif output_format == 'mp4':
        if subtitle_count:
            args.extend(['-c:s', 'mov_text'])
        args.extend(['-movflags', '+faststart', '-f', 'mp4'])
    else:
        if subtitle_count:
            args.extend(['-c:s', 'webvtt'])
        args.extend(['-f', 'webm'])
    return args


def build_commands(
    *,
    input_path: Path,
    output_path: Path,
    subtitle_paths: list[Path],
    attachments: list[dict[str, str]],
    start_time: str,
    end_time: str,
    output_format: str,
    preset: str,
    crf: int,
    audio_mode: str,
    anime_filter: bool,
    probe: dict[str, Any],
    passlog_path: Path,
) -> list[list[str]]:
    validate_preset(preset)
    if output_format not in SUPPORTED_FORMATS:
        raise MediaError('Unsupported output format.')
    start_seconds, end_seconds = validate_range(start_time, end_time)
    duration = end_seconds - start_seconds
    if not 0 <= crf <= 51:
        raise MediaError('CRF must be between 0 and 51.')
    if audio_mode not in {'copy', 'encode'}:
        raise MediaError('Unsupported audio mode.')
    if output_format == 'webm' and preset not in {'copy', 'animethemes-vp9'}:
        raise MediaError('WebM output supports Copy or AnimeThemes VP9 preset.')
    if output_format == 'webm' and preset == 'copy':
        codecs = {stream.get('codec_name') for stream in probe.get('streams', [])}
        if not codecs.intersection({'vp8', 'vp9', 'av1'}):
            raise MediaError('Stream copy to WebM requires a WebM-compatible source video codec.')

    base = [
        'ffmpeg',
        '-hide_banner',
        '-y',
        '-nostats',
        '-progress',
        'pipe:1',
        '-i',
        str(input_path),
    ]
    for subtitle_path in subtitle_paths:
        # Each subtitle is already trimmed to a 00:00:00-based clip. Offset it back
        # to the source timeline so the shared output -ss/-to cuts all streams equally.
        base.extend(['-itsoffset', format_time(start_seconds), '-i', str(subtitle_path)])
    base.extend(['-ss', format_time(start_seconds), '-to', format_time(end_seconds)])

    map_args, subtitle_count = _map_arguments(probe, output_format, len(subtitle_paths))
    common = _common_output_args(output_format, subtitle_count)
    attachment_args: list[str] = []
    if output_format == 'mkv':
        for attachment_index, attachment in enumerate(attachments):
            attachment_args.extend([
                '-attach', attachment['path'],
                '-metadata:s:t:' + str(attachment_index), 'filename=' + attachment['filename'],
                '-metadata:s:t:' + str(attachment_index), 'mimetype=' + attachment['mimetype'],
            ])
    vf = _video_filter(anime_filter)

    if preset == 'copy':
        if vf:
            raise MediaError('AnimeThemes filters cannot be used with stream copy.')
        return [base + map_args + ['-c', 'copy'] + common + attachment_args + [str(output_path)]]

    video_args: list[str]
    if preset.startswith('x264-'):
        speed = preset.split('-', 1)[1]
        video_args = ['-c:v', 'libx264', '-preset', speed, '-crf', str(crf), '-pix_fmt', 'yuv420p']
    elif preset.startswith('x265-'):
        speed = preset.split('-', 1)[1]
        video_args = ['-c:v', 'libx265', '-preset', speed, '-crf', str(crf), '-pix_fmt', 'yuv420p10le']
    else:
        audio_bitrate = get_audio_bitrate(probe)
        gop = get_keyframe_interval(duration)
        null_target = 'NUL' if os.name == 'nt' else '/dev/null'
        first_pass = base + [
            '-map', '0:v:0',
            '-an', '-sn', '-dn',
            '-c:v', 'libvpx-vp9',
            '-b:v', '0',
            '-crf', str(crf),
            '-pass', '1',
            '-passlogfile', str(passlog_path),
            '-cpu-used', '4',
            '-g', str(gop),
            '-threads', str(max(1, os.cpu_count() or 2)),
            '-tile-columns', '6',
            '-frame-parallel', '0',
            '-auto-alt-ref', '1',
            '-lag-in-frames', '25',
            '-row-mt', '1',
            '-pix_fmt', 'yuv420p',
        ]
        if vf:
            first_pass.extend(['-vf', vf])
        first_pass.extend(['-f', 'webm', null_target])

        second_pass = base + map_args + [
            '-c:v', 'libvpx-vp9',
            '-b:v', '0',
            '-crf', str(crf),
            '-pass', '2',
            '-passlogfile', str(passlog_path),
            '-cpu-used', '0',
            '-g', str(gop),
            '-threads', str(max(1, os.cpu_count() or 2)),
            '-tile-columns', '6',
            '-frame-parallel', '0',
            '-auto-alt-ref', '1',
            '-lag-in-frames', '25',
            '-row-mt', '1',
            '-pix_fmt', 'yuv420p',
        ]
        if vf:
            second_pass.extend(['-vf', vf])
        if audio_mode == 'copy' and output_format == 'mkv':
            second_pass.extend(['-c:a', 'copy'])
        else:
            second_pass.extend(['-c:a', 'libopus', '-b:a', audio_bitrate, '-ar', '48k'])
        second_pass.extend(common)
        second_pass.extend(attachment_args)
        second_pass.append(str(output_path))
        return [first_pass, second_pass]

    if vf:
        video_args.extend(['-vf', vf])
    if audio_mode == 'copy' and output_format == 'mkv':
        audio_args = ['-c:a', 'copy']
    elif output_format == 'webm':
        audio_args = ['-c:a', 'libopus', '-b:a', '192k', '-ar', '48k']
    else:
        audio_args = ['-c:a', 'aac', '-b:a', '192k']
    return [base + map_args + video_args + audio_args + common + attachment_args + [str(output_path)]]


def command_for_display(command: list[str]) -> str:
    return shlex.join(command)
