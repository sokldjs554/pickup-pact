#!/usr/bin/env python3
"""Re-caption the reviewed recording; leave application content/timing untouched.

This edits an existing recording, not a new browser session. The input SHA and
frame range bind this correction to the exact published 38.16-second video.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile

INPUT_SHA = '06dab7cf7f29ef1ec8ec42c7f6ee1c365285defc0e7b8b9baaf41768ed935fd3'
OLD_CAPTION = '11분 빠른 도착 예상 / 쿠폰 상실로 1,400원 추가'
NEW_CAPTION = '매장 변경: 메뉴 차액 400원 + 전용 쿠폰 미적용 1,000원 = 1,400원 추가'
FIRST_FRAME, LAST_FRAME = 426, 601


def run(*args: str) -> str:
    return subprocess.run(args, check=True, text=True, capture_output=True).stdout


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def probe(path: Path) -> dict:
    return json.loads(run('ffprobe', '-v', 'error', '-select_streams', 'v:0',
        '-count_frames', '-show_entries', 'stream=width,height,r_frame_rate,nb_read_frames:format=duration',
        '-of', 'json', str(path)))


def correct(source: Path, output: Path, manifest: Path) -> dict:
    if sha(source) != INPUT_SHA:
        raise ValueError('Input does not match the reviewed published video; nothing edited')
    before = probe(source)
    stream = before['streams'][0]
    if (stream['width'], stream['height'], stream['r_frame_rate'], stream['nb_read_frames']) != (1280, 978, '25/1', '954'):
        raise ValueError('Unexpected video geometry, timing or frame count')
    report = json.loads(manifest.read_text(encoding='utf-8'))
    scene = next(s for s in report['scenes'] if s['name'] == '05-consent')
    if scene['caption'] != OLD_CAPTION:
        raise ValueError('The recording manifest does not contain the caption being corrected')
    output.mkdir(parents=True, exist_ok=True)
    video = output / 'pickup-pact-demo.mp4'
    preview = output / 'pickup-pact-preview.gif'
    if video.resolve() == source.resolve():
        raise ValueError('Refusing to overwrite source during encoding')
    font = Path('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc')
    if not font.exists():
        raise RuntimeError('Korean caption font missing')
    with tempfile.TemporaryDirectory(prefix='caption-') as folder:
        text = Path(folder) / 'caption.txt'
        text.write_text(NEW_CAPTION, encoding='utf-8')
        enabled = f"between(n,{FIRST_FRAME},{LAST_FRAME})"
        filters = (f"drawbox=x=0:y=900:w=1280:h=50:color=0x153e35:t=fill:enable='{enabled}',"
            f"drawtext=fontfile={font}:textfile={text}:fontsize=23:fontcolor=white:"
            f"x=(w-tw)/2:y=920:enable='{enabled}'")
        run('ffmpeg', '-y', '-v', 'error', '-i', str(source), '-vf', filters,
            '-an', '-c:v', 'libx264', '-preset', 'medium', '-crf', '18',
            '-pix_fmt', 'yuv420p', '-r', '25', '-movflags', '+faststart', str(video))
    after = probe(video)
    if before != after:
        raise AssertionError((before, after))
    run('ffmpeg', '-v', 'error', '-xerror', '-i', str(video), '-f', 'null', '-')
    run('ffmpeg', '-y', '-v', 'error', '-i', str(video), '-filter_complex',
        'fps=5,scale=800:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=96:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4',
        '-loop', '0', str(preview))
    run('ffmpeg', '-v', 'error', '-xerror', '-i', str(preview), '-f', 'null', '-')
    # Keep original scene timing, order, payment facts and recording provenance.
    scene['caption'] = NEW_CAPTION
    report['caption_correction'] = {
        'date': '2026-09-25', 'source_mp4_sha256': INPUT_SHA,
        'old_caption': OLD_CAPTION, 'new_caption': NEW_CAPTION,
        'first_frame': FIRST_FRAME, 'last_frame_inclusive': LAST_FRAME,
        'start_seconds': FIRST_FRAME / 25, 'end_seconds_exclusive': (LAST_FRAME + 1) / 25,
        'changed_area': 'caption band only: y=900..949; original footer retained',
        'reencoded': True, 'new_browser_recording': False,
        'reason': '400 KRW menu difference plus 1000 KRW store-specific coupon not applicable; not time-expiry surcharge',
    }
    report['media_sha256'][video.name] = sha(video)
    report['media_sha256'][preview.name] = sha(preview)
    (output / 'capture.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    check = {'input_sha256': INPUT_SHA, 'video_sha256': sha(video),
        'gif_sha256': sha(preview), 'geometry_timing_frame_count_unchanged': before == after,
        'duration_seconds': float(after['format']['duration']),
        'full_video_decode': 'passed', 'full_gif_decode': 'passed',
        'caption': NEW_CAPTION}
    (output / 'caption-verification.json').write_text(json.dumps(check, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return check


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path('docs/media/pickup-pact-demo.mp4'))
    parser.add_argument('--manifest', type=Path, default=Path('docs/media/capture.json'))
    parser.add_argument('--output', type=Path, default=Path('verification/corrected-caption'))
    args = parser.parse_args()
    print(json.dumps(correct(args.source, args.output, args.manifest), ensure_ascii=False, indent=2))
