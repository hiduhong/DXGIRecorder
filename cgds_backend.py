"""
cgds_backend.py — CGDisplayStream 深层后端
==========================================
比 mss (CGWindowListCreateImage 截图) 更底层的 CoreGraphics 层采集。

层级对比:
  mss (当前):         CGWindowListCreateImage()  → bitmap → numpy
  本文件 (更深):      CGDisplayStreamCreate()    → IOSurface 推流 → numpy
  SCKit (最深/需app): SCStream + XPC            → CMSampleBuffer → numpy

数据路径 (本文件):
  CGDisplayStream 帧回调 (RunLoop 驱动)
  → IOSurface (GPU帧缓冲直接引用)
  → IOSurfaceLock (ctypes 调用，等价 DXGI Map)
  → BGRA 原始字节 → numpy BGR
  → IOSurfaceUnlock (等价 DXGI Unmap)

优势 vs mss:
  ✅ 推流模式，GPU直接推送，不是每帧独立截图
  ✅ 支持高刷新率（可达 120fps）
  ✅ 延迟更低
  ✅ 不需要 app bundle，命令行可用

依赖: pyobjc-framework-Quartz (已包含 CGDisplayStream)
     IOSurface.framework (macOS 内置，通过 ctypes 调用)
平台: macOS 10.15+
"""
from __future__ import annotations

import sys
import time
import ctypes
import ctypes.util
import threading
import queue
from typing import Callable, Optional

import numpy as np


# ────────────────────────────────────────────────────────
# 依赖检查
# ────────────────────────────────────────────────────────

class CGDSUnavailableError(RuntimeError):
    pass


def _check_deps() -> bool:
    try:
        import Quartz         # noqa
        import CoreFoundation # noqa
        return True
    except ImportError:
        return False


PYOBJC_AVAILABLE = _check_deps()


# ────────────────────────────────────────────────────────
# IOSurface ctypes 绑定
# (pyobjc-Quartz 不暴露 IOSurface lock/unlock/getBaseAddress)
# ────────────────────────────────────────────────────────

class _IOSurface:
    """IOSurface.framework ctypes 直接绑定"""
    _lib = None

    @classmethod
    def _load(cls):
        if cls._lib is not None:
            return cls._lib
        lib = ctypes.CDLL(
            "/System/Library/Frameworks/IOSurface.framework/Versions/A/IOSurface"
        )
        lib.IOSurfaceLock.restype   = ctypes.c_int
        lib.IOSurfaceLock.argtypes  = [ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)]
        lib.IOSurfaceUnlock.restype  = ctypes.c_int
        lib.IOSurfaceUnlock.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)]
        lib.IOSurfaceGetBaseAddress.restype  = ctypes.c_void_p
        lib.IOSurfaceGetBaseAddress.argtypes = [ctypes.c_void_p]
        lib.IOSurfaceGetWidth.restype   = ctypes.c_size_t
        lib.IOSurfaceGetWidth.argtypes  = [ctypes.c_void_p]
        lib.IOSurfaceGetHeight.restype  = ctypes.c_size_t
        lib.IOSurfaceGetHeight.argtypes = [ctypes.c_void_p]
        lib.IOSurfaceGetBytesPerRow.restype  = ctypes.c_size_t
        lib.IOSurfaceGetBytesPerRow.argtypes = [ctypes.c_void_p]
        cls._lib = lib
        return lib

    @classmethod
    def lock(cls, surface_ptr: int) -> bool:
        lib = cls._load()
        kIOSurfaceLockReadOnly = 1
        return lib.IOSurfaceLock(surface_ptr, kIOSurfaceLockReadOnly, None) == 0

    @classmethod
    def unlock(cls, surface_ptr: int):
        lib = cls._load()
        kIOSurfaceLockReadOnly = 1
        lib.IOSurfaceUnlock(surface_ptr, kIOSurfaceLockReadOnly, None)

    @classmethod
    def get_info(cls, surface_ptr: int) -> tuple:
        """返回 (width, height, bpr, base_address)"""
        lib = cls._load()
        w   = lib.IOSurfaceGetWidth(surface_ptr)
        h   = lib.IOSurfaceGetHeight(surface_ptr)
        bpr = lib.IOSurfaceGetBytesPerRow(surface_ptr)
        base = lib.IOSurfaceGetBaseAddress(surface_ptr)
        return w, h, bpr, base


# ────────────────────────────────────────────────────────
# IOSurface → numpy BGR
# ────────────────────────────────────────────────────────

def _iosurface_to_numpy(surface_obj) -> Optional[np.ndarray]:
    """
    IOSurface (CGDisplayStream 帧回调参数) → numpy BGR

    数据路径:
      IOSurface (GPU帧缓冲直接引用)
      → surface.__c_void_p__()    (pyobjc → C 指针)
      → IOSurfaceLock              (锁定 CPU 访问，等价 DXGI Map)
      → IOSurfaceGetBaseAddress    (原始 BGRA 字节指针)
      → numpy BGRA → cv2 BGR
      → IOSurfaceUnlock            (解锁，等价 DXGI Unmap)
    """
    import cv2

    # 用 pyobjc 提供的 __c_void_p__() 获取 ObjC 对象的 C 指针
    try:
        surface_ptr = surface_obj.__c_void_p__()
    except (AttributeError, TypeError):
        return None

    if not surface_ptr:
        return None

    if not _IOSurface.lock(surface_ptr):
        return None

    try:
        w, h, bpr, base = _IOSurface.get_info(surface_ptr)
        if not base or w == 0 or h == 0:
            return None

        # 读取 BGRA 字节（GPU帧缓冲原始数据）
        buf = (ctypes.c_uint8 * (bpr * h)).from_address(base)
        arr = np.frombuffer(buf, dtype=np.uint8).reshape((h, bpr // 4, 4))
        frame_bgra = arr[:, :w, :].copy()   # copy 脱离指针生命周期
        return cv2.cvtColor(frame_bgra, cv2.COLOR_BGRA2BGR)
    finally:
        _IOSurface.unlock(surface_ptr)


# ────────────────────────────────────────────────────────
# 采集器
# ────────────────────────────────────────────────────────

class CGDisplayStreamCapture:
    """
    CGDisplayStream 深层采集器。

    特点:
    - 推流模式 (vs mss 截图模式)
    - 直接对接 GPU 帧缓冲
    - 不需要 app bundle，命令行即可
    - 支持 60/120fps

    ⚠️ start() 后必须在同一线程自旋 RunLoop 才能收到帧。
    """

    def __init__(
        self,
        display_idx: int = 0,
        fps: int = 60,
        show_cursor: bool = True,
    ):
        if not PYOBJC_AVAILABLE:
            raise CGDSUnavailableError(
                "请安装: pip install pyobjc-framework-Quartz"
            )
        self.display_idx = display_idx
        self.fps         = fps
        self.show_cursor = show_cursor
        self._stream     = None
        self._rl_source  = None
        self._running    = False

    def _get_display_id(self) -> int:
        """获取指定索引的 CGDirectDisplayID"""
        import Quartz
        # CGGetActiveDisplayList 返回 (err, ids_tuple, count)
        err, ids, count = Quartz.CGGetActiveDisplayList(16, None, None)
        if err != 0:
            raise CGDSUnavailableError(f"CGGetActiveDisplayList 失败: {err}")
        if self.display_idx >= count:
            raise ValueError(f"显示器索引 {self.display_idx} 越界，共 {count} 台")
        return ids[self.display_idx]

    def start(self, callback: Callable[[np.ndarray], None]) -> None:
        """
        启动 CGDisplayStream 推流。
        ⚠️ 调用后必须在当前线程自旋 RunLoop，帧回调才会触发。
        """
        import Quartz, CoreFoundation

        display_id = self._get_display_id()
        bounds = Quartz.CGDisplayBounds(display_id)
        w = int(bounds.size.width)
        h = int(bounds.size.height)

        props = {
            Quartz.kCGDisplayStreamPreserveAspectRatio: False,
            Quartz.kCGDisplayStreamShowCursor:          self.show_cursor,
            Quartz.kCGDisplayStreamMinimumFrameTime:    1.0 / self.fps,
            Quartz.kCGDisplayStreamQueueDepth:          3,
        }

        frame_status_complete = Quartz.kCGDisplayStreamFrameStatusFrameComplete

        def _handler(status, display_time, surface, update_ref):
            """CGDisplayStream 帧回调"""
            if status == frame_status_complete and surface is not None:
                frame = _iosurface_to_numpy(surface)
                if frame is not None:
                    callback(frame)

        # 创建 CGDisplayStream（RunLoop 版本）
        stream = Quartz.CGDisplayStreamCreate(
            display_id,
            w, h,
            int(Quartz.kCVPixelFormatType_32BGRA),
            props,
            _handler,
        )

        if stream is None:
            raise CGDSUnavailableError(
                "CGDisplayStreamCreate 失败。\n"
                "请检查: 系统设置 → 隐私与安全性 → 屏幕录制 → 授权 Terminal/Python"
            )

        # 获取 RunLoop source 并注入当前线程的 RunLoop
        rl_source = Quartz.CGDisplayStreamGetRunLoopSource(stream)
        if rl_source is None:
            raise CGDSUnavailableError("无法获取 CGDisplayStream RunLoop source")

        rl = CoreFoundation.CFRunLoopGetCurrent()
        CoreFoundation.CFRunLoopAddSource(
            rl, rl_source, CoreFoundation.kCFRunLoopDefaultMode
        )

        # 启动流
        err = Quartz.CGDisplayStreamStart(stream)
        if err != 0:
            raise CGDSUnavailableError(f"CGDisplayStreamStart 失败，错误码: {err}")

        self._stream    = stream
        self._rl_source = rl_source
        self._rl        = rl
        self._running   = True
        self._w         = w
        self._h         = h

        print(f"[CGDS] ✅ 推流启动  显示器=[{self.display_idx}]  {w}x{h}  {self.fps}fps")
        print(f"[CGDS]    数据路径: GPU帧缓冲 → IOSurface (ctypes Lock) → numpy BGR")

    def stop(self) -> None:
        """停止 CGDisplayStream"""
        if not self._running or self._stream is None:
            return

        import Quartz, CoreFoundation
        Quartz.CGDisplayStreamStop(self._stream)

        if self._rl_source and self._rl:
            CoreFoundation.CFRunLoopRemoveSource(
                self._rl, self._rl_source, CoreFoundation.kCFRunLoopDefaultMode
            )

        self._running = False
        self._stream  = None
        print("[CGDS] 推流已停止")

    @property
    def is_running(self) -> bool:
        return self._running


# ────────────────────────────────────────────────────────
# 与 screen_recorder.py 集成的录制接口
# ────────────────────────────────────────────────────────

def record_with_cgds(
    output_path: str,
    fps: int = 60,
    display_idx: int = 0,
    region: tuple = None,       # (left, top, width, height)
    show_cursor: bool = True,
    duration: float = None,     # None = 等待 stop_event
    stop_event: threading.Event = None,
    preview_callback: Callable = None,  # 每帧调用，用于预览
) -> dict:
    """
    同步录制函数（在主线程运行，自旋 RunLoop）。

    返回录制统计信息 dict。
    """
    import cv2, CoreFoundation

    cap = CGDisplayStreamCapture(display_idx, fps, show_cursor)
    display_id = cap._get_display_id()
    import Quartz
    bounds = Quartz.CGDisplayBounds(display_id)
    full_w = int(bounds.size.width)
    full_h = int(bounds.size.height)

    if region:
        l, t, w, h = region
    else:
        l, t, w, h = 0, 0, full_w, full_h

    fourcc = cv2.VideoWriter_fourcc(*"avc1")
    writer = cv2.VideoWriter(output_path, fourcc, fps, (w, h))
    if not writer.isOpened():
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(output_path, fourcc, fps, (w, h))
    if not writer.isOpened():
        raise RuntimeError(f"无法创建视频文件: {output_path}")

    frame_count = [0]
    t_start = time.perf_counter()
    running = [True]

    def on_frame(frame):
        if not running[0]:
            return
        if region:
            frame = frame[t:t+h, l:l+w]
        writer.write(frame)
        frame_count[0] += 1
        if preview_callback:
            preview_callback(frame)

    cap.start(on_frame)

    print(f"[CGDS] 录制中...  分辨率={w}x{h}  FPS={fps}")
    print(f"       输出: {output_path}")

    deadline = (time.perf_counter() + duration) if duration else None

    try:
        while running[0]:
            CoreFoundation.CFRunLoopRunInMode(
                CoreFoundation.kCFRunLoopDefaultMode, 0.016, True
            )
            if stop_event and stop_event.is_set():
                break
            if deadline and time.perf_counter() >= deadline:
                break
    finally:
        running[0] = False
        cap.stop()
        writer.release()

    elapsed = time.perf_counter() - t_start
    actual_fps = frame_count[0] / elapsed if elapsed > 0 else 0
    stats = {
        "frames":     frame_count[0],
        "duration_s": round(elapsed, 2),
        "actual_fps": round(actual_fps, 1),
        "output":     output_path,
    }
    print(f"\n[CGDS] 录制完成!")
    print(f"       帧数: {stats['frames']}")
    print(f"       时长: {stats['duration_s']}s")
    print(f"       实际帧率: {stats['actual_fps']} fps")
    print(f"       文件: {stats['output']}")
    return stats


# ────────────────────────────────────────────────────────
# 独立 CLI 测试入口
# ────────────────────────────────────────────────────────

def _demo_preview():
    """预览演示 (按 q 退出)"""
    import cv2, CoreFoundation

    frame_q: queue.Queue = queue.Queue(maxsize=4)
    stopped = [False]

    cap = CGDisplayStreamCapture(display_idx=0, fps=60)

    def on_frame(f):
        if stopped[0]: return
        if frame_q.full():
            try: frame_q.get_nowait()
            except queue.Empty: pass
        try: frame_q.put_nowait(f)
        except queue.Full: pass

    cap.start(on_frame)
    print("按 'q' 退出预览")

    fc = 0
    t0 = time.perf_counter()

    while True:
        CoreFoundation.CFRunLoopRunInMode(
            CoreFoundation.kCFRunLoopDefaultMode, 0.01, True
        )
        try:
            frame = frame_q.get_nowait()
        except queue.Empty:
            continue

        fc += 1
        elapsed = time.perf_counter() - t0
        fps_now = fc / elapsed if elapsed > 0 else 0

        h, w = frame.shape[:2]
        preview = cv2.resize(frame, (w // 2, h // 2))
        hud = f"CGDisplayStream  {fps_now:.1f}fps  frame={fc}"
        cv2.rectangle(preview, (0, 0), (len(hud)*11, 30), (15, 15, 15), -1)
        cv2.putText(preview, hud, (8, 22),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.65, (100, 255, 50), 2)
        cv2.imshow("CGDisplayStream Deep Capture", preview)

        if cv2.waitKey(1) & 0xFF in (ord("q"), 27):
            break

    stopped[0] = True
    cap.stop()
    cv2.destroyAllWindows()
    elapsed = time.perf_counter() - t0
    print(f"采集 {fc} 帧 / {elapsed:.1f}s = {fc/elapsed:.1f}fps")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="CGDisplayStream 深层采集测试")
    parser.add_argument("--duration", "-d", type=float, default=None, help="录制时长(秒)")
    parser.add_argument("--output",   "-o", type=str,   default=None, help="输出文件")
    parser.add_argument("--fps",      "-f", type=int,   default=60,   help="帧率")
    parser.add_argument("--preview",  "-p", action="store_true",      help="预览模式")
    args = parser.parse_args()

    if sys.platform != "darwin":
        print("[ERROR] 仅支持 macOS")
        sys.exit(1)

    if args.preview or (not args.duration and not args.output):
        _demo_preview()
    else:
        from pathlib import Path
        from datetime import datetime
        out = args.output or str(Path.cwd() / f"cgds_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4")
        record_with_cgds(output_path=out, fps=args.fps, duration=args.duration)
