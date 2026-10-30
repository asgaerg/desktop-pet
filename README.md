# 桌面 AI 宠物（DesktopPet）

住在桌面上的 AI 伴侣：透明置顶窗、会动的立绘、能打字也能说话。

> 由 **DeepSeek V4.1 Flash 与创作者共同设计**。

**能用了，但还没做完**——性格和主动性还在补；记忆刚从"记不住具体的事"里走出来
（现在提炼的是能核对的具体事实，管理界面也能看、能改、能恢复），下一步是"用得自然"。

技术上是 **Electron 外壳 + Python sidecar** 双进程，全部跑在本机；
密钥、记忆库、模型都在本地，没有云端同步。

## 下载

[Releases](https://github.com/asgaerg/desktop-pet/releases) 里两个文件：

| 文件 | 大小 | 说明 |
|---|---|---|
| `DesktopPet-0.2.2-online-setup.exe` | 278 MB | 安装包 |
| `DesktopPet-0.2.2-online-portable.zip` | 355 MB | 绿色包，解压即用 |

**不发离线版**：1.55 GB 的语音模型不跟着包走。要离线语音，
设置 → 语音 里有下载地址（国内镜像 `hf-mirror.com`），下完放进提示的目录即可。

> ⚠️ **一定会遇到安全提示**：exe 没有代码签名，SmartScreen 会报「未知发布者」→
> 点「更多信息」→「仍要运行」；杀软可能误报 `resources\pet\pet.exe`（PyInstaller 打的 Python）→ 加白名单。
> **安装包要放纯英文路径**（如 `C:\Temp`）再运行，中文路径会让 NSIS 失败。
> 校验：`Get-FileHash .\DesktopPet-0.2.2-online-setup.exe -Algorithm SHA256`，
> 对照 [SHA256SUMS.txt](docs/SHA256SUMS.txt)。

## API 与隐私

三处 API，各自独立，可全云端也可全本地：

| 用途 | 默认 | 换本地 |
|---|---|---|
| **大脑**：聊天 / 人格 / 记忆整理 / 描述截图 | DeepSeek 4.1 Flash（OpenAI 兼容） | 填 ollama 之类本地端点 |
| **语音转文字**（ASR） | 本地 faster-whisper `large-v3-turbo` | 默认就是本地，声音不出本机 |
| **文字转语音**（TTS） | `edge-tts`（**联网**） | 改 `windows`（SAPI）即离线 |
| **联网搜索**（可选，默认关） | DeepSeek [Search API](https://api-docs.deepseek.com/zh-cn/guides/search/)（**按次计费**，只有配 DeepSeek 密钥才可用） | 没有密钥时自动用本地 MCP 搜索（零密钥） |

> ⚠️ 联网搜索的计费是**按请求次数**（5 元 / 1000 次成功请求，失败不计），
> 不是按 token —— 这是本项目唯一一处按次花钱的功能。
> 不想花这个钱：不配 DeepSeek 密钥即可（快速档会自动改用本地 MCP 搜索），
> 或者把联网搜索整档关掉。

**截图对话要求大脑支持识图**：DeepSeek 4.1 Flash 支持，开箱可用；
换成纯文本模型或不透传图片的第三方端点时，点截图会报错或回空白。

**内置一个本地检索模型** `bge-small-zh-v1.5`（ONNX，约 91 MB，随安装包分发），
负责判断"哪段文字和当前话题相关"（记忆检索、语录挑选都靠它），
完全本地运行，不需要联网也不需要 API。

## 现在做到哪一步

**能用**：桌面外壳、立绘播放（序列帧 + 视频动作）、聊天（流式、可打断）、
气泡、按住说话、语音识别与合成（**音量可调**）、设置页、托盘、打包安装、Python 侧自检 137/137。

**半成品**：

- **记忆检索层** ❗还没做完：能存能查（向量 + FTS + 分层 + 备份 + 管理窗口），
  但**"该想起什么"的策略很粗**——经常想不起相关的，或者想起来的不对，
  这是目前最影响体验的一块
- **自言自语**：语料 18206 条是真的，挑法就是"按类别权重 + 避开最近念过的"
- **屏幕感知**：全屏检测、截图、隐私闸门都在跑，"看懂屏幕"只是把文字丢给模型
- **联网搜索**：三档（精准 / 快速 / 关）。快速档 2026-10-19 起接了
  [DeepSeek 官方 Search 接口](https://api-docs.deepseek.com/zh-cn/guides/search/)——
  **只有配了 DeepSeek API 密钥才能用**（按次计费：5 元/1000 次），
  没密钥时自动退回本地 MCP 搜索；"结果怎么用进对话"仍在打磨

**没做**：

- **情绪**：三维状态（精力 / 心情 / 亲近度）已经联动**表情**和**语气**；
  但"因此主动做点什么"还很浅，仍算不上人格
- **性格**：只有一段通用提示词，换个提示词就是另一个人
- **主动性**：不会因为"想你了"来找你，只会按计时器按概率开口
- **美术**：全借用原作者素材

## 待完善（TODO）

- [ ] **把记忆检索策略做对**：检索准确率现在是最大短板
- [ ] **把情绪的影响做深**：表情和语气已经跟上，还该影响她说什么、主动做什么
- [ ] **语言表达层**：和情绪层**并列**的一组可调旋钮（修辞 / 古诗成语 / 词汇量 /
      说话温度 / 关心程度），而不是硬写一段人设 —— 硬写等于用静态文本对抗模型自身的表现力。
      设计见 `docs/进度交接.md` §一百八十四
- [ ] **最高条例**：四条**不分优先级**的思考约束（自我调节 / 对陌生知识有新鲜感 /
      主动找有趣的事 / **不一味附和、随时保留其他可能**），写进提示词，
      **用户可自行编辑**；设置页会明确警告：它的重要性不亚于定义她的感受与思维方式
- [ ] **真独立**（长期）：认识事物、从记忆里自我强化、学习，
      最终**不再需要主观层脚手架**也能拟人 —— 所以主观层必须可拆
- [ ] **代码签名**：现在未签名，Windows 必弹 SmartScreen。
      ⚠️ GPG 签名（提交/标签用）管不了这个 —— 要的是 Authenticode 证书。
      路线、成本与坑见 `docs/进度交接.md` §一百八十三
- [ ] **首次启动引导**：第一次打开该告诉用户去哪填密钥
- [ ] **GPU 版 sidecar**：现在 CPU 跑 `large-v3-turbo`，一句话 1~3 秒
- [ ] **给它看视频**：DeepSeek 只吃图片，得先拆帧 + 音轨转写（方案见 `docs/进度交接.md` §一百六十五，暂缓）
- [ ] 看小说 / 看视频自动收集喜欢的句子（表结构已预留）
- [ ] 多显示器 + 高 DPI 窗口定位
- [ ] 记忆库导入导出
- [ ] 语音"说一半插话"只在部分链路可用
- [ ] 安装包中文路径问题
- [ ] Linux / macOS 没试过

## 已知问题（会踩到的坑）

1. **未签名**：SmartScreen 会拦一下，点"更多信息 → 仍要运行"。
2. **安装包要放在纯英文路径里运行**（例如 `C:\Temp`）。放 `F:\项目\` 这种含中文的路径，
   NSIS 会静默失败，只弹一句"不能打开要写入的文件"。
3. 在线版**不带**语音模型，第一次用本地识别会自动下 1.5 GB（走 `hf-mirror` 镜像）。
   设置 → 语音 里能看到下载地址和该放的目录。
4. 杀软可能误报 `resources\pet\pet.exe`（PyInstaller 打的），加白名单即可。
5. 记忆库、日志、模型都在 `%APPDATA%\DesktopPet`，**卸载不会删**（故意的，数据是你的）。
6. **截图对话依赖识图能力**：默认的 DeepSeek 4.1 Flash 支持识图，正常可用；
   但换成纯文本模型、或用不透传图片的第三方端点时，点截图会报错或回空白。
7. **记忆检索不准**：它经常想不起该想起的事，或者想起来的不对——这是目前最影响体验的一块，
   见上面「现在做到哪一步」。
8. **开 VPN 不会让对话变慢**（2026-10-19 修好）：程序会按域名分流，
   发往 DeepSeek / 硅基流动的请求**直连**（实测从 11 秒降到 0.9 秒），
   其余请求照旧走系统代理。想改名单见 `persona.yaml` 的 `net.direct_hosts`。
9. **偶尔"它不理我"**（2026-10-19 修好）：模型偶发只思考、正文 0 字，
   现在会**自动关掉思考重试一次**；万一网络断了，气泡里会说一句
   「我这会儿连不上模型」，而不是留一片空白（见 `docs/进度交接.md` §一百七十）。

---

## 谁做的

**由 DeepSeek V4.1 Flash 与创作者共同设计。**

- **设计与实现：DeepSeek V4.1 Flash** —— 架构、绝大部分代码，以及本文件里那些
  "为什么这么做"的注释、`docs/进度交接.md` 里一百七十多节的排查记录
- **部分前端：GLM 5.3 Flash**
- **提出需求、定方向、测试、逐条验收：创作者** —— 每个功能的取舍都是他拍板的，
  包括"不要什么"（不要 5 秒自动收回探头、不要全屏自动靠边、不要模糊的记忆）

写得快是真的，坑多也是真的。欢迎提 issue / PR。

## 开源组件与素材来源

### 第三方组件

项目本身没有再额外造轮子，主要依赖这些开源项目：

| 组件 | 用在哪 | 许可 |
|---|---|---|
| [Electron](https://www.electronjs.org/) / [electron-builder](https://www.electron.build/) | 桌面外壳与打包 | MIT |
| Python 3.12 / `asyncio` | 大脑运行时 | PSF |
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper) + CTranslate2 | 本地语音识别 | MIT |
| [onnxruntime](https://github.com/microsoft/onnxruntime) | 本地嵌入模型推理 | MIT |
| [bge-small-zh-v1.5](https://huggingface.co/BAAI/bge-small-zh-v1.5)（BAAI） | 语义检索模型 | MIT |
| [edge-tts](https://github.com/rany2/edge-tts) | 在线语音合成（默认 TTS） | **GPL-3.0** ⚠️ |
| [jieba](https://github.com/fxsjy/jieba) | 中文分词 | MIT |
| NumPy / Pillow | 数值计算、截图编码 | BSD / HPND |
| SQLite | 记忆库 | Public Domain |
| `libsimple`（第三方语音运行时） | 本地语音识别引擎（未随仓库分发） | 见其发布页 |
| `websearch-mcp`（**搜索插件**，Go 编写） | 本地搜索后端（**没有 DeepSeek 密钥时的兜底**）：融合 360 / Bing，带相关性打分与多样性去重 | 第三方，二进制未随仓库分发 |
| [`free-search-mcp`](https://github.com/sweetcornna/free-search-mcp)（Python 包） | 「精准搜索」后端：多引擎融合（68 个免 key 来源），约 10~20 秒，命中质量优于快速档 | MIT，未随仓库分发 |
| [DeepSeek Search API](https://api-docs.deepseek.com/zh-cn/guides/search/) | 联网搜索后端（快速档首选，按次计费） | 在线服务 |

⚠️ `edge-tts` 是 **GPL-3.0**。本项目只是把它当运行时依赖调用（没有改它的代码、
也没有链接进我们自己的二进制），但如果要**分发**整套程序，请自行确认
GPL 的合规要求（保留声明、提供源码途径），或者把默认 TTS 换成 `windows`（SAPI）。

### 素材

**立绘与动画不是本项目原创**，来自原作者的项目：

- [MerZlin/dsh-pet-indesktop](https://github.com/MerZlin/dsh-pet-indesktop)
  —— 该仓库说明里写的是基于 [PC2005-cloud/dsh-pet](https://github.com/PC2005-cloud/dsh-pet) 的移植

序列帧贴图与视频动作（`apps/desktop/src/renderer/assets/avatar/` 下的
`sheet.png` / `.webm`）沿用该项目的素材，我们只重写了播放与状态机。

**素材版权归原作者所有。** 要商用或再分发素材，请先去原仓库确认许可。

---

## 设计细节（为什么这么做）

### 长期记忆（P2）怎么在零依赖下做到

### 长期记忆（P2）怎么在零依赖下做到

长期记忆的关键难点不是存储，而是**检索**——而本机没有任何嵌入模型
（没有 sentence-transformers / torch / onnxruntime，下载权重也不现实）。
所以这里用经典的**哈希技巧**自己造向量空间：

```
文本 → 按字符类型统一分词 → 四层特征（整串 / 整词 / 词内单字 / 字符 n-gram）
     → 带符号哈希到 256 维 → L2 归一化 → 余弦相似度 + 关键词提权
```

**带符号**是关键：不带符号的话所有特征只做加法，任意两段文本的向量都会互相靠近，
相似度就失去区分力。**关键词提权**也是必需的：查询"Rust 学得怎么样"里的
"学得怎么样了"会在向量空间贡献大量噪声，把真正的信号稀释掉（实测余弦只有 0.15），
而"Rust 同时出现在两处"本身是极强证据。

诚实说明它的局限：它匹配**字面重合**而不是语义，所以"我在写 Python"与
"最近在折腾代码"匹配不到。要语义匹配得换真模型——`Embedder` 是接口，
换实现后把旧向量重算一遍即可，记忆库与检索逻辑不用改。

隐私上做了三层：写入前**敏感度过滤**（密钥/密码/证件号一律不落盘）、
近似重复**合并**而不是攒副本、容量满时按"重要度 + 使用次数 − 时间衰减"淘汰。
记忆文件在 `~/.desktop-pet/memory.jsonl`——它属于用户而不是项目目录。

界面底部会显示**它记住了多少条**，旁边有个「忘掉」按钮可以一键清空
（会二次确认，并真的从磁盘删除）。记忆是看不见的东西——看不见用户既不会信任它，
也不会想到去清理，所以把它做成可见的、可控的。

### 语音（P3）为什么把音频 I/O 放在前端

本机**一个音频库都没有**（sounddevice / pyaudio / numpy 之外的音频栈全无），
而 Electron 渲染层的 Web Audio API 原生就能采麦克风与播放。所以分工是：

```
Electron 渲染层   采麦克风 PCM、重采样到 16kHz、推给主进程；解码并播放宠物语音
      ↕ 二进制帧（不走 JSON，避免 base64 膨胀三分之一）
Python sidecar    端点检测、识别、合成、打断判定、状态机
```

好处是 Python 侧只处理 **PCM 字节**，纯标准库就能算（`audioop`），
于是语音里最容易出问题的部分——**时序**——可以用合成音频完整离线验证：

| 行为 | 断言 |
|---|---|
| 静音不误触发、人声能识别 | 环境噪声不被当成说话（自适应噪声底） |
| 换气不被当成说完 | 800ms 说 + 200ms 停 + 700ms 说 → 只切出 **1 句** |
| 两句能正确分开 | 说-停-说-停 → 切出 **2 句** |
| 咳嗽/敲键盘被丢弃 | 60ms 声响 → **0 句** |
| 超长说话被截断 | 3s 连续语音在 1s 处截断并标记 `max_length` |
| 用户插话能打断 | 宠物说话时用户开口 → 发 `user.interrupt` + 停止播放 |
| 插话可关闭 | `allow_barge_in=false` 时不再抢话（麦克风差的环境可用） |
| 口型包络是真实算出来的 | 响段 0.42 / 静段 0.00，不是随机数 |

**几个刻意的取值**（都在 `config/persona.yaml` 里可调）：

- `end_silence_ms: 500` —— 说完了判定所需的静音时长。**这是最影响语音体验的一个数**：
  太小会把句子切碎（换气就以为说完了），太大则每句都要多等。
- `barge_in_frames: 5`（100ms）—— 判定插话比判定起说更严格，避免宠物自己的尾音打断自己。
- 麦克风采集**关掉了浏览器的降噪与自动增益**：那些是为通话设计的，会把语音动态压平，
  反而让能量法端点检测更难判断"用户到底有没有在说话"。
- 句尾静音会被**剪掉**再送识别：端点检测为了确认"真的说完了"必须多等 500ms，
  把这 500ms 静音交给 ASR 只会让模型更容易产生幻觉文字。

语音默认是 `asr.provider: mock`——**它能跑通整条链路时序，但不会真的听懂你说话**。
要真识别，改成 `openai` 并配密钥（任何 OpenAI 兼容的 `/audio/transcriptions`），
或装 `faster-whisper` 走本地。`tts.provider: none` 表示暂时不出声，只显示文字。

### 屏幕感知（P4）怎么看、怎么不怕、怎么不烧钱

```
采集层（PillowCapturer，依赖平台）     抓像素 → 8x8 灰度网格 + 按需编码图像 + 窗口标题
    ↓
分析层（纯函数，零平台依赖）           隐私闸门 → 变化检测 → 新鲜度评分
    ↓
内容层（可插拔，按成本从低到高）       L1 窗口标题 → L2 本地 OCR → L3 视觉模型
    ↓
两个通道分开走：
  · 信号通道 novelty  → 自主调度器决定"要不要开口"
  · 内容通道 抽取文本 → 大脑决定"说什么"
```

**不烧钱靠的是变化检测，不是省着用模型。** 实测两个数字：

- 合成帧测试：100 帧里只调用视觉模型约 10 次（不判变化则需 100 次），**省 90%**；
- 真实屏幕：截图压到长边 768px 后，token 量级从约 **2765 降到约 440**（一个数量级）。

内容链默认顺序是 `title → ocr → vlm`，**凑够内容就停**。窗口标题常常已经够用
（"main.py - VS Code"说明了很多），所以默认状态下完全不花钱。

**隐私是硬约束，排在一切分析之前**。命中黑名单的窗口连哈希都不算、连内容抽取
都不进（有测试断言抽取器 0 次调用），也**不会刷新新鲜度**——否则你打开密码管理器
反而会让它更想开口。默认内置 16 条保守规则，不需要你配。

想打开看屏幕，把 [config/persona.yaml](config/persona.yaml) 里
`providers.vision.provider` 从 `none` 改成 `pillow`。想要"真看懂画面"，
再填一个多模态模型名（`vision.model`）与对应密钥环境变量即可；
只装 tesseract 的话走本地 OCR，同样免费。

### 「活泼度」到底是怎么工作的

它不是"随机 30% 概率说话"，而是一条**可解释、可预测**的曲线：

```
意愿分 = 0.9×屏幕新鲜度×screen_watch_prob
       + 0.75×空闲程度×idle_chatter_rate
       + 0.20×亲近度
       ────────────────────────────────────  再乘以 (1 - 冷却惩罚)

意愿分 ≥ 阈值  →  开口
阈值 = 1.05（活泼度 0）… 0.22（活泼度 1），线性反向映射
```

几条刻意的设计：

- **抑制优先于意愿**。免打扰、静音时段、专注保护任一命中，意愿直接归零；
- **冷却不可被活泼度覆盖**。刚交互过 45 秒内、刚说完 60 秒内强制闭嘴——
  宠物最讨人厌的失败模式不是"不说话"，而是"说个不停"；
- **决策里没有随机数**。拖滑杆时行为必须可预期，想调"偶尔安静一下"调的是阈值曲线，
  不是掷骰子；
- **`interrupt_prob` 不参与自主发言**。它是"用户正在说话时插嘴"的倾向，
  属于 P3 语音阶段，用它当主动开口的闸门是语义错位。

配置在 [config/persona.yaml](config/persona.yaml) 的 `autonomy` 段，全部支持热更新。

---

## 快速开始

> 动手前先看一眼 [docs/开发约定.md](docs/开发约定.md)：**平时只改文件、不提交；每次打包前才按版本统一提交一次。**

### 1. 安装依赖

```powershell
pnpm install
```

> **装不动 Electron？** 它的运行时（约 108MB）放在 GitHub Releases，国内常被限速到
> 0.04 MB/s（下完要 45 分钟）。改用镜像即可，实测 34 MB/s：
>
> ```powershell
> $env:ELECTRON_MIRROR = "https://registry.npmmirror.com/-/binary/electron/"
> pnpm install
> ```

### 2. 跑大脑自检（不需要界面、不需要 API 密钥）

```powershell
cd apps/brain
python -m pet --selftest
```

它会在本地起一个 WebSocket 服务、用一个内置客户端跑完整的
`hello → ready → user.text → 流式回复 → 热更新` 往返，并断言协议、配置、
总线隔离、插件容错、提供者降级等 14 项。**改完协议或总线务必跑一遍。**

### 3. 构建并启动界面

```powershell
pnpm install          # 已在上一步做过
pnpm dev              # 自动构建前端产物，然后启动（带 devtools）
```

> `pnpm dev` 会**先构建再启动**（`--build`）。这一步顺带解决了一个隐蔽问题：
> 渲染层的 `sprite.ts` 必须先编译成 `sprite.js` 才能被浏览器加载，忘了构建的
> 症状是"立绘完全不显示"，而控制台里只有一行模块加载失败。让它自动构建，
> 这类问题就不存在了。

> **启动器会做前置检查**，失败时给的是可读的中文提示而不是一堆语法错误：
> Node 版本、Electron 运行时是否存在、pnpm 能不能跑、Python 解释器能否找到。

#### Node 版本太旧怎么办

PATH 里的 `node` 如果是 v14 之类，构建工具会以各种看不懂的方式失败
（`??`、`?.` 语法错误，或者 pnpm 报 "requires at least Node.js v18.12"）。
本项目用的是 v18.12.0，已经够用。

**启动器会自己找可用的新版 Node**，顺序是：

1. 环境变量 `PET_NODE`
2. 项目根的 `.pet-node` 文件（一行路径即可）
3. **nvm-windows 已装的其他版本**（自动挑够新的）
4. 官方安装目录

第 3 条是关键：nvm-windows 的 `nvm use` 要改盘符根目录的**符号链接**，
因此**需要管理员权限**。但 nvm 已经装好的各版本目录本身就能直接执行，
把那个目录顶到 PATH 最前就够了 —— **不用动任何系统级设置**。
启动器会自动这么做，并打印一行说明：

```
[launch] 当前 Node 14.15.0 偏旧，已改用 v18.12.0（D:\nvm\v18.12.0\node.exe）
[launch] 只为本次启动生效，没有改动你的系统设置
```

**唯一需要你手动做一次的是 pnpm**：它的安装位置绑定在某个 Node 版本下，
换 Node 之后要在新版本下重装一次 —— 大概十几秒，且**不需要管理员权限**：

```powershell
# 让 PATH 用上 v18，再在新版本下装 pnpm
$env:PATH = "D:\nvm\v18.12.0;$env:PATH"
npm install -g pnpm@10.6.1
```

之后 `pnpm dev` 就能用了。

> ⚠️ **注意顺序**：`pnpm` 命令本身也是被 PATH 里的 Node 拉起来的。
> 如果 Node 还是 v14，`pnpm dev` 会在**启动器运行之前**就退出，报的是一句
> 与项目毫无关系的 `This version of pnpm requires at least Node.js v18.12`。
> 所以要么先按上面装好 pnpm，要么直接用下面的方式启动。
>
> **不想装 pnpm 也行** —— 构建步骤已改为直接调用 esbuild 二进制
> （它不依赖 Node、也不依赖 pnpm）：
>
> ```powershell
> & "D:\nvm\v18.12.0\node.exe" scripts\launch.mjs --dev --build
> ```
>
> 这就是把上一行的 `pnpm dev` 展开写了。项目根的 `dev.cmd` 已经做好了这个包装，
> 直接敲 `.\dev.cmd` 即可。

> 如果更愿意走 nvm 的标准流程：**管理员** PowerShell 里 `nvm use 18.12.0`，
> 再同样重装一次 pnpm。效果一样，只是需要提权。

> **想跑测试套件需要 Node 20+**（测试代码用了 `??`、`?.` 等较新语法）。
> `pnpm dev` 只需要 Node 18+，所以**构建和运行不受影响**：
> 构建脚本里只留了构建与轻量检查，测试被拆到独立的 `pnpm test`。

> **必须用 `pnpm dev` / `pnpm start` 启动，不要直接双击 electron.exe。**
> 它们走 `scripts/launch.mjs`，会在启动前清掉环境变量 `ELECTRON_RUN_AS_NODE`。
> 若被设置（DSH 这类 Electron 宿主会往下传），Electron 会**完全不初始化应用层**：
> `require('electron')` 退化成路径字符串，app/window/tray 全是 undefined，
> 而 `process.versions.electron` 依然显示正常版本号，报错堆栈全在 Node 内部 ——
> 极难定位。启动器用 `delete env.XXX` 清除（**不能用置空**，空串会被 Chromium
> 当成"已设置"，实测无效）。
>
> **Python 解释器**：按 `PET_PYTHON` → `DSH_PYTHON` → `PYTHON` → 项目内
> `.venv`/`venv` → PATH 的顺序查找。启动器会自动把托管环境的 Python 路径转交过去，
> 所以代码里没有硬编码任何机器专属路径。

### 4. 换成真实模型（可选）

默认用内置 mock 大脑，**开箱即用、零密钥**，回复由本地模板 + 活泼度 + 情绪生成。
接真实模型只改配置，不动代码：

```powershell
$env:DEEPSEEK_API_KEY = "sk-..."     # 密钥只放环境变量，不写进配置文件
```

然后把 [config/persona.yaml](config/persona.yaml) 里的 `providers.brain.provider`
改成 `openai`。任何 OpenAI 兼容的 `/chat/completions` 都能用
（DeepSeek / 通义 / Kimi / 本地 llama.cpp / Ollama / vLLM）。

---

## 使用

| 操作 | 方式 |
|---|---|
| 聊天 | 底部输入框，Enter 发送 |
| **语音对话** | **点「语音」按钮开麦**（状态指示灯会显示在听 / 在想 / 在说） |
| **打断宠物** | 它说话时你直接开口（自动停下）；打字时按 Esc |
| **戳一下让它说话** | 左键单击宠物本体（跳过意愿与冷却，立刻主动说一句） |
| 移动宠物 | 拖拽宠物本体（位移超过 6px 就只移动，不会误触发"戳一下"） |
| 调活泼度 | 输入栏上方的滑杆（**热更新，立刻生效，不用重启**） |
| 免打扰 | 输入栏的「免打扰」按钮或托盘菜单（默认 30 分钟） |
| 显示/隐藏 | `Ctrl+Alt+P` 或托盘菜单 |
| 聚焦输入框 | `Ctrl+Alt+L` |
| 免打扰开关 | `Ctrl+Alt+Q` |
| 退出 | 托盘菜单（窗口没有关闭按钮，这是有意的） |

底部那行小字会实时显示它**为什么现在不说话**（免打扰 / 静音时段 / 专注保护 /
正在攒意愿 / 上次开口是什么时候），省掉"它怎么不吭声了"的猜测。

**点击穿透**：宠物窗口平时把鼠标事件透给下面的应用，只有指针落在宠物或输入栏上
才拦截。所以它不会挡住你点桌面图标。

---

## 目录结构

```
apps/
  desktop/                    Electron 外壳
    src/main/                 主进程：窗口 / 托盘 / socket / sidecar 管理
      bootstrap.ts            启动前的环境清理（见上文 ELECTRON_RUN_AS_NODE）
      index.ts                编排：把窗口、托盘、socket、sidecar 接起来
      petWindow.ts            透明置顶窗 + 点击穿透
      petSocket.ts            零依赖 WebSocket 客户端（本机环回）
      sidecar.ts              Python 进程生命周期（就绪探测 / 退避重启 / 平滑退出）
      tray.ts                 托盘菜单 + 用代码画的图标
    src/preload/              安全桥（白名单通道，contextIsolation + sandbox）
    src/renderer/             立绘与界面
      sprite.ts               序列帧播放器 + 程序化占位宠物（打包成 sprite.js）
      voice-io.js             麦克风采集 / 重采样 / 宠物语音播放
      app.js                  协议事件 -> 表现
    src/renderer/assets/avatar/default/   占位素材（可用 pnpm avatar 重新生成）
    tools/make-placeholder-avatar.mjs     生成占位序列帧（零依赖，自己写 PNG 编码）
  brain/                      Python 大脑（零第三方依赖；仅截屏可选依赖 Pillow）
    pet/ws_server.py          自己实现的 RFC6455 WebSocket 服务端
    pet/bus.py                事件总线（模块间唯一耦合点）
    pet/config.py             配置加载 / 校验 / 热更新 + YAML 子集解析器
    pet/autonomy.py           活泼度调度器：意愿评分 / 冷却 / 免打扰时段解析
    pet/vision.py             感知分析层：隐私闸门 · 变化检测 · 新鲜度（纯函数，可离线测）
    pet/vision_capture.py     感知采集层：Windows 截屏 + 图像编码 + 窗口标题（薄适配器）
    pet/content.py            内容层：标题 / 本地 OCR / 视觉模型 三级可插拔抽取链
    pet/vision_test.py        真实屏幕自检
    pet/voice.py              语音链路：端点检测 · 打断 · 状态机 · ASR/TTS 引擎
    pet/embedding.py          零依赖文本嵌入（哈希技巧 + 关键词提权）
    pet/memory.py             长期记忆：条库 · 检索 · 去重 · 画像 · 持久化
    pet/brains/               生成提供者：mock（本地）+ openai_compat（流式 SSE）
    pet/gateway.py            连接层：协议 <-> 总线 <-> 大脑 <-> 感知
    pet/plugins.py            插件接口（后续功能的挂载点）
    pet/selftest.py           离线自检（30 项，含真实 WebSocket 往返）
packages/protocol/            通信协议唯一来源（protocol.json + TS 类型）
config/persona.yaml           人格、活泼度、自主行为配置
docs/桌面宠物方案.md           完整技术方案与路线图
scripts/launch.mjs            确定性启动器
```

### 构建与校验命令

```powershell
pnpm build                      # 主进程 + preload + 渲染层，并跑渲染层语法门禁
pnpm avatar                     # 重新生成占位序列帧素材
cd apps/brain
python -m pet --selftest        # 大脑自检：30 项，不需要界面和密钥
python -m pet --vision-test     # 对着真实屏幕验证感知采集（需要桌面会话）
python -m pet --check-config    # 打印实际生效的配置
```

渲染层有两道门禁，`pnpm build` 都会自动跑。为什么必须有：`renderer/` 下的文件直接
喂给 Chromium，而这里的界面**无法在开发环境里目视验证**，所以只能靠门禁兜底。

**① 静态契约检查**（`check:renderer`）——每一条都对应一类真实踩过的缺陷：

| 检查项 | 防的是什么 |
|---|---|
| 每个 `.js` 能做纯语法编译 | 混入 TypeScript 语法会让模块整体解析失败（立绘曾因此从未加载） |
| 元素 id 引用都在 index.html 里存在 | `$('x')` 取到 null，随后访问属性报错 |
| 订阅通道都在 preload 白名单内 | `pet.on()` 抛异常，而它在模块顶层 —— **整个脚本不执行** |
| 调用的 `pet.*` 方法都已暴露 | 调了个 preload 没实现的方法 |
| `els.*` 属性都已定义、文件无 BOM | 属性名拼错；BOM 会让 ESM 加载器行为异常 |

**② 运行时测试**（`test:renderer`，53 项）——在 Node 里搭一个假 DOM，
**把真实的 `app.js` 与 `sprite.js` 跑起来**，然后推动协议事件、检查真实状态：

- 流式对话：气泡累积、思考态、结束后移除流式标记；
- **串台保护**：迟到/错配的 `say.delta` 必须被丢弃（否则两句回复会混在一起）；
- 立绘状态机：素材真的加载（`mode === 'sprite'`）、帧真的在推进、
  发言切 `talk`、结束后回 `idle`、`blink` 播完自动回 `idle`、
  低精力进 `sleep` 并恢复；
- 口型：说话时被驱动到合法的非零值、不说话时归零；
- 输入去空白、空输入不发送、滑杆回显、免打扰、语音状态灯、记忆显示与清空、
  自主行为状态翻译。

这一类问题**静态检查一个都抓不到**——它们语法完全正确，只在运行时才暴露。

**③ 主进程测试**（`test:main`，39 项）——覆盖三块**从未被执行过**的逻辑：

- **`petSocket.ts`**：手写的 WebSocket 客户端。测试起一个**真 socket** 服务端，
  验证握手、帧掩码、大于 125 字节的长度分支、**跨 TCP 分片的帧拼接**、ping→pong。
  这是全仓库最容易写错的部分，错了的表现是"连不上/偶尔断"。
- **`audioPairer.ts`**：语音"元信息 + 二进制帧"的配对。一个 WebSocket 上文本与
  二进制是两条独立通道，必须靠序号配对——**这里曾有一个真 bug**：元信息先到时
  序号会错位，导致配对永远不成功、语音静默丢失。
- **`sidecar.ts`**：Python 解释器查找顺序、以及退避重启的窗口限流。

**④ 真实联调**（`test:e2e`，34 项）——这是唯一一条**真正跨进程**的测试：
拉起真实的 Python sidecar，用真实的 `petSocket` 连上去，逐条校验字节往返。

为什么它不可替代：其余测试都是"某一端 + 打桩的另一端"，因此
**两侧各自自洽但彼此不匹配**这类问题永远测不出来。它验证的包括：
`ready` 携带全部五个子系统状态、流式增量**拼起来与最终文本完全一致**、
配置越界值被夹取、非法输入被拒后连接仍可用、免打扰真的拦得住、
以及**语音链路整条走通**（推静音不会被误识别，推正弦波"说话"能触发识别与回复）。

> 受限环境里 Node 不能起带管道的子进程（`spawn EPERM`），此时可以先用
> 外部终端跑 `python -m pet --port 8931`，再设 `PET_E2E_PORT=8931` 运行测试。

**⑤ 协议一致性**（`check:protocol`，10 项）——协议有三份"同一份定义的副本"：
`protocol.json`（人读的单一来源）、TS 类型、Python 常量。任一处不同步，
症状都是最难查的那种：前端静默丢弃一条消息，或 sidecar 拒收一条合法请求，
**而两侧的测试都会通过**（各自自洽）。所以这里做跨文件断言：
上行白名单覆盖、下行白名单覆盖、gateway 是否登记、前端是否处理、
情绪枚举一致、版本号三处一致。

> **需要 Node 20+。** 这几套检查都会先检查版本并给出明确提示，而不是抛一堆
> 不知所云的语法错误。PATH 里的 `node` 若太旧（本机是 v14），请用较新的 Node
> 运行 `pnpm build`。注意：`pnpm dev` 本身不受影响（Electron 自带 Node）。

---

## 设计上的几条硬规则

1. **模块之间不直接调用，一切过事件总线。** 加功能 = 订阅事件 + 发布事件，主循环零改动。
2. **Python sidecar 零第三方依赖。** WebSocket 服务端、YAML 解析、HTTP 流式客户端都是自己写的。
   换来的是启动快、打包不炸、换机器不用配环境。
3. **不信任另一个进程。** 上行消息做严格校验，类型白名单 + 长度上限，坏消息只丢弃不回崩。
4. **不静默失败。** 配置写错（如 `overall: 很活泼`）直接报错并指出行号，
   不悄悄用默认值——那会让用户改了配置却毫无反应，比崩溃更难查。
5. **流式是硬要求。** LLM 一出字就推给界面，首字 <1s，端到端 <2s。超过 3s 宠物感就没了。
6. **密钥只放环境变量。** 配置文件里只写变量名。

---

## 再加一个功能（可扩展性说明）

写一个插件即可，不用改主循环：

```python
# apps/brain/pet/plugins_examples/pomodoro.py
from ..plugins import Plugin, ToolSpec

class Pomodoro(Plugin):
    name = "pomodoro"
    description = "每 25 分钟提醒一次"

    def subscribe(self) -> list[str]:
        return ["user.text"]          # 只听自己关心的事件

    async def on_event(self, event) -> None:
        if "开始专注" in event.payload.get("text", ""):
            await self.ctx.say("好，25 分钟后叫你")
            self.ctx.every(25 * 60, self._ring)

    async def _ring(self) -> None:
        await self.ctx.say("时间到啦，起来动动")

    def tools(self) -> list[ToolSpec]:
        return [ToolSpec(name="pomodoro.start", description="开始一个番茄钟")]
```

插件异常被总线隔离：一个插件崩了不会让宠物哑巴（自检里有一项专门验证这条）。
