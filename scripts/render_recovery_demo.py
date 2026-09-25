#!/usr/bin/env python3
"""Cut only captured scenes at normal speed, with source timing and hashes."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess


def command(*args):
    return subprocess.run(args, check=True, text=True, capture_output=True).stdout


def render(folder:Path):
    report=json.loads((folder/'capture.json').read_text())
    source=folder/report['raw_video']
    assert hashlib.sha256(source.read_bytes()).hexdigest()==report['raw_sha256']
    font='/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'
    assert Path(font).exists()
    segments=[]
    footer=folder/'footer.txt';footer.write_text('실제 주문·결제가 아닌 가상 체험이에요. 화면은 실제 공개 사이트에서 촬영했어요.')
    for index,scene in enumerate(report['scenes']):
        text=folder/f'caption-{index}.txt';text.write_text(scene['caption'])
        target=folder/f'segment-{index:02}.mp4';segments.append(target)
        duration=scene['end']-scene['start'];assert duration>0
        vf=(f'scale=1280:900,pad=1280:978:0:0:color=0x153e35,'
            f'drawtext=fontfile={font}:textfile={text}:fontsize=24:fontcolor=white:x=(w-tw)/2:y=912,'
            f'drawtext=fontfile={font}:textfile={footer}:fontsize=16:fontcolor=0xd7ec99:x=(w-tw)/2:y=953')
        command('ffmpeg','-y','-v','error','-ss',str(scene['start']),'-i',str(source),'-t',str(duration),
                '-vf',vf,'-an','-r','25','-c:v','libx264','-preset','medium','-crf','19','-pix_fmt','yuv420p',str(target))
    listing=folder/'concat.txt';listing.write_text(''.join(f"file '{x.resolve()}'\n" for x in segments))
    video=folder/'pickup-pact-recovery.mp4'
    command('ffmpeg','-y','-v','error','-f','concat','-safe','0','-i',str(listing),'-c','copy','-movflags','+faststart',str(video))
    command('ffmpeg','-v','error','-xerror','-i',str(video),'-f','null','-')
    preview=folder/'pickup-pact-recovery.gif'
    command('ffmpeg','-y','-v','error','-i',str(video),'-filter_complex','fps=5,scale=800:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=96:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4','-loop','0',str(preview))
    command('ffmpeg','-v','error','-xerror','-i',str(preview),'-f','null','-')
    info=json.loads(command('ffprobe','-v','error','-show_entries','format=duration:stream=width,height,nb_frames,r_frame_rate','-of','json',str(video)))
    report.update(edited_duration_seconds=float(info['format']['duration']),
                  edited_probe=info,full_video_decode='passed',full_gif_decode='passed',
                  playback_speed=1,media_sha256={x.name:hashlib.sha256(x.read_bytes()).hexdigest() for x in (video,preview)})
    offset=0
    for scene,segment in zip(report['scenes'],segments):
        duration=float(json.loads(command('ffprobe','-v','error','-show_entries','format=duration','-of','json',str(segment)))['format']['duration'])
        scene.update(edited_start=offset,edited_end=offset+duration);offset+=duration
    (folder/'capture.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    return report

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('folder',type=Path);a=p.parse_args();print(json.dumps(render(a.folder),ensure_ascii=False,indent=2))
