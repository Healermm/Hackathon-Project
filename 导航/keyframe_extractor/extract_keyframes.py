#!/usr/bin/env python3
"""Extract stable, non-redundant keyframes from a walking video.

The default adaptive mode is designed for downstream camera-pose recovery:
it keeps enough visual overlap while forcing a frame when the gap gets large.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import shutil
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator, Optional

import av
import numpy as np
from PIL import Image, ImageDraw, ImageOps
from tqdm import tqdm


@dataclass
class Candidate:
    frame_number: int
    timestamp: float
    rgb: np.ndarray
    gray: np.ndarray
    sharpness: float
    novelty: float


@dataclass
class KeyframeRecord:
    index: int
    filename: str
    frame_number: int
    timestamp_sec: float
    sharpness: float
    novelty: float
    reason: str
    width: int
    height: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="从 walking video 中提取适合 VGGT/VLM 的关键帧。"
    )
    parser.add_argument("video", type=Path, help="输入视频路径")
    parser.add_argument("-o", "--output", type=Path, default=Path("output/keyframes"))
    parser.add_argument("--mode", choices=("adaptive", "interval"), default="adaptive")
    parser.add_argument("--sample-every", type=float, default=0.20,
                        help="每隔多少秒分析一帧，默认 0.20")
    parser.add_argument("--min-gap", type=float, default=0.50,
                        help="adaptive 模式关键帧最小间隔，默认 0.50 秒")
    parser.add_argument("--max-gap", type=float, default=2.00,
                        help="adaptive 模式关键帧最大间隔，默认 2.00 秒")
    parser.add_argument("--novelty-threshold", type=float, default=0.105,
                        help="画面变化阈值，越小输出越多，默认 0.105")
    parser.add_argument("--interval", type=float, default=1.00,
                        help="interval 模式的窗口长度，默认 1 秒")
    parser.add_argument("--min-sharpness", type=float, default=0.0003,
                        help="最低清晰度；低于它的画面尽量不选")
    parser.add_argument("--analysis-width", type=int, default=320,
                        help="分析用缩略图宽度，不影响输出分辨率")
    parser.add_argument("--jpeg-quality", type=int, default=93)
    parser.add_argument("--max-frames", type=int, default=0,
                        help="最多输出多少帧；0 表示不限制")
    parser.add_argument("--overwrite", action="store_true", help="覆盖已有输出目录")
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    if not args.video.is_file():
        raise ValueError(f"视频不存在: {args.video}")
    positive = ("sample_every", "min_gap", "max_gap", "interval", "analysis_width")
    for name in positive:
        if getattr(args, name) <= 0:
            raise ValueError(f"--{name.replace('_', '-')} 必须大于 0")
    if args.min_gap > args.max_gap:
        raise ValueError("--min-gap 不能大于 --max-gap")
    if not 1 <= args.jpeg_quality <= 100:
        raise ValueError("--jpeg-quality 必须在 1 到 100 之间")


def video_info(video: Path) -> dict:
    with av.open(str(video)) as container:
        stream = container.streams.video[0]
        fps = float(stream.average_rate) if stream.average_rate else None
        duration = (
            float(stream.duration * stream.time_base)
            if stream.duration is not None and stream.time_base is not None
            else (float(container.duration / av.time_base) if container.duration else None)
        )
        return {
            "path": str(video.resolve()),
            "codec": stream.codec_context.name,
            "width": stream.codec_context.width,
            "height": stream.codec_context.height,
            "fps": fps,
            "duration_sec": duration,
            "declared_frames": stream.frames or None,
        }


def analysis_gray(rgb: np.ndarray, width: int) -> np.ndarray:
    image = Image.fromarray(rgb)
    height = max(1, round(image.height * width / image.width))
    image = image.resize((width, height), Image.Resampling.BILINEAR).convert("L")
    return np.asarray(image, dtype=np.float32) / 255.0


def sharpness_score(gray: np.ndarray) -> float:
    """Mean squared spatial gradient; higher means sharper."""
    dx = np.diff(gray, axis=1)
    dy = np.diff(gray, axis=0)
    return float((np.mean(dx * dx) + np.mean(dy * dy)) / 2.0)


def novelty_score(gray: np.ndarray, reference: Optional[np.ndarray]) -> float:
    """Combine pixel change and luminance-histogram distance in [0, 1]."""
    if reference is None:
        return 1.0
    pixel_change = float(np.mean(np.abs(gray - reference)))
    hist_a, _ = np.histogram(gray, bins=32, range=(0.0, 1.0), density=True)
    hist_b, _ = np.histogram(reference, bins=32, range=(0.0, 1.0), density=True)
    hist_a /= max(hist_a.sum(), 1e-9)
    hist_b /= max(hist_b.sum(), 1e-9)
    hist_distance = float(np.abs(hist_a - hist_b).sum() / 2.0)
    return min(1.0, 0.75 * pixel_change + 0.25 * hist_distance)


def iter_sampled_frames(video: Path, sample_every: float) -> Iterator[tuple[int, float, np.ndarray]]:
    with av.open(str(video)) as container:
        stream = container.streams.video[0]
        fallback_fps = float(stream.average_rate) if stream.average_rate else 30.0
        next_sample = 0.0
        for frame_number, frame in enumerate(container.decode(stream)):
            timestamp = (
                float(frame.pts * frame.time_base)
                if frame.pts is not None and frame.time_base is not None
                else frame_number / fallback_fps
            )
            if timestamp + 1e-6 < next_sample:
                continue
            yield frame_number, timestamp, frame.to_ndarray(format="rgb24")
            next_sample = timestamp + sample_every


def make_candidate(frame_number: int, timestamp: float, rgb: np.ndarray,
                   reference: Optional[np.ndarray], analysis_width: int) -> Candidate:
    gray = analysis_gray(rgb, analysis_width)
    return Candidate(
        frame_number=frame_number,
        timestamp=timestamp,
        rgb=rgb,
        gray=gray,
        sharpness=sharpness_score(gray),
        novelty=novelty_score(gray, reference),
    )


def choose_adaptive(args: argparse.Namespace, progress: tqdm) -> Iterator[tuple[Candidate, str]]:
    selected_time: Optional[float] = None
    reference: Optional[np.ndarray] = None
    best: Optional[Candidate] = None

    for frame_number, timestamp, rgb in iter_sampled_frames(args.video, args.sample_every):
        candidate = make_candidate(frame_number, timestamp, rgb, reference, args.analysis_width)
        progress.update(1)
        if selected_time is None:
            yield candidate, "first_frame"
            selected_time, reference = timestamp, candidate.gray
            continue

        gap = timestamp - selected_time
        if gap < args.min_gap:
            continue

        # Keep the strongest fallback. Novelty matters most, sharpness breaks ties.
        quality = candidate.novelty + min(candidate.sharpness / args.min_sharpness, 2.0) * 0.025
        if best is None:
            best = candidate
        else:
            best_quality = best.novelty + min(best.sharpness / args.min_sharpness, 2.0) * 0.025
            if quality > best_quality:
                best = candidate

        if candidate.novelty >= args.novelty_threshold and candidate.sharpness >= args.min_sharpness:
            yield candidate, "visual_change"
            selected_time, reference, best = timestamp, candidate.gray, None
        elif gap >= args.max_gap:
            chosen = best or candidate
            reason = "max_gap" if chosen.sharpness >= args.min_sharpness else "max_gap_blurry"
            yield chosen, reason
            selected_time, reference, best = chosen.timestamp, chosen.gray, None


def choose_interval(args: argparse.Namespace, progress: tqdm) -> Iterator[tuple[Candidate, str]]:
    window_index = -1
    best: Optional[Candidate] = None
    reference: Optional[np.ndarray] = None
    for frame_number, timestamp, rgb in iter_sampled_frames(args.video, args.sample_every):
        current_window = int(timestamp // args.interval)
        if current_window != window_index:
            if best is not None:
                yield best, "sharpest_in_interval"
                reference = best.gray
            window_index, best = current_window, None
        candidate = make_candidate(frame_number, timestamp, rgb, reference, args.analysis_width)
        progress.update(1)
        if best is None or candidate.sharpness > best.sharpness:
            best = candidate
    if best is not None:
        yield best, "sharpest_in_interval"


def save_candidate(candidate: Candidate, reason: str, index: int, frames_dir: Path,
                   jpeg_quality: int) -> KeyframeRecord:
    filename = f"frame_{index:05d}_t{candidate.timestamp:010.3f}.jpg"
    image = Image.fromarray(candidate.rgb)
    image.save(frames_dir / filename, "JPEG", quality=jpeg_quality, subsampling=0)
    return KeyframeRecord(
        index=index,
        filename=f"frames/{filename}",
        frame_number=candidate.frame_number,
        timestamp_sec=round(candidate.timestamp, 6),
        sharpness=round(candidate.sharpness, 7),
        novelty=round(candidate.novelty, 7),
        reason=reason,
        width=image.width,
        height=image.height,
    )


def create_contact_sheet(records: list[KeyframeRecord], output: Path) -> None:
    if not records:
        return
    thumb_w, thumb_h, label_h, columns = 240, 135, 28, 4
    rows = math.ceil(len(records) / columns)
    sheet = Image.new("RGB", (columns * thumb_w, rows * (thumb_h + label_h)), "white")
    draw = ImageDraw.Draw(sheet)
    for i, record in enumerate(records):
        image = Image.open(output / record.filename).convert("RGB")
        thumb = ImageOps.fit(image, (thumb_w, thumb_h), method=Image.Resampling.LANCZOS)
        x, y = (i % columns) * thumb_w, (i // columns) * (thumb_h + label_h)
        sheet.paste(thumb, (x, y))
        draw.text((x + 6, y + thumb_h + 6), f"#{record.index}  {record.timestamp_sec:.2f}s", fill="black")
    sheet.save(output / "contact_sheet.jpg", "JPEG", quality=90)


def write_metadata(records: list[KeyframeRecord], info: dict, args: argparse.Namespace) -> None:
    config = {
        "mode": args.mode,
        "sample_every_sec": args.sample_every,
        "min_gap_sec": args.min_gap,
        "max_gap_sec": args.max_gap,
        "novelty_threshold": args.novelty_threshold,
        "interval_sec": args.interval,
        "min_sharpness": args.min_sharpness,
        "analysis_width": args.analysis_width,
        "jpeg_quality": args.jpeg_quality,
    }
    payload = {"schema_version": "1.0", "source": info, "config": config,
               "keyframe_count": len(records), "keyframes": [asdict(r) for r in records]}
    (args.output / "keyframes.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    with (args.output / "keyframes.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(asdict(records[0]).keys()) if records else ["index"])
        writer.writeheader()
        writer.writerows(asdict(record) for record in records)


def main() -> int:
    args = parse_args()
    try:
        validate_args(args)
        info = video_info(args.video)
        if args.output.exists():
            if not args.overwrite:
                raise ValueError(f"输出目录已存在: {args.output}；如需覆盖请加 --overwrite")
            shutil.rmtree(args.output)
        frames_dir = args.output / "frames"
        frames_dir.mkdir(parents=True)

        total = math.ceil(info["duration_sec"] / args.sample_every) if info["duration_sec"] else None
        records: list[KeyframeRecord] = []
        with tqdm(total=total, unit="sample", desc="分析视频") as progress:
            chooser = choose_adaptive(args, progress) if args.mode == "adaptive" else choose_interval(args, progress)
            for candidate, reason in chooser:
                records.append(save_candidate(candidate, reason, len(records), frames_dir, args.jpeg_quality))
                if args.max_frames and len(records) >= args.max_frames:
                    break

        write_metadata(records, info, args)
        create_contact_sheet(records, args.output)
        print(f"完成：提取 {len(records)} 个关键帧")
        print(f"图片：{frames_dir.resolve()}")
        print(f"元数据：{(args.output / 'keyframes.json').resolve()}")
        return 0
    except (ValueError, av.error.FFmpegError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
