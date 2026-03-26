import ctypes
import AppKit
import CoreFoundation
import signal

cgs = ctypes.CDLL('/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics')
cf = ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')

cgs.CGSMainConnectionID.restype = ctypes.c_int
cid = cgs.CGSMainConnectionID()

cgs.CGSSetWindowProperty.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p]

app = AppKit.NSApplication.sharedApplication()

# 把窗口变大，并设置为纯红色，方便观察
rect = AppKit.NSRect(AppKit.NSPoint(300, 300), AppKit.NSSize(500, 400))
window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
    rect, AppKit.NSTitledWindowMask | AppKit.NSClosableWindowMask, AppKit.NSBackingStoreBuffered, False
)
window.setTitle_("CGS 终极测试 - 纯红方块")
window.setBackgroundColor_(AppKit.NSColor.redColor())
window.makeKeyAndOrderFront_(None)

# 构造 CFString 和 CFBoolean 来调用未公开的 CGS API
cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
cf.CFStringCreateWithCString.restype = ctypes.c_void_p
cf_str = cf.CFStringCreateWithCString(None, b'secure', 134217984) # kCFStringEncodingUTF8
cf_true = ctypes.c_void_p.in_dll(cf, 'kCFBooleanTrue')

print("正在强行调用 WindowServer 底层 API...")
print("尝试注入 kCGSWindowSecure=True 属性。")
err = cgs.CGSSetWindowProperty(cid, window.windowNumber(), cf_str, cf_true)

print(f"执行完毕。CGS 返回码: {err}")
print("👉 请看向屏幕上的纯红色窗口！")
print("👉 并请打开你的 OBS 或者 CGDisplayStream 录制预览程序！")
print("判断标准：如果在录屏预览中，这个纯红色的方块变成了彻底的【黑屏/透明】，说明我们终于绕过了。")
print("如果录出来还是红框，说明这套 API 彻底被苹果在现代 macOS 中封杀了。")

signal.signal(signal.SIGINT, signal.SIG_DFL)
AppKit.NSApp.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)
AppKit.NSApp.activateIgnoringOtherApps_(True)

from PyObjCTools import AppHelper
AppHelper.runEventLoop(installInterrupt=True)
