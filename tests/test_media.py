from pathlib import Path

import pytest

from app.media import MediaError, build_commands, format_time, parse_time, trim_external_subtitle


def sample_probe(codec: str = 'h264') -> dict:
    return {
        'format': {'duration': '120.0'},
        'streams': [
            {'index': 0, 'codec_type': 'video', 'codec_name': codec},
            {'index': 1, 'codec_type': 'audio', 'codec_name': 'aac', 'bit_rate': '192000'},
            {'index': 2, 'codec_type': 'subtitle', 'codec_name': 'ass'},
            {'index': 3, 'codec_type': 'attachment', 'codec_name': 'ttf'},
        ],
    }


def test_time_parsing_and_formatting() -> None:
    assert parse_time('01:02:03.500') == pytest.approx(3723.5)
    assert parse_time('12.25') == pytest.approx(12.25)
    assert format_time(12.25) == '00:00:12.250'


def test_invalid_time_range() -> None:
    with pytest.raises(MediaError):
        build_commands(
            input_path=Path('/tmp/input.mkv'),
            output_path=Path('/tmp/output.mkv'),
            subtitle_paths=[],
            attachments=[],
            start_time='00:00:10.000',
            end_time='00:00:05.000',
            output_format='mkv',
            preset='x264-medium',
            crf=18,
            audio_mode='copy',
            anime_filter=False,
            probe=sample_probe(),
            passlog_path=Path('/tmp/passlog'),
        )


def test_mkv_command_preserves_attachments_and_subtitles() -> None:
    commands = build_commands(
        input_path=Path('/tmp/input.mkv'),
        output_path=Path('/tmp/output.mkv'),
        subtitle_paths=[Path('/tmp/subtitle.ass')],
        attachments=[{'path': '/tmp/font.ttf', 'filename': 'font.ttf', 'mimetype': 'application/x-truetype-font'}],
        start_time='00:00:01.000',
        end_time='00:00:11.000',
        output_format='mkv',
        preset='x264-medium',
        crf=18,
        audio_mode='copy',
        anime_filter=False,
        probe=sample_probe(),
        passlog_path=Path('/tmp/passlog'),
    )
    command = commands[0]
    assert ['-ss', '00:00:01.000'] == command[command.index('-ss'):command.index('-ss') + 2]
    assert ['-to', '00:00:11.000'] == command[command.index('-to'):command.index('-to') + 2]
    assert '0:t?' not in command
    assert '-attach' in command
    assert '/tmp/font.ttf' in command
    assert 'copy' in command


def test_animethemes_preset_is_two_pass() -> None:
    commands = build_commands(
        input_path=Path('/tmp/input.mkv'),
        output_path=Path('/tmp/output.mkv'),
        subtitle_paths=[],
        attachments=[],
        start_time='0',
        end_time='90',
        output_format='mkv',
        preset='animethemes-vp9',
        crf=18,
        audio_mode='encode',
        anime_filter=True,
        probe=sample_probe(),
        passlog_path=Path('/tmp/passlog'),
    )
    assert len(commands) == 2
    assert commands[0][commands[0].index('-cpu-used') + 1] == '4'
    assert commands[1][commands[1].index('-cpu-used') + 1] == '0'
    assert 'hqdn3d=0:0:3:3,gradfun,unsharp' in commands[1]


def test_subtitle_trim_and_rebase(tmp_path: Path) -> None:
    source = tmp_path / 'source.ass'
    target = tmp_path / 'trimmed.ass'
    source.write_text(
        '[Script Info]\nScriptType: v4.00+\n\n[V4+ Styles]\n'
        'Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n'
        'Style: Default,Arial,20,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,2,10,10,10,1\n\n'
        '[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n'
        'Dialogue: 0,0:00:04.00,0:00:08.00,Default,,0,0,0,,Before and inside\n'
        'Dialogue: 0,0:00:11.00,0:00:13.00,Default,,0,0,0,,Inside\n',
        encoding='utf-8',
    )
    trim_external_subtitle(source, target, 5, 12)
    content = target.read_text(encoding='utf-8')
    assert '0:00:00.00' in content
    assert 'Before and inside' in content
    assert 'Inside' in content
