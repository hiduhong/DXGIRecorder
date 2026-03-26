#!/usr/bin/env python3
"""
stealth_v2.py — 终极反录屏方案：多重技术栈绕过 CGDisplayStream
"""

import sys
import os
import signal
import ctypes
import ctypes.util
import time
import platform

try:
    import objc
    import AppKit
    from AppKit import (
        NSWindow, NSApplication, NSWindowSharingNone, NSWindowSharingReadOnly,
        NSObject, NSMakeRect, NSTitledWindowMask, NSClosableWindowMask,
        NSMiniaturizableWindowMask,
        NSBackingStoreBuffered, NSColor, NSTextField, NSButton,
        NSMomentaryLightButton, NSCenterTextAlignment, NSFont,
        NSApplicationActivationPolicyRegular, NSRoundedBezelStyle,
        NSSecureTextField, NSView, NSScrollView, NSTextView,
        NSBorderlessWindowMask,
    )
    import Quartz
    from Quartz import (
        CGMainDisplayID, CGDisplayCreateImage, CGRectMake,
        CGImageGetWidth, CGImageGetHeight,
        CGWindowListCreateImage, kCGWindowListOptionAll,
        kCGWindowImageDefault, kCGNullWindowID,
    )
except ImportError as e:
    print(f"请先安装 pyobjc:\n  pip install pyobjc-core pyobjc-framework-Cocoa pyobjc-framework-Quartz")
    print(f"导入失败: {e}")
    sys.exit(1)


# ═══════════════════════════════════════════════════════════════════════
# 工具：获取 macOS 版本
# ═══════════════════════════════════════════════════════════════════════
def macos_version():
    ver = platform.mac_ver()[0]
    parts = ver.split('.')
    return tuple(int(x) for x in parts)


# ═══════════════════════════════════════════════════════════════════════
# 层 A: SkyLight 私有框架
# ═══════════════════════════════════════════════════════════════════════
class SkyLightAPI:
    def __init__(self):
        self.lib = None
        self.cid = None
        self._available_funcs = {}
        self._load()

    def _load(self):
        paths = [
            '/System/Library/PrivateFrameworks/SkyLight.framework/SkyLight',
            '/System/Library/PrivateFrameworks/SkyLight.framework/Versions/A/SkyLight',
        ]
        for p in paths:
            try:
                self.lib = ctypes.cdll.LoadLibrary(p)
                break
            except OSError:
                continue

        if not self.lib:
            print("⚠️  SkyLight.framework 无法加载 (可能是 SIP 限制)")
            return

        for fname in ['SLSMainConnectionID', 'CGSMainConnectionID']:
            try:
                func = getattr(self.lib, fname)
                func.restype = ctypes.c_uint32
                self.cid = func()
                print(f"✅ {fname}() = {self.cid}")
                break
            except AttributeError:
                continue

        if self.cid is None:
            print("⚠️  无法获取 CGS 连接 ID")
            return

        probe_list = [
            'SLSSetWindowCaptureExclude',
            'CGSSetWindowCaptureExclude',
            'SLSSetWindowTags',
            'SLSClearWindowTags',
            'CGSSetWindowTags',
            'CGSClearWindowTags',
            'SLSSetWindowSharingState',
            'CGSSetWindowSharingState',
            'SLSSetWindowOpacity',
            'SLSSetWindowLevel',
            'SLSSetWindowProperty',
            'CGSSetWindowProperty',
        ]
        for name in probe_list:
            try:
                f = getattr(self.lib, name)
                self._available_funcs[name] = f
                print(f"  🔍 {name}: 可用")
            except AttributeError:
                pass

    @property
    def available(self):
        return self.lib is not None and self.cid is not None

    def set_capture_exclude(self, window_id: int, exclude: bool) -> bool:
        for fname in ['SLSSetWindowCaptureExclude', 'CGSSetWindowCaptureExclude']:
            if fname in self._available_funcs:
                func = self._available_funcs[fname]
                func.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_bool]
                func.restype = ctypes.c_int32
                ret = func(self.cid, window_id, exclude)
                status = "成功" if ret == 0 else f"返回 {ret}"
                print(f"  ➡️ {fname}(cid={self.cid}, wid={window_id}, {exclude}) => {status}")
                return ret == 0
        print("  ❌ CaptureExclude 函数不可用")
        return False

    def set_window_tags(self, window_id: int, tag_bits: int, do_set: bool = True) -> bool:
        set_name = None
        for prefix in ['SLS', 'CGS']:
            n = f'{prefix}SetWindowTags' if do_set else f'{prefix}ClearWindowTags'
            if n in self._available_funcs:
                set_name = n
                break

        if not set_name:
            print("  ❌ WindowTags 函数不可用")
            return False

        func = self._available_funcs[set_name]
        func.argtypes = [
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.c_int32,
        ]
        func.restype = ctypes.c_int32

        tags = ctypes.c_uint64(tag_bits)
        ret = func(self.cid, window_id, ctypes.byref(tags), 64)
        action = "设置" if do_set else "清除"
        status = "成功" if ret == 0 else f"返回 {ret}"
        print(f"  ➡️ {set_name}(wid={window_id}, tags=0x{tag_bits:x}) => {status}")
        return ret == 0

    def set_sharing_state(self, window_id: int, state: int) -> bool:
        for fname in ['SLSSetWindowSharingState', 'CGSSetWindowSharingState']:
            if fname in self._available_funcs:
                func = self._available_funcs[fname]
                func.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_int32]
                func.restype = ctypes.c_int32
                ret = func(self.cid, window_id, state)
                return ret == 0
        return False


# ═══════════════════════════════════════════════════════════════════════
# 层 C: NSSecureTextField 黑魔法
# ═══════════════════════════════════════════════════════════════════════
def make_secure_host_view(frame_rect):
    secure_field = NSSecureTextField.alloc().initWithFrame_(frame_rect)
    secure_field.setWantsLayer_(True)
    secure_field.setBezeled_(False)
    secure_field.setDrawsBackground_(False)
    secure_field.setEditable_(False)
    secure_field.setSelectable_(False)
    secure_field.setStringValue_("")
    secure_field.setAlphaValue_(1.0)
    return secure_field


# ═══════════════════════════════════════════════════════════════════════
# 自检工具
# ═══════════════════════════════════════════════════════════════════════
def self_test_window_visible(window):
    results = {}

    display_id = CGMainDisplayID()
    img = CGDisplayCreateImage(display_id)
    if img:
        results['CGDisplayCreateImage'] = f"截图成功 ({CGImageGetWidth(img)}x{CGImageGetHeight(img)})"
    else:
        results['CGDisplayCreateImage'] = "截图失败 (可能需要录屏权限)"

    screen_rect = CGRectMake(0, 0, 0, 0)
    img2 = CGWindowListCreateImage(screen_rect, kCGWindowListOptionAll, kCGNullWindowID, kCGWindowImageDefault)
    if img2:
        results['CGWindowListCreateImage'] = f"截图成功 ({CGImageGetWidth(img2)}x{CGImageGetHeight(img2)})"
    else:
        results['CGWindowListCreateImage'] = "截图失败"

    wid = window.windowNumber()
    from Quartz import kCGWindowListOptionIncludingWindow
    img3 = CGWindowListCreateImage(
        screen_rect,
        kCGWindowListOptionIncludingWindow,
        wid,
        kCGWindowImageDefault,
    )
    if img3:
        w, h = CGImageGetWidth(img3), CGImageGetHeight(img3)
        if w > 1 and h > 1:
            results['CGWindowList(单窗口)'] = f"可见 ({w}x{h})"
        else:
            results['CGWindowList(单窗口)'] = f"异常尺寸 ({w}x{h})"
    else:
        results['CGWindowList(单窗口)'] = "✅ 窗口不可见（被屏蔽）"

    return results


def save_screenshot(window, filename_prefix="stealth_test"):
    from Quartz import (
        CGDisplayCreateImage, CGMainDisplayID,
        CGImageDestinationCreateWithURL, CGImageDestinationAddImage,
        CGImageDestinationFinalize,
    )
    from Foundation import NSURL

    desktop = os.path.expanduser("~/Desktop")
    display_id = CGMainDisplayID()

    for method_name, capture_func in [
        ("CGDisplay", lambda: CGDisplayCreateImage(display_id)),
        ("CGWindowList", lambda: CGWindowListCreateImage(
            CGRectMake(0, 0, 0, 0), kCGWindowListOptionAll, kCGNullWindowID, kCGWindowImageDefault
        )),
    ]:
        img = capture_func()
        if img:
            path = os.path.join(desktop, f"{filename_prefix}_{method_name}.png")
            url = NSURL.fileURLWithPath_(path)
            dest = CGImageDestinationCreateWithURL(url, "public.png", 1, None)
            if dest:
                CGImageDestinationAddImage(dest, img, None)
                CGImageDestinationFinalize(dest)
                print(f"  📸 已保存: {path}")


# ═══════════════════════════════════════════════════════════════════════
# 主 App 代理 — 所有纯 Python 方法加 @objc.python_method
# ═══════════════════════════════════════════════════════════════════════
class AppDelegate(NSObject):

    def applicationDidFinishLaunching_(self, notification):
        self.skylight = SkyLightAPI()
        self.is_stealth = False

        rect = NSMakeRect(300, 300, 520, 450)
        style = NSTitledWindowMask | NSClosableWindowMask | NSMiniaturizableWindowMask
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, style, NSBackingStoreBuffered, False,
        )
        self.window.setTitle_("🛡️ 反录屏终极版 — 多重绕过 CGDisplayStream")
        self.window.setLevel_(3)
        self.window.setOpaque_(False)
        self.window.setHasShadow_(True)
        self.window.setIgnoresMouseEvents_(False)

        cv = self.window.contentView()
        cv.setWantsLayer_(True)

        self.secure_host = make_secure_host_view(cv.bounds())
        cv.addSubview_(self.secure_host)

        self.label = NSTextField.alloc().initWithFrame_(NSMakeRect(20, 220, 480, 170))
        self.label.setEditable_(False)
        self.label.setBezeled_(False)
        self.label.setDrawsBackground_(False)
        self.label.setSelectable_(False)
        self.label.setAlignment_(NSCenterTextAlignment)
        self.label.setFont_(NSFont.systemFontOfSize_(14))
        cv.addSubview_(self.label)

        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(20, 90, 480, 120))
        scroll.setHasVerticalScroller_(True)
        scroll.setBorderType_(1)
        self.log_view = NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, 460, 120))
        self.log_view.setEditable_(False)
        self.log_view.setFont_(NSFont.monospacedSystemFontOfSize_weight_(11, 0.0))
        scroll.setDocumentView_(self.log_view)
        cv.addSubview_(scroll)

        self.btn_toggle = self._make_button("🔮 切换: 隐身/可见", 20, 40, 160, 40, self.toggleStealth_)
        self.btn_test = self._make_button("🧪 自检验证", 190, 40, 120, 40, self.runSelfTest_)
        self.btn_save = self._make_button("💾 保存截图对比", 320, 40, 160, 40, self.saveScreenshot_)

        cv.addSubview_(self.btn_toggle)
        cv.addSubview_(self.btn_test)
        cv.addSubview_(self.btn_save)

        self._update_ui()
        self.window.makeKeyAndOrderFront_(self)

        self._log("═══ 反录屏终极版启动 ═══")
        self._log(f"macOS 版本: {platform.mac_ver()[0]}")
        self._log(f"SkyLight 可用: {self.skylight.available}")
        if self.skylight.available:
            self._log(f"可用函数: {list(self.skylight._available_funcs.keys())}")
        self._log("点击 [🔮 切换] 开启隐身，然后点 [🧪 自检] 验证。")

    @objc.python_method
    def _make_button(self, title, x, y, w, h, action):
        btn = NSButton.alloc().initWithFrame_(NSMakeRect(x, y, w, h))
        btn.setTitle_(title)
        btn.setButtonType_(NSMomentaryLightButton)
        btn.setBezelStyle_(NSRoundedBezelStyle)
        btn.setTarget_(self)
        btn.setAction_(action)
        return btn

    @objc.python_method
    def _log(self, msg):
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] {msg}\n"
        storage = self.log_view.textStorage()
        storage.beginEditing()
        attr_str = AppKit.NSAttributedString.alloc().initWithString_(line)
        storage.appendAttributedString_(attr_str)
        storage.endEditing()
        self.log_view.scrollRangeToVisible_(AppKit.NSMakeRange(storage.length(), 0))
        print(line, end='')

    # ── ObjC action 方法（按钮回调，必须接受 sender 参数）──

    @objc.IBAction
    def toggleStealth_(self, sender):
        self.is_stealth = not self.is_stealth
        wid = self.window.windowNumber()
        self._log(f"{'🟢 开启隐身' if self.is_stealth else '🔴 关闭隐身'} (windowNumber={wid})")

        if self.is_stealth:
            self._engage_stealth(wid)
        else:
            self._disengage_stealth(wid)

        self._update_ui()

    @objc.IBAction
    def runSelfTest_(self, sender):
        self._log("─── 自检开始 ───")
        results = self_test_window_visible(self.window)
        for method, result in results.items():
            self._log(f"  {method}: {result}")
        self._log("─── 自检结束 ───")
        self._log("💡 提示: 点 [💾 保存截图] 可保存到桌面，用肉眼对比窗口是否在截图中可见。")

    @objc.IBAction
    def saveScreenshot_(self, sender):
        self._log("正在保存截图到桌面...")
        mode = "stealth" if self.is_stealth else "visible"
        save_screenshot(self.window, f"stealth_test_{mode}")
        self._log("✅ 已保存！请打开桌面上的 PNG 文件，查看窗口区域是否变黑/消失。")

    # ── 纯 Python 内部方法 ──

    @objc.python_method
    def _engage_stealth(self, wid):
        self.window.setIgnoresMouseEvents_(False)
        self.window.setSharingType_(NSWindowSharingNone)
        self._log("  [E] NSWindowSharingNone ✅")

        try:
            layer = self.window.contentView().layer()
            if layer:
                layer.setValue_forKey_(True, "contentsProtected")
                self._log("  [D] CALayer.contentsProtected = YES ✅")
        except Exception as e:
            self._log(f"  [D] CALayer.contentsProtected 失败: {e}")

        try:
            cv = self.window.contentView()
            self.secure_host.setFrame_(cv.bounds())
            self._log("  [C] NSSecureTextField 安全宿主层 ✅")
        except Exception as e:
            self._log(f"  [C] NSSecureTextField 部署失败: {e}")

        self._log("  [B] 开启 SkyLight WindowTag 隐身/隔离实验")
        tag_capture_exclude = 0x800  # 尝试 kCGSStickyTag 或其他 tags
        if self.skylight.available:
            self.skylight.set_window_tags(wid, tag_capture_exclude, True)
            self.skylight.set_window_tags(wid, 0x10000, True)
            
            self._log("  [B] 正在应用 CGSSetWindowProperty (C级调用) ...")
            try:
                import ctypes
                cf = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
                sls = self.skylight.lib
                cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
                cf.CFStringCreateWithCString.restype = ctypes.c_void_p
                kCFBooleanTrue = ctypes.c_void_p.in_dll(cf, "kCFBooleanTrue")
                sls.SLSSetWindowProperty.argtypes = [ctypes.c_int, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p]
                sls.SLSSetWindowProperty.restype = ctypes.c_int
                
                for key_name in [b"CGSCaptureExclude", b"CGSWindowIsSecure", b"IgnoreForScreenAccess"]:
                    key_ptr = cf.CFStringCreateWithCString(None, key_name, 134217984) # kCFStringEncodingUTF8
                    ret = sls.SLSSetWindowProperty(self.skylight.cid, wid, key_ptr, kCFBooleanTrue)
                    self._log(f"  [B] SLSSetWindowProperty({key_name.decode()}) => {ret}")
            except Exception as e:
                self._log(f"  [B] CGS Property 失败: {e}")

        self._log("  [A] 生成带有真实 CGImage 和背板颜色的实心内容层来强制触发 WindowServer DRM 孔洞")
        try:
            import Quartz
            import AppKit
            cv = self.window.contentView()
            
            # 创建一个带有实际像素的 16x16 图片
            color_space = Quartz.CGColorSpaceCreateDeviceRGB()
            ctx = Quartz.CGBitmapContextCreate(None, 16, 16, 8, 16*4, color_space, Quartz.kCGImageAlphaPremultipliedLast)
            Quartz.CGContextSetRGBFillColor(ctx, 0.0, 0.0, 0.0, 1.0) # 纯黑
            Quartz.CGContextFillRect(ctx, Quartz.CGRectMake(0,0,16,16))
            cg_img = Quartz.CGBitmapContextCreateImage(ctx)
            
            if not hasattr(self, 'drm_layer'):
                self.drm_layer = AppKit.CALayer.layer()
                self.drm_layer.setFrame_(cv.bounds())
                self.drm_layer.setAutoresizingMask_(18) # width/height
                self.drm_layer.setContentsGravity_(Quartz.kCAGravityResize)
                # 重要: 设置极低透明度，让用户肉眼看不见这个遮罩
                self.drm_layer.setOpacity_(0.01)
                self.drm_layer.setContents_(cg_img)
                self.drm_layer.setBackgroundColor_(Quartz.CGColorCreate(color_space, [0.0, 0.0, 0.0, 1.0]))
                
            if self.drm_layer.superlayer() is None:
                # 把这个 DRM layer 放到最顶层，覆盖所有控件！
                cv.layer().addSublayer_(self.drm_layer)
                
            # 设置双重保护: contentsProtected 和 preventsCapture
            self.drm_layer.setValue_forKey_(True, "contentsProtected")
            self.drm_layer.setValue_forKey_(True, "preventsCapture")
            
            self._log("  [A] 置顶 DRM 黑色面纱已应用 (肉眼透明，但录屏应为黑块) ✅")
            
        except Exception as e:
            self._log(f"  [A] 实心图层渲染部署失败: {e}")

    @objc.python_method
    def _disengage_stealth(self, wid):
        self.window.setIgnoresMouseEvents_(False)
        self.window.setSharingType_(NSWindowSharingReadOnly)
        self._log("  [E] NSWindowSharingReadOnly ✅")

        try:
            layer = self.window.contentView().layer()
            if layer:
                layer.setValue_forKey_(False, "contentsProtected")
                layer.setValue_forKey_(False, "preventsCapture")
                self._log("  [D] contentsProtected = NO")
        except Exception:
            pass

        if hasattr(self, 'av_layer') and self.av_layer.superlayer() is not None:
            self.av_layer.setPreventsCapture_(False)
            self.av_layer.removeFromSuperlayer()
            self._log("  [A] AVSampleBufferDisplayLayer 已移除")

        if self.skylight.available:
            self.skylight.set_window_tags(wid, 0x800 | 0x10000, False)
            try:
                import ctypes
                cf = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
                sls = self.skylight.lib
                cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
                cf.CFStringCreateWithCString.restype = ctypes.c_void_p
                kCFBooleanFalse = ctypes.c_void_p.in_dll(cf, "kCFBooleanFalse")
                for key_name in [b"CGSCaptureExclude", b"CGSWindowIsSecure", b"IgnoreForScreenAccess"]:
                    key_ptr = cf.CFStringCreateWithCString(None, key_name, 134217984)
                    sls.SLSSetWindowProperty(self.skylight.cid, wid, key_ptr, kCFBooleanFalse)
            except Exception:
                pass
            
        self._log("  [A/B] 已还原 SkyLight 设置")

    @objc.python_method
    def _update_ui(self):
        if self.is_stealth:
            self.label.setStringValue_(
                "👻 隐身模式已开启\n\n"
                "所有技术栈已叠加激活。\n"
                "请点 🧪自检 或 💾保存截图 来验证效果。\n"
                "也可以用 OBS / QuickTime / 第三方录屏 查看。"
            )
            self.label.setTextColor_(NSColor.systemRedColor())
            self.window.setBackgroundColor_(
                NSColor.colorWithRed_green_blue_alpha_(0.15, 0.0, 0.0, 0.95)
            )
        else:
            self.label.setStringValue_(
                "👀 正常可见模式\n\n"
                "此窗口现在可以被所有录屏方式捕获。\n"
                "点击 🔮切换 来开启隐身。"
            )
            self.label.setTextColor_(NSColor.labelColor())
            self.window.setBackgroundColor_(
                NSColor.colorWithRed_green_blue_alpha_(0.0, 0.1, 0.2, 0.95)
            )

    def applicationShouldTerminateAfterLastWindowClosed_(self, app):
        return True


# ═══════════════════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════════════════
def main():
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    print("=" * 60)
    print("  反录屏终极版 — 多重绕过 CGDisplayStream")
    print("=" * 60)

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)

    delegate = AppDelegate.alloc().init()
    app.setDelegate_(delegate)

    app.activateIgnoringOtherApps_(True)

    from PyObjCTools import AppHelper
    AppHelper.runEventLoop(installInterrupt=True)


if __name__ == "__main__":
    main()