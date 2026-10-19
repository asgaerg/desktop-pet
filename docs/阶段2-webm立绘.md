# 第二阶段：接入参考应用的 webm 素材

> 2026-10-01 凌晨。接在 `进度交接.md` 之后读。

## 参考资源

用户提供的现成桌宠 `dsh-pet-standalone-webm-chat.zip` → 已解包到 **`F:\ref-pet\`**（项目外）。
详细清单见 `F:\ref-pet\清单.md`。

**没有应用源码**（PyInstaller 编译进 exe），但有素材、配置和一份 DSH 桥接插件。

| 资产 | 位置 | 状态 |
|---|---|---|
| 106 个 webm 动画（角色 shenshen） | `_internal\assets\characters\shenshen\videos\` | **已复制进项目并启用** |
| 命中框 / 镜像规则 / 步幅标定 | 同目录三份 json | 已抄进我们的 `avatar.json` |
| DSH 桥接插件 | `_internal\integrations\dsh-pet-bridge\` | 0.2.0，85 KB，零依赖。**未接入** |
| 聊天背景图 9 张、音效 6 个 | `assets\chat\`、`assets\sounds\` | 未用 |
| 人格预设 / 菜单模板 / QSS | `_internal\pet\` | **菜单模板待用来抄结构** |

**已确认方向**：UI 保持我们现在的深色系，只抄参考应用的**结构与交互**；
右键菜单抄它的层级结构，**功能先不实现**。

## 这轮做完的：webm 立绘

### 目录

```
src/renderer/assets/avatar/
  default/            106 个 webm + avatar.json   ← 当前启用（视频）
  default-sprite/     序列帧那套（保留备用）
  default-placeholder/ 占位图
```

### 架构：门面模式

`sprite.ts` 读清单的 `kind` 字段：

- `"video"` → 把 `play / setMouth / setEmotion / start / stop / currentState / debug`
  全部委托给 `VideoPlayer`
- 否则 → 走原来的序列帧逻辑

**所以 `app.js` 一行都没改**（两种形态对外接口完全一致）。

`videoPlayer.ts` 由 esbuild **内联进 `sprite.js`**，没有独立产物 —— 所以
`build:renderer` 仍然只有一条 esbuild 命令，别去加第二条。

### 三个必须记住的坑

**① 视频没有 alpha 通道，背景是纯黑**

实测：像素格式 `yuv420p`（不是 `yuva420p`），canvas 读出角落像素 `[0,0,0,255]`。
必须**逐帧把黑像素的 alpha 置 0**。

黑底很干净，存在清晰阈值线：81.7% 是纯 `(0,0,0)`，而每通道 ≤6 的过渡像素
只占 **0.004%**（230400 个像素里 10 个），所以简单阈值就能抠干净、不伤角色暗部。

> 试过 `mix-blend-mode: screen`（黑色在 screen 下等于透明）：**不行** ——
> 会把角色深色部分与页面底色混合，角色变成一层淡影。

**② CSP 必须放开 `media-src 'self'`**

不写时回落到 `default-src 'none'`，`<video>` 全部被拒，立绘**静默**退化成
程序化占位宠物（只在控制台留线索）。实测报错原文：

```
Refused to load media from '...webm' because it violates the following Content
Security Policy directive: "default-src 'none'". Note that 'media-src' was not
explicitly set, so 'default-src' is used as a fallback
```

**③ 必须按角色范围裁剪缩放，不能整帧适配**

视频 640×360，而角色只占宽 34%（命中框 `[212,60,428,330]`）。
整帧适配时角色只画出 **81×101** 像素（画布 240×240），小得看不清；
按命中框外扩（横 35%、上 45%、下 12%，留给挥手跳跃和道具）后画到
**192×240** —— 实测画布占用像素从 9.9% 提到 29.3%。

### 状态映射

在 `avatar.json` 的 `animations` 里，每种状态给一个**路径数组**（随机挑一个）。

⚠️ **路径必须写全**（如 `idle/待机呼吸休闲`）—— 浏览器**无法枚举目录**，
只写文件名的话播放器没法知道它在哪个分类下，只能逐个试错加载。

```
idle     idle/待机呼吸休闲
drag     drag/被鼠标拖拽悬空反馈
curious  turn/东张西望、random/被吓一跳、random/蝴蝶蜜蜂环绕头顶开花
happy    click/点击回应-开心跃动、点击回应-元气挥手
shy      click/点击回应-害羞惊讶、点击回应-挠痒咯咯笑
angry    click/点击回应-傲娇生气
sleep    random/原地小憩沉眠、random/打瞌睡被惊醒
talk     random/是啊，吃什么、深度思考碎碎念、悠闲哼歌
work     random/工作状态-忙碌点按、思考冒泡、清点归档、原地踱步张望
workDone random/工作状态-雀跃庆祝
workTired random/工作状态-垂头叹气冒汗
```

别名会合并：`shy`/`angry` → `happy`，`turn` → `curious`，
`work` → `talk`，`workDone` → `happy`，`workTired` → `sleep`。

**`work` / `workDone` / `workTired` 是给 DSH agent 状态预留的**（还没接）。

清单里还登记了：
- `randomPool` —— 89 个按 吃/节日/玩/音乐/趣味/碎碎念 粗分，供以后做自主行为
- `moveStrides` —— 每圈地面位移（防走路滑步），来自原作者的两种方法交叉标定
- `noMirror` —— 6 个含文字的动画不能水平镜像（原素材 `text_clips.json`，逐帧人工核对得来）

这些**都还没接入播放逻辑**。

## 用户点单的下一个任务（还没做）

**① 右键菜单**：抄结构，来源
`F:\ref-pet\_internal\pet\menu_templates\modern-default-v1.json`。功能先不实现。

```
（顶部）动画头像
───
聊天 / 看屏幕
───
动画中心 / 角色 / 播放速度 / 大小 / 音乐（子菜单）
───
桌宠控制（子菜单：拖拽物理/禁止移动/置顶/开机自启/回角落/隐藏/生成/清除/成本/边缘探测）
───
工具与帮助（子菜单：余额/DSH/DeepSeek网页/检查更新/GitHub/夸克）
───
设置… / 切换模板
───
退出
```

**实现方式的关键判断**：它的 QSS 里**没有 QMenu 规则** → 用的是 **Qt 原生菜单**。
所以我们也该用 Electron 的 `Menu.popup()`（同为系统原生外观），
**不要**自己画 HTML 菜单。

**② 应用状态栏**：用户说的"应用的状态栏"具体指什么**还需确认**
（可能是宠物界面上的在线状态、任务栏按钮、或参考应用那种带小圆点的状态条）。

## 本轮新增的环境坑

| 坑 | 事实 |
|---|---|
| CSP `media-src` | 不写会回落到 `default-src 'none'`，视频全被拒 |
| webm 立绘 | **无 alpha、纯黑底**，必须逐帧抠图；`screen` 混合不行 |
| 中文路径 + `loadURL('data:')` | 加载不了中文路径的媒体，要用 `loadFile` |
| esbuild 内联 | `sprite.ts` import 的模块会被打进 `sprite.js`，别额外加构建步骤 |
