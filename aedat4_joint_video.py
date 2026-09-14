import argparse
import csv
import hashlib
import json
import subprocess
import time
import tempfile
from pathlib import Path

import aedat
import imageio_ffmpeg
import numpy as np
from PIL import Image


def digest(path):
    with path.open("rb") as stream:
        h = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def render_videos(events, timestamps, gray_frames, exposures, midpoints, begin, end, count, fps, output, notify, outputs):
    height, width = gray_frames.shape[1:]
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-n", "-f", "rawvideo", "-pix_fmt", "rgb24",
               "-s", f"{width * 2}x{height}", "-r", str(fps), "-i", "pipe:0"]
    names = {"joint": "01 事件与灰度联合视频.mp4", "events": "02 红绿事件视频.mp4", "gray": "03 灰度视频.mp4"}
    for kind in outputs:
        command += ["-map", "0:v:0"]
        if kind != "joint":
            x = 0 if kind == "events" else width
            command += ["-vf", f"crop={width}:{height}:{x}:0"]
        command += ["-an", "-c:v", "libx264", "-preset", "fast", "-crf", "15", "-pix_fmt",
                    "yuv420p", "-fps_mode", "passthrough", "-movflags", "+faststart", str(output / names[kind])]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    best, best_score, max_error, repeated, gaps, previous = None, -1, 0, 0, 0, None
    notify(20, "生成联合画面并编码所选MP4")
    with (output / "联合编码日志.txt").open("wb") as log, (output / "04 时间对应表.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["输出帧号", "播放时间秒", "事件窗起点微秒", "事件窗终点微秒", "灰度原始帧号", "曝光起点微秒", "曝光终点微秒", "曝光中点与事件窗中心差微秒", "重复上一灰度帧", "匹配差超过一个输出周期"])
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=log, creationflags=flags)
        try:
            for i in range(count):
                start = begin + (i * 1_000_000 + fps - 1) // fps
                stop = begin + ((i + 1) * 1_000_000 + fps - 1) // fps
                center = (start + stop) // 2
                index = int(np.searchsorted(midpoints, center))
                chosen = min((k for k in (index - 1, index) if 0 <= k < len(midpoints)), key=lambda k: abs(int(midpoints[k]) - center))
                error = int(midpoints[chosen]) - center
                left, right = np.searchsorted(timestamps, [start, stop])
                rendered = np.zeros((height, width, 3), dtype=np.uint8)
                for offset in range(int(left), int(right), 250_000):
                    part = events[offset:min(offset + 250_000, int(right))]
                    pixels = part["y"].astype(np.int64) * width + part["x"]
                    _, reverse_indices = np.unique(pixels[::-1], return_index=True)
                    selected = part[len(part) - 1 - reverse_indices]
                    rendered[selected["y"], selected["x"], 0] = np.where(selected["on"], 0, 255)
                    rendered[selected["y"], selected["x"], 1] = np.where(selected["on"], 255, 0)
                gray = np.repeat(gray_frames[chosen, :, :, None], 3, axis=2)
                combined = np.concatenate([rendered, gray], axis=1)
                process.stdin.write(combined.tobytes())
                event_number = int(right - left)
                if event_number > best_score:
                    best, best_score = combined.copy(), event_number
                duplicate = previous == chosen
                gap = abs(error) > 1_000_000 / fps
                max_error, repeated, gaps = max(max_error, abs(error)), repeated + duplicate, gaps + gap
                writer.writerow([i, i / fps, start, stop, chosen, *exposures[chosen], error, duplicate, gap])
                previous = chosen
                if i % max(1, count // 100) == 0:
                    notify(20 + 70 * (i + 1) / count, f"编码 {i + 1}/{count} 帧")
        finally:
            try:
                process.stdin.close()
            finally:
                code = process.wait()
        if code:
            raise RuntimeError("视频编码失败，请查看联合编码日志.txt")
    Image.fromarray(best).save(output / "05 联合画面预览.png")
    return {"fps": fps, "output_frames": count, "duration_seconds": count / fps,
            "common_start_us": begin, "common_end_us": end, "max_gray_midpoint_error_us": max_error,
            "repeated_gray_frames": repeated, "large_gap_frames": gaps,
            "method": "三路视频来自同一联合画面时间轴，事件按固定窗口累积、灰度匹配最近曝光中点；非曝光区间严格配对。正事件绿、负事件红，同像素保留窗内末次极性，无额外滤波。"}


def convert(source, output, fps, progress=None, outputs=("joint", "events", "gray")):
    started = time.perf_counter()
    if not 1 <= fps <= 500:
        raise ValueError("FPS必须在1到500之间")
    outputs = tuple(dict.fromkeys(outputs))
    if not outputs or any(kind not in ("joint", "events", "gray") for kind in outputs):
        raise ValueError("请至少选择一种有效的视频类型")
    source = source.resolve(strict=True)
    output = output.resolve()
    if output.exists():
        raise FileExistsError("输出目录已存在，请使用新目录，避免覆盖")
    notify = progress or (lambda percent, message: None)
    notify(0, "校验原始文件，请勿继续写入此录制")
    before = digest(source)
    original_stat = source.stat()
    decoder = aedat.Decoder(str(source))
    streams = decoder.id_to_stream()
    if sum(s["type"] == "events" for s in streams.values()) != 1 or sum(s["type"] == "frame" for s in streams.values()) != 1:
        raise ValueError("必须具有唯一事件流和灰度流")
    frames, midpoints = [], []
    event_count, dtype, shape = 0, None, None
    first_t, last_t, max_x, max_y = None, None, -1, -1
    maps = []
    notify(5, "解析录制并建立磁盘缓存，进度取决于文件大小")
    with tempfile.TemporaryDirectory(prefix="aedat4-export-") as cache:
        cache = Path(cache)
        try:
            with (cache / "events.bin").open("wb") as event_file, (cache / "timestamps.bin").open("wb") as time_file, (cache / "frames.bin").open("wb") as frame_file:
                for packet in decoder:
                    if "events" in packet and len(packet["events"]):
                        part = packet["events"]
                        if dtype is not None and part.dtype != dtype:
                            raise ValueError("事件数据格式不一致")
                        dtype = part.dtype
                        times = part["t"].astype(np.int64)
                        if np.any(np.diff(times) < 0) or (last_t is not None and int(times[0]) < last_t):
                            raise ValueError("事件时间戳非单调")
                        if np.any(part["x"] < 0) or np.any(part["y"] < 0):
                            raise ValueError("事件坐标超范围")
                        first_t = int(times[0]) if first_t is None else first_t
                        last_t = int(times[-1])
                        max_x, max_y = max(max_x, int(part["x"].max())), max(max_y, int(part["y"].max()))
                        part.tofile(event_file)
                        times.tofile(time_file)
                        event_count += len(part)
                    if "frame" in packet:
                        f = packet["frame"]
                        pixels = f["pixels"]
                        if f["format"] != "L" or pixels.dtype != np.uint8 or pixels.ndim != 2:
                            raise ValueError("只接受8位灰度APS帧")
                        if f.get("offset_x", 0) or f.get("offset_y", 0):
                            raise ValueError("暂不支持带偏移的裁剪灰度帧")
                        shape = pixels.shape if shape is None else shape
                        if pixels.shape != shape or any(d % 2 for d in shape):
                            raise ValueError("分辨率不一致或非偶数")
                        exposure = (int(f["exposure_begin_t"]), int(f["exposure_end_t"]))
                        midpoint = sum(exposure) // 2
                        if exposure[1] < exposure[0] or (midpoints and midpoint <= midpoints[-1]):
                            raise ValueError("灰度曝光时间或时间顺序异常")
                        frames.append(exposure)
                        midpoints.append(midpoint)
                        pixels.tofile(frame_file)
            del decoder
            if not event_count or not frames:
                raise ValueError("事件或灰度实际数据为空，未生成视频")
            height, width = shape
            if max_x >= width or max_y >= height:
                raise ValueError("事件坐标超出灰度画面范围")
            begin, end = max(first_t, midpoints[0]), min(last_t + 1, midpoints[-1])
            count = (end - begin) * fps // 1_000_000
            if count < 1:
                raise ValueError("两路没有足够的共同时间范围")
            current_stat = source.stat()
            if (current_stat.st_size, current_stat.st_mtime_ns) != (original_stat.st_size, original_stat.st_mtime_ns):
                raise RuntimeError("源文件仍在变化，请停止录制后再导出")
            events = np.memmap(cache / "events.bin", dtype=dtype, mode="r", shape=(event_count,))
            maps.append(events)
            timestamps = np.memmap(cache / "timestamps.bin", dtype=np.int64, mode="r", shape=(event_count,))
            maps.append(timestamps)
            gray_frames = np.memmap(cache / "frames.bin", dtype=np.uint8, mode="r", shape=(len(frames), height, width))
            maps.append(gray_frames)
            midpoints = np.asarray(midpoints, dtype=np.int64)
            output.mkdir(parents=True, exist_ok=False)
            report = render_videos(events, timestamps, gray_frames, frames, midpoints, begin, end, count, fps, output, notify, outputs)
            report["output_types"] = list(outputs)
            notify(97, "编码结束，核验源文件未被修改")
            if digest(source) != before:
                raise RuntimeError("源文件哈希变化，本次结果未通过核验")
            report.update(source=str(source), source_sha256=before, source_unchanged=True,
                          source_events=event_count, source_gray_frames=len(frames),
                          seconds_elapsed=time.perf_counter() - started)
            (output / "06 转换结果.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            notify(100, "完成：所选MP4、时间对应表及报告已保存")
            return report
        finally:
            for mapped in maps:
                mapped._mmap.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AEDAT4共同时间轴所选MP4导出，磁盘缓存处理")
    parser.add_argument("input", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--types", nargs="+", choices=("joint", "events", "gray"), default=["joint", "events", "gray"])
    args = parser.parse_args()
    print(json.dumps(convert(args.input, args.output_dir, args.fps, outputs=args.types), ensure_ascii=False, indent=2))
