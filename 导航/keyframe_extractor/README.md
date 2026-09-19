# Walking Video 关键帧提取器

把行走视频转换成适合后续 **VGGT 相机位姿恢复** 和 **VLM 地标识别** 的关键帧序列。

## 它做了什么

- 每隔一小段时间解码一帧，避免逐帧分析造成浪费。
- 用缩略灰度图计算画面变化和清晰度。
- `adaptive` 模式在视角发生明显变化时选帧，同时用最大间隔保证轨迹连续。
- `interval` 模式在每个固定时间窗口选择最清晰的一帧。
- 保留原始分辨率，并记录视频时间戳和原始帧号。
- 输出 JSON、CSV 和缩略接触表。

## 安装

项目上层现有的 `.venv` 已包含所需依赖，可直接使用：

```bash
cd /Users/xujiawei/Projects/导航
./.venv/bin/python keyframe_extractor/extract_keyframes.py --help
```

如果要创建独立环境：

```bash
cd /Users/xujiawei/Projects/导航/keyframe_extractor
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 推荐用法

处理当前目录的 `1.mp4`：

```bash
cd /Users/xujiawei/Projects/导航
./.venv/bin/python keyframe_extractor/extract_keyframes.py \
  1.mp4 \
  -o keyframe_extractor/output/1 \
  --overwrite
```

默认参数适合普通步行视频：关键帧至少相隔 0.5 秒、至多相隔 2 秒；画面变化足够大时提前选帧。

固定每秒选一张最清晰的画面：

```bash
./.venv/bin/python keyframe_extractor/extract_keyframes.py \
  1.mp4 -o keyframe_extractor/output/1_interval \
  --mode interval --interval 1.0 --overwrite
```

## 常用调参

| 目标 | 参数 |
|---|---|
| 输出更多关键帧 | 降低 `--novelty-threshold`，例如 `0.08` |
| 输出更少关键帧 | 提高 `--novelty-threshold`，例如 `0.15` |
| 增加 VGGT 相邻帧重叠 | 减小 `--max-gap`，例如 `1.2` |
| 跳过更多模糊画面 | 提高 `--min-sharpness`，例如 `0.001` |
| 限制快速试跑数量 | `--max-frames 20` |

室内、走廊或白墙场景纹理较少，建议把 `--max-gap` 设为 `1.0`～`1.5` 秒。快速转弯较多的视频，可把 `--sample-every` 减小到 `0.1` 秒。

## 输出结构

```text
output/1/
├── frames/
│   ├── frame_00000_t000000.000.jpg
│   └── ...
├── contact_sheet.jpg
├── keyframes.csv
└── keyframes.json
```

`keyframes.json` 是后续流程的主入口。每帧包含：

- `frame_number`：解码帧序号；
- `timestamp_sec`：视频时间戳；
- `sharpness`：清晰度分数；
- `novelty`：相对上一关键帧的视觉变化；
- `reason`：被选中的原因；
- `filename`：关键帧图片相对路径。

## 给 VGGT 的建议

VGGT 需要连续视角之间有足够重叠，因此不能只保留“场景切换帧”。先看 `contact_sheet.jpg`：如果相邻图片跨度过大，就降低 `--max-gap`；如果画面过于重复，则提高 `--novelty-threshold`。正式跑长视频前，可先截取一小段验证参数。
