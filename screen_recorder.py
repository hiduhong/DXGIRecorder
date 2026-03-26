"""
Mac Screen Recorder
===================
仿 OBS 显示器采集模式，基于 macOS CGDisplayStream / CoreGraphics API。
支持多显示器、指定区域、实时预览、MP4 输出。

后端层级（从深到浅）:
  1. CGDisplayStream (深层后端)  ← 本脚本优先使用（等价 Windows DXGI Duplication）
       GPU 帧缓冲 → IOSurface → ctypes Lock → numpy
       推流模式，即使在终端也能稳定工作，不会像 SCKit 那样崩溃 (133)。
  2. mss / CoreGraphics          ← 回退（截图模式，每帧单独请求）
       CGWindowListCreateImage → bitmap → numpy

平台: macOS 10.15+
"""

import sys
import time
import signal
import argparse
import threading
from pathlib import Path
from datetime import datetime

import numpy as np
import cv2

# ──────────────────────────────────────────────
# 平台检查
# ──────────────────────────────────────────────
if sys.platform != "darwin":
    print("[ERROR] 此脚本仅支持 macOS。Windows 请使用 DXGI Desktop Duplication。")
    sys.exit(1)

# ──────────────────────────────────────────────
# 采集后端选择（优先 CGDisplayStream）
# ──────────────────────────────────────────────
import platform

def _macos_version() -> tuple:
    v = platform.mac_ver()[0].split(".")
    try:
        return tuple(int(x) for x in v[:2])
    except:
        return (0, 0)

BACKEND = None

# ✅ 尝试 CGDisplayStream (深层后端, macOS 所有版本, GPU帧缓冲 推流, 终端兼容)
try:
    import objc          # noqa: F401
    import Quartz        # noqa: F401
    import CoreFoundation  # noqa: F401
    from cgds_backend import CGDisplayStreamCapture, CGDSUnavailableError
    # CGDisplayStream 在 10.15+ 非常稳定
    if _macos_version() >= (10, 15):
        BACKEND = "cgdisplaystream"
        print("[INFO] 使用 CGDisplayStream 后端 (GPU帧缓冲推流, 等价 DXGI Duplication)")
except ImportError:
    pass

# 回退到 mss (基于 CoreGraphics CGWindowListCreateImage)
if BACKEND is None:
    try:
        import mss
        BACKEND = "mss"
        print("[INFO] 使用 mss/CoreGraphics 后端")
    except ImportError:
        print("[ERROR] 请安装依赖: pip install -r requirements.txt")
        sys.exit(1)


# ──────────────────────────────────────────────
# 显示器信息
# ──────────────────────────────────────────────

def list_displays() -> list[dict]:
    """列出所有显示器信息"""
    try:
        import mss as mss_lib
        with mss_lib.mss() as sct:
            displays = []
            for i, m in enumerate(sct.monitors[1:]):
                displays.append({
                    "index": i,
                    "width": m["width"],
                    "height": m["height"],
                    "left": m["left"],
                    "top": m["top"],
                })
            return displays
    except Exception:
        return [{"index": 0, "width": 1920, "height": 1080, "left": 0, "top": 0}]


# ──────────────────────────────────────────────
# 采集器类
# ──────────────────────────────────────────────

class MacScreenRecorder:
    def __init__(
        self,
        output_path: str = None,
        fps: int = 30,
        monitor_idx: int = 0,
        region: tuple = None,
        codec: str = "avc1",
        show_cursor: bool = True,
    ):
        self.fps = fps
        self.monitor_idx = monitor_idx
        self.region = region
        self.codec = codec
        self.show_cursor = show_cursor
        self.output_path = output_path or self._default_output_path()

        self._recording = False
        self._frame_count = 0
        self._start_time = None
        self._writer: cv2.VideoWriter = None
        self._thread: threading.Thread = None
        self._lock = threading.Lock()
        self._preview_frame: np.ndarray = None

    @staticmethod
    def _default_output_path() -> str:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        return str(Path.cwd() / f"recording_{ts}.mp4")

    def _get_monitor(self) -> dict:
        displays = list_displays()
        if self.monitor_idx >= len(displays):
            print(f"[WARN] 显示器 {self.monitor_idx} 不存在，使用主显示器")
            return displays[0]
        return displays[self.monitor_idx]

    def _init_writer(self, w: int, h: int):
        fourcc = cv2.VideoWriter_fourcc(*self.codec)
        self._writer = cv2.VideoWriter(self.output_path, fourcc, self.fps, (w, h))
        if not self._writer.isOpened():
            print(f"[WARN] 编码器 {self.codec} 不可用，回退到 mp4v")
            fourcc = cv2.VideoWriter_fourcc(*"mp4v")
            self._writer = cv2.VideoWriter(self.output_path, fourcc, self.fps, (w, h))
        if not self._writer.isOpened():
            raise RuntimeError(f"无法创建视频文件: {self.output_path}")

    # ── 录制循环 (mss 回退) ──
    def _record_loop_mss(self):
        import mss as mss_lib
        monitor = self._get_monitor()
        # 处理全屏或区域
        if self.region:
            l, t, w, h = self.region
            cap_region = {"left": monitor["left"] + l, "top": monitor["top"] + t, "width": w, "height": h}
        else:
            cap_region = {"left": monitor["left"], "top": monitor["top"], "width": monitor["width"], "height": monitor["height"]}
        
        w, h = cap_region["width"], cap_region["height"]
        self._init_writer(w, h)
        interval = 1.0 / self.fps
        self._start_time = time.perf_counter()

        with mss_lib.mss() as sct:
            while self._recording:
                t0 = time.perf_counter()
                img = sct.grab(cap_region)
                frame_bgr = cv2.cvtColor(np.array(img), cv2.COLOR_BGRA2BGR)
                self._preview_frame = frame_bgr
                with self._lock:
                    self._writer.write(frame_bgr)
                    self._frame_count += 1
                
                elapsed = time.perf_counter() - t0
                sleep_time = interval - elapsed
                if sleep_time > 0:
                    time.sleep(sleep_time)

    # ── 录制循环 (CGDisplayStream 核心后端) ──
    def _record_loop_cgds(self):
        import CoreFoundation
        from cgds_backend import CGDisplayStreamCapture

        monitor = self._get_monitor()
        w, h = monitor["width"], monitor["height"]

        crop = None
        if self.region:
            l, t, cw, ch = self.region
            crop = (t, t + ch, l, l + cw)
            w, h = cw, ch

        self._init_writer(w, h)
        self._start_time = time.perf_counter()

        def _on_frame(frame_bgr):
            if not self._recording: return
            if crop:
                y0, y1, x0, x1 = crop
                frame_bgr = frame_bgr[y0:y1, x0:x1]
            self._preview_frame = frame_bgr
            with self._lock:
                self._writer.write(frame_bgr)
                self._frame_count += 1

        try:
            cap = CGDisplayStreamCapture(display_idx=self.monitor_idx, fps=self.fps, show_cursor=self.show_cursor)
            cap.start(_on_frame)
            # 在单独线程自旋 RunLoop
            while self._recording:
                CoreFoundation.CFRunLoopRunInMode(CoreFoundation.kCFRunLoopDefaultMode, 0.016, True)
            cap.stop()
        except Exception as e:
            print(f"[WARN] CGDisplayStream 失败: {e}")
            self._record_loop_mss()

    def start(self):
        if self._recording: return
        self._recording = True
        self._frame_count = 0
        target = self._record_loop_cgds if BACKEND == "cgdisplaystream" else self._record_loop_mss
        self._thread = threading.Thread(target=target, daemon=True)
        self._thread.start()

    def stop(self) -> dict:
        if not self._recording: return {}
        self._recording = False
        if self._thread: self._thread.join(timeout=5)
        with self._lock:
            if self._writer: self._writer.release()
        
        duration = time.perf_counter() - self._start_time if self._start_time else 0
        actual_fps = self._frame_count / duration if duration > 0 else 0
        stats = {"frames": self._frame_count, "duration_s": round(duration, 2), "actual_fps": round(actual_fps, 1), "output": self.output_path}
        print(f"\n[INFO] 录制完成! 时长: {stats['duration_s']}s, 帧率: {stats['actual_fps']}")
        return stats

    def is_recording(self) -> bool:
        return self._recording

    def get_latest_frame(self) -> np.ndarray | None:
        return self._preview_frame

# 预览函数
def run_preview(recorder: MacScreenRecorder, scale: float = 0.5):
    win_name = "Preview (press Q to stop)"
    while recorder.is_recording():
        frame = recorder.get_latest_frame()
        if frame is None:
            time.sleep(0.01)
            continue
        h, w = frame.shape[:2]
        preview = cv2.resize(frame, (int(w * scale), int(h * scale)))
        cv2.imshow(win_name, preview)
        if cv2.waitKey(1) & 0xFF == ord('q'):
            recorder.stop()
            break
    cv2.destroyAllWindows()

def main():
    parser = argparse.ArgumentParser(description="macOS Screen Recorder")
    parser.add_argument("--preview", "-p", action="store_true", help="显示预览")
    parser.add_argument("--duration", "-d", type=float, help="录制时长")
    parser.add_argument("--monitor", "-m", type=int, default=0, help="显示器索引")
    parser.add_argument("--fps", "-f", type=int, default=30, help="帧率")
    args = parser.parse_args()

    recorder = MacScreenRecorder(monitor_idx=args.monitor, fps=args.fps)
    recorder.start()
    
    if args.preview:
        run_preview(recorder)
    elif args.duration:
        time.sleep(args.duration)
        recorder.stop()
    else:
        print("按 Ctrl+C 停止录制...")
        try:
            while recorder.is_recording(): time.sleep(0.5)
        except KeyboardInterrupt:
            recorder.stop()

if __name__ == "__main__":
    main()
