"""
sck_backend.py — 深层 ScreenCaptureKit 后端
=============================================
等价于 Windows 的 DXGI Desktop Duplication API。

数据路径:
  SCStream 帧回调 (后台串行队列)
  → CMSampleBuffer → CVImageBuffer (IOSurface)
  → CVPixelBufferLockBaseAddress (锁定 CPU 读取)
  → BGRA bytes → numpy → BGR

关键设计:
  ✅ 用 _spin_runloop() 自旋 NSRunLoop 等待所有 SCK 异步回调
  ✅ 帧回调分派到独立后台串行队列，不阻塞主线程
  ✅ 经过 ObjC runtime 内省验证的真实方法签名

依赖:
  pip install pyobjc-core pyobjc-framework-ScreenCaptureKit \
              pyobjc-framework-CoreMedia pyobjc-framework-Quartz

平台: macOS 12.3+
"""

from __future__ import annotations

import sys
import time
import threading
import queue
from typing import Callable, Optional

import numpy as np


# ────────────────────────────────────────────────────────
# 依赖检查
# ────────────────────────────────────────────────────────

def _check_deps() -> bool:
    try:
        import objc       # noqa
        import Quartz     # noqa  (包含 CoreVideo 绑定)
        import CoreMedia  # noqa
        import Foundation # noqa
        return True
    except ImportError:
        return False


PYOBJC_AVAILABLE = _check_deps()


class SCKUnavailableError(RuntimeError):
    """SCK 不可用时抛出"""
    pass


# ────────────────────────────────────────────────────────
# NSRunLoop 自旋 — 解决 SCK 异步回调问题
# ────────────────────────────────────────────────────────

def _spin_runloop(done_event: threading.Event, timeout: float = 5.0) -> bool:
    """
    自旋当前线程的 NSRunLoop 直到 done_event 被设置或超时。

    SCK 所有 completionHandler 通过 GCD 分派，需要 RunLoop 转动才能触发。
    直接 Event.wait() 会阻塞 RunLoop，导致回调永不触发。
    """
    import Foundation
    runloop = Foundation.NSRunLoop.currentRunLoop()
    deadline = time.monotonic() + timeout
    while not done_event.is_set() and time.monotonic() < deadline:
        runloop.runUntilDate_(
            Foundation.NSDate.dateWithTimeIntervalSinceNow_(0.05)
        )
    return done_event.is_set()


# ────────────────────────────────────────────────────────
# 核心实现
# ────────────────────────────────────────────────────────

if PYOBJC_AVAILABLE:
    import objc
    import Quartz
    import CoreMedia
    import Foundation
    import Quartz.CoreVideo as CoreVideo

    try:
        import ScreenCaptureKit as SCK
        _SCK_NATIVE = True
    except ImportError:
        _SCK_NATIVE = False

    # ── CMSampleBuffer → numpy BGR ──────────────────────
    def _sample_buffer_to_numpy(sample_buffer) -> Optional[np.ndarray]:
        """
        CMSampleBuffer → numpy BGR

        数据路径:
          CMSampleBuffer
          → CMSampleBufferGetImageBuffer() → CVPixelBuffer (IOSurface)
          → CVPixelBufferLockBaseAddress   (锁定 CPU, 等价 DXGI Map)
          → 读取 BGRA 字节               → numpy
          → CVPixelBufferUnlockBaseAddress (解锁, 等价 DXGI Unmap)
        """
        import cv2, ctypes

        pixel_buffer = CoreMedia.CMSampleBufferGetImageBuffer(sample_buffer)
        if pixel_buffer is None:
            return None

        CoreVideo.CVPixelBufferLockBaseAddress(
            pixel_buffer, CoreVideo.kCVPixelBufferLock_ReadOnly
        )
        try:
            width  = CoreVideo.CVPixelBufferGetWidth(pixel_buffer)
            height = CoreVideo.CVPixelBufferGetHeight(pixel_buffer)
            bpr    = CoreVideo.CVPixelBufferGetBytesPerRow(pixel_buffer)
            base   = CoreVideo.CVPixelBufferGetBaseAddress(pixel_buffer)

            if base is None or width == 0 or height == 0:
                return None

            buf = (ctypes.c_uint8 * (bpr * height)).from_address(int(base))
            arr = np.frombuffer(buf, dtype=np.uint8).reshape((height, bpr // 4, 4))
            frame_bgra = arr[:, :width, :]
            frame_bgr  = cv2.cvtColor(frame_bgra, cv2.COLOR_BGRA2BGR)
            return frame_bgr.copy()
        finally:
            CoreVideo.CVPixelBufferUnlockBaseAddress(
                pixel_buffer, CoreVideo.kCVPixelBufferLock_ReadOnly
            )

    # ── SCStreamOutput Delegate ─────────────────────────
    if _SCK_NATIVE:
        class _SCStreamDelegate(objc.lookUpClass("NSObject")):
            """实现 SCStreamOutput + SCStreamDelegate 协议"""
            _protocols_ = [
                objc.protocolNamed("SCStreamOutput"),
                objc.protocolNamed("SCStreamDelegate"),
            ]

            def initWithCallback_(self, callback: Callable):
                self = objc.super(_SCStreamDelegate, self).init()
                if self is None:
                    return None
                self._callback = callback
                return self

            def stream_didOutputSampleBuffer_ofType_(
                self, stream, sample_buffer, output_type
            ):
                """帧回调 — 在后台串行队列调用"""
                try:
                    frame = _sample_buffer_to_numpy(sample_buffer)
                    if frame is not None:
                        self._callback(frame)
                except Exception as e:
                    print(f"[SCK] 帧处理异常: {e}")

            def stream_didStopWithError_(self, stream, error):
                if error:
                    print(f"[SCK] 流错误: {error}")


# ────────────────────────────────────────────────────────
# 公开采集器
# ────────────────────────────────────────────────────────

class SCKCapture:
    """
    ScreenCaptureKit 深层采集器。

    用法:
        cap = SCKCapture(monitor_idx=0, fps=60)
        cap.start(callback=lambda frame: ...)
        ...
        cap.stop()
    """

    def __init__(
        self,
        monitor_idx: int = 0,
        fps: int = 60,
        show_cursor: bool = True,
    ):
        if not PYOBJC_AVAILABLE:
            raise SCKUnavailableError(
                "pyobjc 未安装。请运行:\n"
                "  pip install pyobjc-core pyobjc-framework-ScreenCaptureKit "
                "pyobjc-framework-CoreMedia pyobjc-framework-Quartz"
            )
        if not _SCK_NATIVE:
            raise SCKUnavailableError(
                "ScreenCaptureKit 绑定不可用: pip install pyobjc-framework-ScreenCaptureKit"
            )

        self.monitor_idx = monitor_idx
        self.fps         = fps
        self.show_cursor = show_cursor
        self._stream     = None
        self._delegate   = None
        self._bg_queue   = None
        self._running    = False

    # ── 内部辅助 ──────────────────────────────────────

    def _get_displays_sync(self) -> list:
        """
        同步获取显示器列表。
        使用正确的 ObjC 方法名: getShareableContentWithCompletionHandler:
        （经 ObjC runtime 内省验证）
        """
        result = [None, None]   # [content, error]
        done   = threading.Event()

        def handler(content, error):
            result[0] = content
            result[1] = error
            done.set()

        # ✅ 正确方法名（不是 getWithCompletionHandler_）
        SCK.SCShareableContent.getShareableContentWithCompletionHandler_(handler)

        ok = _spin_runloop(done, timeout=5.0)
        if not ok or result[0] is None:
            err = str(result[1]) if result[1] else "超时"
            raise SCKUnavailableError(
                f"无法枚举显示器 ({err})。\n"
                "请前往: 系统设置 → 隐私与安全性 → 屏幕录制 → 授权 Terminal/Python"
            )
        return list(result[0].displays())

    def _build_config(self, display) -> object:
        """
        构建 SCStreamConfiguration。
        使用 ObjC runtime 验证的 setter 方法名。
        """
        cfg = SCK.SCStreamConfiguration.alloc().init()
        # -setMinimumFrameInterval:  ✅
        cfg.setMinimumFrameInterval_(CoreMedia.CMTimeMake(1, int(self.fps)))
        # -setPixelFormat:           ✅
        cfg.setPixelFormat_(Quartz.kCVPixelFormatType_32BGRA)
        # -setWidth: / -setHeight:   ✅
        cfg.setWidth_(display.width())
        cfg.setHeight_(display.height())
        # -setShowsCursor:           ✅
        cfg.setShowsCursor_(self.show_cursor)
        # -setQueueDepth:            ✅
        cfg.setQueueDepth_(3)
        return cfg

    # ── 公开接口 ──────────────────────────────────────

    def start(self, callback: Callable[[np.ndarray], None]) -> None:
        """启动 SCStream 推流，每帧调用 callback(frame_bgr)"""
        if self._running:
            return

        displays = self._get_displays_sync()
        if self.monitor_idx >= len(displays):
            raise ValueError(
                f"显示器索引 {self.monitor_idx} 越界，共 {len(displays)} 台"
            )

        display = displays[self.monitor_idx]

        # 内容过滤器: -initWithDisplay:excludingWindows:  ✅
        content_filter = (
            SCK.SCContentFilter.alloc()
            .initWithDisplay_excludingWindows_(display, [])
        )

        cfg = self._build_config(display)

        # 创建 Delegate
        self._delegate = _SCStreamDelegate.alloc().initWithCallback_(callback)

        # 创建 SCStream: -initWithFilter:configuration:delegate:  ✅
        self._stream = SCK.SCStream.alloc().initWithFilter_configuration_delegate_(
            content_filter, cfg, self._delegate
        )

        # 后台串行队列（帧回调在此运行，不占主线程）
        self._bg_queue = Foundation.NSOperationQueue.alloc().init()
        self._bg_queue.setMaxConcurrentOperationCount_(1)

        # 注册输出: -addStreamOutput:type:sampleHandlerQueue:error:  ✅
        err_ref = objc.NULL
        ok = self._stream.addStreamOutput_type_sampleHandlerQueue_error_(
            self._delegate,
            SCK.SCStreamOutputTypeScreen,
            self._bg_queue,
            err_ref,
        )
        if not ok:
            raise SCKUnavailableError("addStreamOutput 失败")

        # 启动: -startCaptureWithCompletionHandler:  ✅
        done   = threading.Event()
        err_box = [None]

        def _on_start(error):
            err_box[0] = error
            done.set()

        self._stream.startCaptureWithCompletionHandler_(_on_start)
        ok = _spin_runloop(done, timeout=8.0)

        if not ok:
            raise SCKUnavailableError("SCStream.startCapture 超时 (8s)")
        if err_box[0]:
            raise SCKUnavailableError(f"SCStream 启动失败: {err_box[0]}")

        self._running = True
        print(f"[SCK] ✅ 推流启动  显示器=[{self.monitor_idx}]  "
              f"{display.width()}x{display.height()}  {self.fps}fps")
        print(f"[SCK]    数据路径: GPU→IOSurface→CMSampleBuffer→CVPixelBuffer→numpy")

    def stop(self) -> None:
        """停止 SCStream"""
        if not self._running or self._stream is None:
            return

        done = threading.Event()

        def _on_stop(error):
            if error:
                print(f"[SCK] 停止时出错: {error}")
            done.set()

        self._stream.stopCaptureWithCompletionHandler_(_on_stop)
        _spin_runloop(done, timeout=3.0)

        self._running = False
        self._stream  = None
        print("[SCK] 推流已停止")

    @property
    def is_running(self) -> bool:
        return self._running


# ────────────────────────────────────────────────────────
# 帧迭代器
# ────────────────────────────────────────────────────────

class SCKFrameIterator:
    """
    同步帧迭代器，将 SCK 推流回调封装为 Python for 循环。

    用法:
        with SCKFrameIterator(fps=60) as frames:
            for frame in frames:    # frame: np.ndarray BGR
                cv2.imshow("SCK", frame)
                if cv2.waitKey(1) & 0xFF == ord('q'):
                    break
    """

    def __init__(
        self,
        monitor_idx: int = 0,
        fps: int = 60,
        show_cursor: bool = True,
        queue_size: int = 4,
        timeout: float = 1.0,
    ):
        self._cap     = SCKCapture(monitor_idx, fps, show_cursor)
        self._queue: queue.Queue = queue.Queue(maxsize=queue_size)
        self._timeout  = timeout
        self._stopped  = False

    def _on_frame(self, frame: np.ndarray):
        if self._stopped:
            return
        if self._queue.full():
            try:
                self._queue.get_nowait()  # 丢弃最旧帧，保持低延迟
            except queue.Empty:
                pass
        try:
            self._queue.put_nowait(frame)
        except queue.Full:
            pass

    def __enter__(self):
        self._cap.start(self._on_frame)
        return self

    def __exit__(self, *_):
        self._stopped = True
        self._cap.stop()

    def __iter__(self):
        return self

    def __next__(self) -> np.ndarray:
        while not self._stopped:
            try:
                return self._queue.get(timeout=self._timeout)
            except queue.Empty:
                continue
        raise StopIteration


# ────────────────────────────────────────────────────────
# 独立测试入口
# ────────────────────────────────────────────────────────

def _demo():
    import cv2
    print("=== SCK 深层采集演示 (按 q 退出) ===")
    fc = 0
    t0 = time.perf_counter()

    with SCKFrameIterator(fps=60) as frames:
        for frame in frames:
            fc += 1
            elapsed = time.perf_counter() - t0
            fps_now = fc / elapsed if elapsed > 0 else 0

            h, w = frame.shape[:2]
            preview = cv2.resize(frame, (w // 2, h // 2))
            hud = f"SCK GPU-stream  {fps_now:.1f}fps  frame={fc}"
            cv2.rectangle(preview, (0, 0), (len(hud)*11, 30), (15, 15, 15), -1)
            cv2.putText(preview, hud, (8, 22),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 200, 255), 2)
            cv2.imshow("SCK Deep Capture", preview)
            if cv2.waitKey(1) & 0xFF in (ord("q"), 27):
                break

    cv2.destroyAllWindows()
    elapsed = time.perf_counter() - t0
    print(f"采集 {fc} 帧 / {elapsed:.1f}s = {fc/elapsed:.1f}fps")


if __name__ == "__main__":
    if sys.platform != "darwin":
        print("[ERROR] 仅支持 macOS")
        sys.exit(1)
    import platform
    ver = tuple(int(x) for x in platform.mac_ver()[0].split(".")[:2])
    if ver < (12, 3):
        print(f"[ERROR] 需要 macOS 12.3+，当前: {platform.mac_ver()[0]}")
        sys.exit(1)
    _demo()
