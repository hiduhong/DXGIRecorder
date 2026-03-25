"""
Mac Screen Recorder
===================
仿 OBS 显示器采集模式，基于 macOS ScreenCaptureKit / CoreGraphics API。
支持多显示器、指定区域、实时预览、MP4 输出。

后端层级（从深到浅）:
  1. ScreenCaptureKit SCStream   ← 本脚本优先使用（等价 Windows DXGI Duplication）
       GPU → IOSurface → CMSampleBuffer → CVPixelBuffer → numpy
       推流模式，系统级采集，可绕过窗口保护
  2. mss / CoreGraphics          ← 回退（截图模式，每帧单独请求）
       CGWindowListCreateImage → bitmap → numpy

平台: macOS 12.3+ (ScreenCaptureKit) / macOS 10.15+ (CoreGraphics fallback)
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
# 采集后端选择（优先 ScreenCaptureKit，其次 mss/CoreGraphics）
# ──────────────────────────────────────────────
import subprocess, platform

def _macos_version() -> tuple:
    v = platform.mac_ver()[0].split(".")
    return tuple(int(x) for x in v[:2])

BACKEND = None

# 尝试 ScreenCaptureKit (深层后端, macOS 12.3+, 等价 Windows DXGI Duplication)
if _macos_version() >= (12, 3):
    try:
        import objc          # noqa: F401
        import Quartz        # noqa: F401
        import CoreMedia     # noqa: F401
        import ScreenCaptureKit  # noqa: F401
        BACKEND = "screencapturekit"
        print("[INFO] 使用 ScreenCaptureKit 后端 (GPU推流，等价 DXGI Duplication)")
    except ImportError:
        pass

# 回退到 mss (基于 CoreGraphics CGWindowListCreateImage，与 OBS CoreGraphics 源相同)
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
            # monitors[0] 是全部显示器虚拟拼接，从 1 开始是真实显示器
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
    """
    macOS 屏幕录制器，仿 OBS 显示器采集模式。

    参数:
        output_path  : 输出 MP4 文件路径（None = 自动命名）
        fps          : 目标帧率（建议 30 或 60）
        monitor_idx  : 显示器索引，0 = 主显示器
        region       : (left, top, width, height) 采集区域，None = 全屏
        codec        : 视频编码器（'avc1'=H264, 'mp4v'=MPEG4）
        show_cursor  : 是否采集鼠标光标（类 OBS 选项）
    """

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
        self.region = region  # (left, top, width, height)
        self.codec = codec
        self.show_cursor = show_cursor
        self.output_path = output_path or self._default_output_path()

        self._recording = False
        self._frame_count = 0
        self._start_time = None
        self._writer: cv2.VideoWriter = None
        self._thread: threading.Thread = None
        self._lock = threading.Lock()
        self._preview_frame: np.ndarray = None  # 最新帧，用于预览共享

    # ── 工具方法 ──────────────────────────────

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

    def _build_capture_region(self, monitor: dict) -> dict:
        """将用户参数转换为 mss 格式的采集区域"""
        if self.region:
            l, t, w, h = self.region
            return {
                "left":   monitor["left"] + l,
                "top":    monitor["top"] + t,
                "width":  w,
                "height": h,
            }
        return {
            "left":   monitor["left"],
            "top":    monitor["top"],
            "width":  monitor["width"],
            "height": monitor["height"],
        }

    def _init_writer(self, w: int, h: int):
        fourcc = cv2.VideoWriter_fourcc(*self.codec)
        self._writer = cv2.VideoWriter(self.output_path, fourcc, self.fps, (w, h))
        if not self._writer.isOpened():
            # avc1 可能不可用，回退到 mp4v
            print(f"[WARN] 编码器 {self.codec} 不可用，回退到 mp4v")
            fourcc = cv2.VideoWriter_fourcc(*"mp4v")
            self._writer = cv2.VideoWriter(self.output_path, fourcc, self.fps, (w, h))
        if not self._writer.isOpened():
            raise RuntimeError(f"无法创建视频文件: {self.output_path}")

    # ── 录制循环 (mss/CoreGraphics 后端) ──────

    def _record_loop_mss(self):
        import mss as mss_lib

        monitor = self._get_monitor()
        cap_region = self._build_capture_region(monitor)
        w, h = cap_region["width"], cap_region["height"]
        self._init_writer(w, h)
        interval = 1.0 / self.fps

        print(f"[INFO] 录制中...  显示器={self.monitor_idx}  区域={w}x{h}  FPS={self.fps}")
        print(f"       输出文件: {self.output_path}")

        self._start_time = time.perf_counter()

        with mss_lib.mss() as sct:
            while self._recording:
                t0 = time.perf_counter()

                # 抓取屏幕（等同 OBS CoreGraphics 显示采集）
                img = sct.grab(cap_region)
                frame = np.array(img)
                frame_bgr = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)

                # 共享给预览
                self._preview_frame = frame_bgr

                with self._lock:
                    self._writer.write(frame_bgr)
                    self._frame_count += 1

                elapsed = time.perf_counter() - t0
                sleep_time = interval - elapsed
                if sleep_time > 0:
                    time.sleep(sleep_time)

    # ── 录制循环 (ScreenCaptureKit 深层后端) ────
    # 使用 sck_backend.SCKCapture 实现 GPU→IOSurface→CMSampleBuffer 推流。
    # 等价于 Windows DXGI Desktop Duplication 的 AcquireNextFrame 循环。

    def _record_loop_sck(self):
        """
        ScreenCaptureKit 推流录制。

        数据路径:
          SCStream 帧回调
          → CMSampleBuffer → CVPixelBuffer (IOSurface)
          → 锁定 CPU 读取 → numpy BGRA → cv2 BGR
          → VideoWriter
        """
        from sck_backend import SCKCapture, SCKUnavailableError

        monitor   = self._get_monitor()
        w, h      = monitor["width"], monitor["height"]
        interval  = 1.0 / self.fps

        # 如果用户指定了区域，SCK 捕整屏后在 numpy 层裁剪
        crop = None
        if self.region:
            l, t, cw, ch = self.region
            crop = (t, t + ch, l, l + cw)   # (y0, y1, x0, x1)
            w, h = cw, ch

        self._init_writer(w, h)
        self._start_time = time.perf_counter()

        print(f"[SCK] 深层 ScreenCaptureKit 推流启动")
        print(f"      显示器={self.monitor_idx}  分辨率={w}x{h}  目标FPS={self.fps}")
        print(f"      数据路径: GPU→IOSurface→CMSampleBuffer→CVPixelBuffer→numpy")
        print(f"      输出文件: {self.output_path}")

        captured_frames = [0]   # 用列表以便闭包修改

        def _on_frame(frame_bgr: np.ndarray):
            """SCStream 帧回调（在主队列调度）"""
            if not self._recording:
                return

            # 裁剪到用户指定区域
            if crop:
                y0, y1, x0, x1 = crop
                frame_bgr = frame_bgr[y0:y1, x0:x1]

            # 共享最新帧给预览
            self._preview_frame = frame_bgr

            with self._lock:
                self._writer.write(frame_bgr)
                self._frame_count += 1

        try:
            cap = SCKCapture(
                monitor_idx=self.monitor_idx,
                fps=self.fps,
                show_cursor=self.show_cursor,
            )
            cap.start(_on_frame)

            # 主循环：只负责等待停止信号（帧处理全在回调里）
            # 等价于 DXGI 的 while(running) { AcquireNextFrame(); ... ReleaseFrame(); }
            while self._recording:
                time.sleep(interval)

        except SCKUnavailableError as e:
            print(f"[WARN] SCK 启动失败，回退到 mss: {e}")
            self._record_loop_mss()
            return
        finally:
            try:
                cap.stop()
            except Exception:
                pass

    # ── 公开接口 ──────────────────────────────

    def start(self):
        """开始录制 (后台线程，不阻塞)"""
        if self._recording:
            print("[WARN] 录制已在进行中")
            return

        # macOS 屏幕录制权限检查
        self._check_permission()

        self._recording = True
        self._frame_count = 0

        target = self._record_loop_sck if BACKEND == "screencapturekit" else self._record_loop_mss
        self._thread = threading.Thread(target=target, daemon=True, name="ScreenCapture")
        self._thread.start()

    def stop(self) -> dict:
        """停止录制，返回录制统计信息"""
        if not self._recording:
            print("[WARN] 录制未启动")
            return {}

        self._recording = False
        if self._thread:
            self._thread.join(timeout=5)

        with self._lock:
            if self._writer and self._writer.isOpened():
                self._writer.release()

        duration = time.perf_counter() - self._start_time if self._start_time else 0
        actual_fps = self._frame_count / duration if duration > 0 else 0

        stats = {
            "frames":     self._frame_count,
            "duration_s": round(duration, 2),
            "actual_fps": round(actual_fps, 1),
            "output":     self.output_path,
        }
        print(f"\n[INFO] 录制完成!")
        print(f"       帧数: {stats['frames']}")
        print(f"       时长: {stats['duration_s']}s")
        print(f"       实际帧率: {stats['actual_fps']} fps")
        print(f"       文件: {stats['output']}")
        return stats

    def is_recording(self) -> bool:
        return self._recording

    @staticmethod
    def _check_permission():
        """检查 macOS 屏幕录制权限"""
        try:
            import Quartz
            # 尝试获取窗口列表，无权限时会失败
            Quartz.CGWindowListCopyWindowInfo(
                Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID
            )
        except Exception:
            # 无 pyobjc 时跳过检查
            pass

    def get_latest_frame(self) -> np.ndarray | None:
        """获取最新一帧（用于预览）"""
        return self._preview_frame


# ──────────────────────────────────────────────
# 实时预览窗口（仿 OBS 预览）
# ──────────────────────────────────────────────

def run_preview(recorder: MacScreenRecorder, scale: float = 0.5):
    """
    在录制同时显示实时预览（类 OBS 预览窗格）
    按 'q' 停止录制并退出
    """
    win_name = "Mac Screen Recorder - Preview (press Q to stop)"
    print(f"[INFO] 预览已开启 (缩放比例 {scale*100:.0f}%)，按 'q' 停止")

    while recorder.is_recording():
        frame = recorder.get_latest_frame()
        if frame is None:
            time.sleep(0.01)
            continue

        # 缩放预览
        h, w = frame.shape[:2]
        preview = cv2.resize(frame, (int(w * scale), int(h * scale)),
                             interpolation=cv2.INTER_LINEAR)

        # 叠加录制状态 HUD
        elapsed = time.perf_counter() - (recorder._start_time or time.perf_counter())
        hud = f"REC  {int(elapsed//60):02d}:{int(elapsed%60):02d}  {recorder._frame_count} frames"
        cv2.rectangle(preview, (0, 0), (len(hud) * 10 + 16, 28), (20, 20, 20), -1)
        cv2.putText(preview, hud, (8, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 80, 255), 2)
        cv2.putText(preview, hud, (8, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)

        cv2.imshow(win_name, preview)
        if cv2.waitKey(1) & 0xFF in (ord("q"), ord("Q"), 27):
            recorder.stop()
            break

    cv2.destroyAllWindows()


# ──────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(
        description="macOS 屏幕录制 (仿 OBS 显示器采集模式)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 列出所有显示器
  python screen_recorder.py --list-displays

  # 录制主显示器 30 秒
  python screen_recorder.py --duration 30

  # 录制并实时预览
  python screen_recorder.py --preview

  # 录制第二台显示器，60fps，指定区域
  python screen_recorder.py --monitor 1 --fps 60 --region 0 0 1280 720
        """
    )
    parser.add_argument("--list-displays", action="store_true", help="列出所有显示器信息")
    parser.add_argument("--output",   "-o", type=str,   default=None,            help="输出文件路径")
    parser.add_argument("--fps",      "-f", type=int,   default=30,              help="目标帧率 (默认 30)")
    parser.add_argument("--monitor",  "-m", type=int,   default=0,               help="显示器索引 (默认 0 = 主显示器)")
    parser.add_argument("--region",   "-r", type=int,   nargs=4,
                        metavar=("L", "T", "W", "H"),                            help="采集区域: left top width height")
    parser.add_argument("--codec",          type=str,   default="avc1",          help="编码器 (avc1=H264, mp4v=MPEG4)")
    parser.add_argument("--duration", "-d", type=float, default=None,            help="录制时长(秒)，不填则手动 Ctrl+C 停止")
    parser.add_argument("--preview",  "-p", action="store_true",                 help="显示实时预览窗口")
    parser.add_argument("--scale",          type=float, default=0.5,             help="预览缩放比例 (默认 0.5)")
    return parser.parse_args()


def main():
    args = parse_args()

    if args.list_displays:
        displays = list_displays()
        print(f"\n找到 {len(displays)} 台显示器:")
        for d in displays:
            print(f"  [{d['index']}] {d['width']}x{d['height']}  偏移=({d['left']},{d['top']})")
        return

    recorder = MacScreenRecorder(
        output_path=args.output,
        fps=args.fps,
        monitor_idx=args.monitor,
        region=tuple(args.region) if args.region else None,
        codec=args.codec,
    )

    # 支持 Ctrl+C 优雅停止
    def _sigint_handler(sig, frame):
        print("\n[INFO] Ctrl+C 收到，正在停止...")
        recorder.stop()
        sys.exit(0)
    signal.signal(signal.SIGINT, _sigint_handler)

    recorder.start()

    if args.preview:
        run_preview(recorder, scale=args.scale)
    elif args.duration:
        print(f"[INFO] 录制 {args.duration} 秒后停止...")
        time.sleep(args.duration)
        recorder.stop()
    else:
        print("[INFO] 按 Ctrl+C 停止录制...")
        while recorder.is_recording():
            time.sleep(0.5)


if __name__ == "__main__":
    main()
