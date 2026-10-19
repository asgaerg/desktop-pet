# DesktopPet v0.1.0

桌面 AI 伴侣：透明置顶窗、会动的立绘、能打字也能说话，全部跑在本机。

> **这是一个壳。** 桌面部分能用，但情绪、性格、主动性、记忆检索都还没做完。

## 下载

| 文件 | 大小 | 说明 |
|---|---|---|
| `DesktopPet-0.1.0-online-setup.exe` | 278 MB | 安装包 |
| `DesktopPet-0.1.0-online-portable.zip` | 354 MB | 绿色包，解压即用 |

**不发离线版**：1.55 GB 的语音模型不跟着包走。要离线语音，
设置 → 语音 里有下载地址（`hf-mirror.com`），下完放进提示目录即可。

## ⚠️ 安全提示

| 提示 | 原因 | 处理 |
|---|---|---|
| SmartScreen「未知发布者」 | 没有代码签名证书 | 更多信息 → 仍要运行 |
| 杀软报 `pet.exe` 可疑 | PyInstaller 打的 Python 程序常被误报 | 加白名单，否则起不来 |
| 「不能打开要写入的文件」 | 安装包放在含中文的路径 | 先拷到 `C:\Temp` 再运行 |

校验：`Get-FileHash .\DesktopPet-0.1.0-online-setup.exe -Algorithm SHA256`，
对照仓库里的 `docs/SHA256SUMS.txt`。

## 安装

```powershell
copy .\DesktopPet-0.1.0-online-setup.exe C:\Temp\setup.exe
C:\Temp\setup.exe
```

默认装到 `%LOCALAPPDATA%\Programs\DesktopPet`，不需要管理员权限。
绿色版解压到**可写**目录（别放 `C:\Program Files`），双击 `DesktopPet.exe`。

## 第一次启动

1. 宠物是空白的 —— 还没填密钥，它没法说话
2. 右键 → 设置 → **AI 与对话** → 填 DeepSeek 的 API key
3. 想用语音：设置 → **语音** → 「本地大模型（离线）」

数据都在 `%APPDATA%\DesktopPet\`，卸载不删。

## API 与隐私

| 用途 | 默认 | 换本地 |
|---|---|---|
| **大脑**（聊天 / 人格 / 记忆 / 描述截图） | DeepSeek 4.1 Flash | ollama 等本地端点 |
| **语音转文字**（ASR） | 本地 faster-whisper `large-v3-turbo` | 默认就是本地 |
| **文字转语音**（TTS） | `edge-tts`（联网） | 改 `windows`（SAPI） |

**截图对话要求大脑支持识图**：DeepSeek 4.1 Flash 支持，开箱可用；
换成纯文本模型或不透传图片的第三方端点时会报错或回空白。

内置 `bge-small-zh-v1.5`（ONNX，91 MB，随包分发）做本地语义检索，不需要联网。

## 这版有什么

**能用**：桌面外壳、立绘播放、聊天（流式 / 可打断）、按住说话、语音识别与合成、
设置页、托盘、打包安装。

**还没做完**：记忆检索策略（最影响体验）、情绪（只有状态，不联动）、
性格（一段通用提示词）、主动性（只有调度骨架）、美术（借用原作者素材）。

## 谁做的

- 绝大部分代码：**DeepSeek 4.1 Flash**
- 部分前端：**GLM 5.3 Flash**
- 提需求、测试、补细节：**创作者**

## 第三方组件与素材

主要依赖：Electron、electron-builder、Python 3.12、faster-whisper / CTranslate2、
onnxruntime、bge-small-zh-v1.5、edge-tts（**GPL-3.0**）、jieba、NumPy、Pillow、SQLite。
联网搜索用独立的 **`websearch-mcp`**（Go 编写的 MCP 搜索服务，融合 360 / Bing）。

**立绘与动画素材来自** [MerZlin/dsh-pet-indesktop](https://github.com/MerZlin/dsh-pet-indesktop)
（基于 [PC2005-cloud/dsh-pet](https://github.com/PC2005-cloud/dsh-pet) 的移植）。
素材版权归原作者，本项目只借用。

完整清单见 README 的「开源组件与素材来源」。
