#!/usr/bin/env python3
"""Apply a reviewed cut list to the exact public recording, without retiming."""
import argparse,hashlib,json
from pathlib import Path
from capture_readme_demo import edit

RAW_SHA='3db869173b8f74f5a42d9057e1b3d5f6c7a493db0ec24d4e40a0f8afd121e455'
# PTS in the actual WebM, inspected visually. Wall-clock action offsets are
# not assumed to equal the video's first-frame origin.
CUTS=[
 ('01-home',.6,3.2,'매장이 거절해도, 원래 주문은 남아요'),
 ('02-order',4.8,8.8,'전 매장 쿠폰과 1,000P를 적용해 주문해요'),
 ('02-busy',9.2,12.8,'주문한 매장이 늦어지면, 다른 매장을 살펴봐요'),
 ('03-refused',15.2,20.2,'새 매장이 거절하면, 원래 주문과 혜택을 유지해요'),
 ('04-automatic-recovery',23.0,29.2,'응답이 끊겨도, 서버가 같은 작업을 다시 확인해요'),
 ('05-recovered',29.2,33.2,'복구 후에도 같은 주문 · 쿠폰과 포인트 그대로'),
 ('07-pickup',37.2,42.3,'체험 시계를 진행해 수령 · 결제는 한 번만'),
 ('08-receipt',43.0,47.2,'같은 주문의 영수증 · 3,200원 / 1,000P 사용·32P 적립'),
 ('09-comparison',58.2,63.2,'취소 후 재주문과 비교 · 같았던 결과도 함께 공개해요'),
]

def main(output):
 report=json.loads((output/'capture.json').read_text())
 source=output/report['source_video']
 assert hashlib.sha256(source.read_bytes()).hexdigest()==RAW_SHA,'not the reviewed recording'
 assert report['recorded_release_commit']=='000a106cc5fda6a3be595a83f938087096c3ad63'
 assert report['same_order'] and report['recovery_without_manual_command'] and report['cash_due']==3200
 report['unadjusted_wall_clock_scenes']=report['scenes']
 report['scenes']=[dict(name=name,start=start,duration=round(end-start,3),caption=caption) for name,start,end,caption in CUTS]
 report['timeline_review']={'raw_sha256':RAW_SHA,'basis':'reviewed source-video PTS, not wall-clock action timestamps',
   'speed':1.0,'new_browser_recording':False,'application_pixels_changed':False,
   'note':'Original WebM retained. Remove only between-scene waits and virtual preparation. Captions remain outside app frame.'}
 edit(output,report)
 print(json.dumps({'duration':report['edited_duration_seconds'],'media_sha256':report['media_sha256']},ensure_ascii=False))

if __name__=='__main__':
 parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True)
 main(parser.parse_args().output)
