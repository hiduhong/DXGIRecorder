# macOS 深层屏幕录制工具

基于 **CGDisplayStream**（GPU帧缓冲推流）的 Python 屏幕录制工具，比普通截图方案更底层。

## 后端层级

```
层级   API                      数据路径                              命令行可用
────   ─────────────────────    ──────────────────────────────────   ──────────
浅层   mss / CGWindowList       CGWindowListCreateImage → bitmap     ✅
中层   CGDisplayStream (本项目) GPU帧缓冲 → IOSurface → numpy        ✅ 推荐
深层   ScreenCaptureKit SCStream XPC → CMSampleBuffer → numpy        ⚠️ 需 .app
```

### CGDisplayStream vs mss 对比

| | mss (截图) | CGDisplayStream (本项目) |
|--|--|--|
| 模式 | 截图（每帧独立请求） | 推流（GPU主动推送） |
| 延迟 | 较高 | 低 |
| 最高帧率 | ~30fps | 60/120fps |
| 数据路径 | CGWindowList → CPU bitmap | GPU帧缓冲 → IOSurface → ctypes |
| 等价 Windows | GDI BitBlt | DXGI Desktop Duplication |

## 🍎 实战：针对“隐藏软件”的深层捕获

某些软件会通过 `CGWindowListOptionExcludeDesktopElements` 或特定的透明度层试图在普通截图工具（如 mss, OBS 窗口采集）下隐藏显示内容。

由于本项目使用的是 **CGDisplayStream** 后端，它是直接从 **GPU 帧缓冲 (FrameBuffer)** 读取经过合成的最终画面，因此这类“软件层面对隐藏”技巧对本项目无效。

### 演示视频：打破软件隐藏
<video src="assets/demo_deep_capture.mp4" controls="controls" width="100%"></video>

---

## 环境要求

- **macOS**: 12.0+
- **Python**: 3.10+（推荐使用 conda `test` 环境）
- **权限**: 系统设置 → 隐私与安全性 → **屏幕录制** → 勾选 Terminal / Python

---

## 安装

```bash
# 使用 conda test 环境安装依赖
conda run -n test pip install -r requirements.txt
```

> **⚠️ 重要**：运行前需要对 Python 二进制签名屏幕录制权限（一次性操作）：
> ```bash
> # 找到 conda 环境的 python 路径
> PYTHON=$(conda run -n test which python3.11)
>
> # 签名（注入屏幕录制 entitlement）
> codesign --sign - --force --entitlements entitlements.plist $PYTHON
> ```

---

## 使用方法

> 所有命令使用 **绝对路径 Python**，不要用 `conda run`（会 fork 子进程导致签名失效）

```bash
PYTHON=/Users/user/miniconda3/envs/test/bin/python3.11
```

---

### CGDisplayStream 后端（推荐 · 更深层）

#### 实时预览

```bash
$PYTHON cgds_backend.py --preview
# 按 q 退出
```

#### 指定时长录制

```bash
# 录制 30 秒，60fps
$PYTHON cgds_backend.py --duration 30 --fps 60

# 指定输出文件 + 时长
$PYTHON cgds_backend.py --duration 60 --fps 60 --output ~/Desktop/capture.mp4
```

#### 代码调用

```python
from cgds_backend import record_with_cgds
import threading

stop = threading.Event()

# 同步录制（需要在主线程运行）
stats = record_with_cgds(
    output_path="output.mp4",
    fps=60,
    display_idx=0,          # 显示器索引
    duration=30,            # 秒，None=等待 stop_event
    stop_event=stop,        # 手动停止
)
print(stats)
# → {'frames': 1800, 'duration_s': 30.0, 'actual_fps': 60.0, 'output': '...'}
```

---

### screen_recorder.py（高层接口 · 支持预览）

```bash
# 查看显示器列表
$PYTHON screen_recorder.py --list-displays

# 录制主显示器 30 秒
$PYTHON screen_recorder.py --duration 30

# 实时预览（按 q 停止）
$PYTHON screen_recorder.py --preview

# 录制第二台显示器，60fps
$PYTHON screen_recorder.py --monitor 1 --fps 60

# 录制指定区域 (left top width height)
$PYTHON screen_recorder.py --region 0 0 1280 720 --duration 30
```

#### 参数说明

| 参数 | 简写 | 默认 | 说明 |
|------|------|------|------|
| `--list-displays` | | | 列出所有显示器 |
| `--output` | `-o` | 自动命名 | 输出 MP4 路径 |
| `--fps` | `-f` | 30 | 目标帧率 |
| `--monitor` | `-m` | 0 | 显示器索引 |
| `--region` | `-r` | 全屏 | `Left Top Width Height` |
| `--codec` | | avc1 | H264=avc1, MPEG4=mp4v |
| `--duration` | `-d` | 手动停止 | 自动停止秒数 |
| `--preview` | `-p` | 关 | 实时预览窗口 |
| `--scale` | | 0.5 | 预览缩放比例 |

#### 代码调用

```python
from screen_recorder import MacScreenRecorder
import time

recorder = MacScreenRecorder(
    output_path="output.mp4",
    fps=60,
    monitor_idx=0,
    region=(0, 0, 1920, 1080),  # 左 上 宽 高，None=全屏
)
recorder.start()       # 后台线程，不阻塞
time.sleep(10)
stats = recorder.stop()
```

---

## 项目结构

```
DXGIRecorder/
├── cgds_backend.py      # ✅ 深层后端：CGDisplayStream GPU帧缓冲推流
├── screen_recorder.py   # ✅ 高层接口：自动选后端 + CLI + 预览
├── sck_backend.py       # 🔬 实验性：ScreenCaptureKit（需要 .app bundle）
├── entitlements.plist   # 签名配置（屏幕录制 entitlement）
├── requirements.txt     # Python 依赖
└── README.md
```

---

## 常见问题

### conda run 崩溃 (exit 133)

`conda run` 会 fork 子进程，codesign 签名不继承。必须使用绝对路径：

```bash
# ❌ 错误
conda run -n test python screen_recorder.py

# ✅ 正确
/Users/user/miniconda3/envs/test/bin/python3.11 screen_recorder.py
```

### 黑屏 / 只录到壁纸

未授权屏幕录制权限：

> 系统设置 → 隐私与安全性 → 屏幕录制 → 开启 Terminal 或 Python

### ScreenCaptureKit SCStream 崩溃

SCKit 的 `startCapture` 需要 `.app bundle`，命令行 python 会被 `replayd` XPC 拒绝并 abort。详见 [SCK_LIMITATION.md](SCK_LIMITATION.md)。

### 帧率低于目标

- 使用 `cgds_backend.py` 替代 `screen_recorder.py`（推流 vs 截图）
- 用 `--region` 缩小采集区域
- 关闭 `--preview`
