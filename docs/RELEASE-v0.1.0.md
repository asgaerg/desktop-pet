# DesktopPet v0.1.0

> ⚠️ **先读这段再下载。** 这个项目**没有代码签名**，Windows 和杀软大概率会拦你一下。
> 这不是病毒，但你需要自己确认来源再放行。

---

## ⚠️ 安全提示（重要）

| 你会遇到的提示 | 为什么会这样 | 怎么处理 |
|---|---|---|
| **Windows SmartScreen**：“Windows 已保护你的电脑 / 未知发布者” | exe 没有代码签名证书（一张证书一年几千块，个人项目没买） | 点「更多信息」→「仍要运行」 |
| **杀毒软件**报 `resources\pet\pet.exe` 可疑 | 它是 PyInstaller 打包的 Python 程序，这类 exe 常年被启发式误报 | 加白名单 / 恢复隔离区文件，否则程序起不来（界面会卡在黑屏或直接退出） |
| 浏览器/微信下载后**文件被标记为“来自网络”** | Windows 的 Mark-of-the-Web | 右键属性 → 勾「解除锁定」，或直接运行点「仍要运行」 |
| **安装失败**：“不能打开要写入的文件 …” | 安装包放在了**含中文的路径**里（如 `F:\项目\`），NSIS 的已知毛病 | 先把安装包复制到 `C:\Temp` 这种纯英文路径再运行 |
| 装完**窗口空白 / 只有 not found** | 你装的是旧构建（10-18 之前） | 下这个 Release 里的 v0.1.0，已修复 |

**校验来源**：下载后请对照本页末尾的 SHA256（`SHA256SUMS.txt`）。

```powershell
Get-FileHash .\DesktopPet-0.1.0-online-setup.exe -Algorithm SHA256
```

---

## 两个版本选哪个

| | 在线版（online） | 离线版（offline） |
|---|---|---|
| 安装包 | **278 MB** | **1,630 MB** |
| 绿色包 | **354 MB** | **1,777 MB** |
| 语音识别 | 默认走云端；也可自己放本地模型（首次自动下 1.5 GB） | **自带** large-v3-turbo（1.55 GB），装完即可离线识别 |
| 适合 | 有网、硬盘紧张 | 没网 / 想让语音完全不出本机 |

两个版本**功能完全一致**，区别只有那 1.55 GB 的语音模型带不带。

---

## 安装

```powershell
# 1) 先复制到纯英文路径（重要）
copy .\DesktopPet-0.1.0-online-setup.exe C:\Temp\setup.exe

# 2) 双击或静默安装
C:\Temp\setup.exe
```

默认装到 `%LOCALAPPDATA%\Programs\DesktopPet`，不需要管理员权限。

**绿色版**：解压到任意**可写**目录（别放 `C:\Program Files`），双击 `DesktopPet.exe`。

---

## API 与隐私（必读）

要配置**三处 API**，互相独立，可以全云端也可以全本地：

| 用途 | 默认 | 换成纯本地 |
|---|---|---|
| **大脑**（聊天 / 人格 / 记忆 / 描述截图） | DeepSeek，OpenAI 兼容 | 填 ollama 之类的本地端点 |
| **语音转文字**（ASR） | 本地 faster-whisper `large-v3-turbo` | 默认就是本地，声音不出本机 |
| **文字转语音**（TTS） | `edge-tts`（**联网**） | 改成 `windows`（SAPI，离线） |

⚠️ **截图对话要求大脑支持识图**：DeepSeek 的文本模型不支持图片输入，
点截图会报错或回一句空白。要用这个功能，换成 qwen-vl / gpt-4o / gemini 这类视觉模型。

📦 **内置了一个本地检索模型** `bge-small-zh-v1.5`（ONNX，约 91 MB），
负责判断"哪段文字和当前话题相关"（记忆检索、语录挑选都靠它），
完全本地运行，不需要联网也不需要 API。

记忆库、日志、模型都在 `%APPDATA%\DesktopPet\`，没有云端同步。

## 第一次启动

1. 会弹一个空白宠物（此时还没有 API 密钥，它没法说话）
2. 右键宠物 → 设置（或托盘图标）→ **AI 与对话** → 填 DeepSeek 的 API key
   - 密钥存本地 `%APPDATA%\DesktopPet\credentials.json`，不进仓库、不上传
3. 想用语音：设置 → **语音** → 选「本地大模型（离线）」
   - 在线版会提示模型下载地址，可以把模型放到 `%APPDATA%\DesktopPet\pet-data\asr-models`
   - 离线版已经放好了，直接用

数据都在 `%APPDATA%\DesktopPet\`：记忆库、日志、模型、配置。卸载**不会**删它（故意的）。

---

## 这版有什么 / 没什么

**能用**：桌面壳、立绘播放（序列帧 + 视频动作）、聊天（流式、可打断）、气泡、
按住说话、语音识别与合成、设置页、托盘、打包安装。

**还没做**：**情绪系统没接完**（只有状态，不影响她说什么）、性格内容基本为空、
主动行为只有调度骨架、美术是借用原作者素材。

也就是说：**这是个能跑起来的壳**，别期待"她真的有人格"。

详见仓库 [README](https://github.com/asgaerg/desktop-pet#readme)。

---

## 素材版权

立绘与动画素材来自原作者项目 [MerZlin/dsh-pet-indesktop](https://github.com/MerZlin/dsh-pet-indesktop)
（基于 [PC2005-cloud/dsh-pet](https://github.com/PC2005-cloud/dsh-pet) 的移植）。
素材版权归原作者，本项目只借用。商用或再分发素材前请先确认原作者许可。

---

## 已知问题

- 未签名 → SmartScreen 提示（见上表）
- 安装包不能放含中文的路径
- 首次本地语音要下载 / 复制 1.5 GB
- 杀软可能误报 `pet.exe`
- 多显示器 / 高 DPI 下窗口定位还没打磨好
- 语音"说一半插话"只在部分链路可用
