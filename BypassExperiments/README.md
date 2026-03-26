# 屏幕录制底层对抗实验 (Bypass Experiments)

这个项目是用来测试各类 macOS 软件中所谓的“反录屏”、“秘密聊天”、“防盗录”技术，在遭遇真正的硬件级捕获 API（即本仓库的 `CGDisplayStream`）时，会有何种表现的深度技术验证。

## 实验结论

**在现代 macOS（特别是 12+）下，普通的“防录屏 API”对 `CGDisplayStream` 均全面失效！** 只有带有硬件级别数字版权管理（DRM/FairPlay/HDCP）的安全视频流，才有可能导致 `CGDisplayStream` 真正发生硬件黑屏。

我们依次尝试并验证了以下能够绕过传统截图库（如 mss, CGWindowListCreateImage）的方法：

### 失败清单（在 CGDisplayStream 面前原形毕露）：

1. ❌ **`NSWindowSharingNone` (软件级屏蔽)**  
   - 官方推荐的让窗口不要被其他程序共享捕捉的方式。
   - **结果：** 完全失效，在 `cgds_backend.py` 和 OBS 的显示器捕获模式下依旧清晰可见。
   
2. ❌ **`CALayer.secure = YES` (底层渲染级隔离)**  
   - 苹果早期版本的未公开安全修饰符。
   - **结果：** 现代 WindowServer compositor 在向后端推流时，已经无视这个针对普通 UI 层的标记。

3. ❌ **`AVSampleBufferDisplayLayer.preventsCapture` (流媒体级防截屏)**  
   - macOS 13 专为安全软件/流媒体开放的最高权限屏蔽机制。
   - **结果：** 虽然它能黑掉它渲染的*内部视频流数据*，但是加载在上面的其他子视图容器（文字、按钮甚至底色背景），由于其依然走普通的渲染树路径，依旧会被帧缓冲获取，导致无法做到整窗隐身。

4. ❌ **`kCGSWindowSecure` (底层私有指针属性)**  
   - 强行调用内核级 API `CGSSetWindowProperty` 注入 secure 属性。
   - **结果：** 依然无效，再次证明硬件管道数据抽取先于安全过滤检查返回。

---

## 结论意味着什么？

1. 如果你想做的是**恶意/强制录像监控**，我们的 `cgds_backend.py` 代表着**目前能够获得的最大采集深度**（这也等价于 Windows 系统下的 DXGI Desktop Duplication ）。
2. 想在应用态（如 Python 或者各类安全工具的普通软件里），实现“老板键/防截屏”使得全屏录像工具找不到自己——**在现代 macOS 安全机制和硬件直通能力下，此路已经被物理法则彻底堵在底层驱动了。**

如果你是一个安全研究员，你可以拿着这个“大红框”视频，告诉所有人：“**那些号称绝对防止录屏截屏的安全沟通软件，如果不是在使用专门定制的 DRM 容器内强行渲染每帧文字，那么它就在对着你撒谎。**”
