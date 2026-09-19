#!/usr/bin/env python3
"""检查 iPhone 视频的解码尺寸、帧率、时长和旋转元数据。"""
import json, sys
from pathlib import Path
import av

if len(sys.argv) != 2:
    raise SystemExit('用法: python3 video_probe.py /path/to/input.mp4')
path = Path(sys.argv[1])
with av.open(str(path)) as c:
    v = c.streams.video[0]
    result = {
        'file': str(path), 'width': v.width, 'height': v.height,
        'fps': float(v.average_rate) if v.average_rate else None,
        'frames': v.frames, 'duration_seconds': float(c.duration / av.time_base) if c.duration else None,
        'has_audio': bool(c.streams.audio), 'metadata': dict(c.metadata),
        'video_metadata': dict(v.metadata),
    }
print(json.dumps(result, ensure_ascii=False, indent=2))
