# 用 Windows 自带的 SAPI 把文字合成为语音（离线、零延迟、免费）。
#
# --------------------------------------------------------------------------- #
# ⚠️ 这个脚本必须带 UTF-8 BOM
# --------------------------------------------------------------------------- #
#
# PowerShell 5.1 对**没有 BOM** 的 .ps1 按系统 ANSI 代码页（这台机器是 GBK）
# 解析，中文注释会变成乱码，进而**语法错误、脚本启动即退出**。
#
# 这条坑在 2026-10-08 真的咬过我：我给 `probe-key.ps1` 加诊断日志时用编辑器
# 改文件、把 BOM 弄丢了，于是那个监视器"静默失效"，我花了很久去追一个
# **自己制造的假故障**（详见 docs 第五十节）。
#
# 所以：**任何用编辑器改过这个文件之后，都要确认 BOM 还在**：
#     python -c "import pathlib; b=pathlib.Path('win-tts.ps1').read_bytes(); print(b[:3])"
#     期望 (239, 187, 191)
#
# --------------------------------------------------------------------------- #
# 用法
# --------------------------------------------------------------------------- #
#
#     # 列出本机可用的音色
#     powershell -NoProfile -ExecutionPolicy Bypass -File win-tts.ps1 -ListVoices
#
#     # 合成到 WAV 文件
#     powershell -NoProfile -ExecutionPolicy Bypass -File win-tts.ps1 `
#         -Text "你好呀" -OutFile out.wav -Voice "Microsoft Huihui Desktop"
#
# --------------------------------------------------------------------------- #
# 为什么输出 WAV 文件而不是 stdout
# --------------------------------------------------------------------------- #
#
# 两个理由：
#
# ① **和 probe-key.ps1 同一个教训** —— 经过 stdout 管道传二进制在 Windows 上
#    很脆（文本模式下会被换行转换破坏）。写文件最稳。
#
# ② **WAV 可以直接取出裸 PCM**。`SetOutputToWaveFile` 写出的是标准
#    16kHz / 16bit / 单声道 PCM —— 正是前端 `voice-io.js` 的 `format='pcm'`
#    分支要的东西（它直接按 Int16 解包络、喂口型）。
#    云端 TTS 返回的是 **MP3**，压缩格式**没法算包络**，口型只能退回随机动效。
#    这是本地路线的一个实打实的好处。

param(
    [string]$Text = '',
    [string]$OutFile = '',
    [string]$Voice = '',
    [int]$Rate = 0,
    [int]$Volume = 100,
    [string]$LogFile = '',
    [switch]$ListVoices
)

$ErrorActionPreference = 'Stop'

# --------------------------------------------------------------------------- #
# 出错时把原因写进文件
# --------------------------------------------------------------------------- #
#
# ## 为什么要脚本自己写日志（2026-10-08 踩到）
#
# 调用方（Electron 主进程）不能用管道收 stderr（`pushToTalk.ts` 那段说明过
# 这个环境里管道的坑），也不能靠 `cmd /c ... > file` 或 `powershell -Command
# "... *> file"` —— 那两种**在 Node 的 spawn 里都会因为参数被二次加引号而拼坏**
# （实测：手工在 shell 里跑成功、WAV 67480 字节；Node 里跑 exit=1 且日志文件为空）。
#
# 所以：**脚本自己写日志**。PowerShell 的原始错误信息一定拿得到，
# 调用方只读那个文件 —— 不依赖任何 shell 级的重定向。
#
# 不传 `-LogFile` 时行为不变（不写日志）。
function Write-Diag([string]$msg) {
    if (-not $LogFile) { return }
    try {
        Set-Content -LiteralPath $LogFile -Value $msg -Encoding UTF8
    } catch {
        # 日志都写不了就只能放弃 —— 不能让"记日志"把合成本身搞挂
    }
}

try {

# 只要能从 stdlib 找到，就不用管 Office 那套 COM
Add-Type -AssemblyName System.Speech

$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer

# ---------------------------------------------------------------- 列出音色
if ($ListVoices) {
    # 输出格式刻意用**制表符分隔**，Node 侧按行切开就行 ——
    # 比 JSON 简单，而且在 PowerShell 5.1 里不用担心 ConvertTo-Json 的深度问题。
    foreach ($v in $synth.GetInstalledVoices()) {
        $info = $v.VoiceInfo
        # Enabled=False 的音色（有些系统上中文语音默认没启用）也要报出来，
        # 否则用户在设置页看不到它、以为没有中文声音。
        $ok = if ($v.Enabled) { '1' } else { '0' }
        Write-Output ("{0}`t{1}`t{2}`t{3}`t{4}" -f $info.Name, $info.Culture, $info.Gender, $info.Age, $ok)
    }
    $synth.Dispose()
    exit 0
}

# ---------------------------------------------------------------- 合成
if (-not $Text) {
    Write-Diag '没有要合成的文字（-Text 为空）'
    [Console]::Error.WriteLine('没有要合成的文字（-Text 为空）')
    $synth.Dispose()
    exit 2
}
if (-not $OutFile) {
    Write-Diag '没有指定输出文件（-OutFile 为空）'
    [Console]::Error.WriteLine('没有指定输出文件（-OutFile 为空）')
    $synth.Dispose()
    exit 2
}

# ---------------------------------------------------------------- 选音色
#
# ## ⚠️ `SelectVoice` 失败**不是致命错误**（2026-10-08 实测踩到）
#
# 从 Electron 起的 PowerShell 里，`SelectVoice('Microsoft Huihui Desktop')` 会抛：
#
#     No voice installed on the system or none available with the current
#     security setting.
#
# 而**同一个进程**里 `GetInstalledVoices()` 明明列出了那个音色（Enabled=True）
# —— 枚举得到、选不中。同一台机器、同一个脚本、同一个 64 位 PowerShell，
# 唯一变量是「谁起的进程」（shell ✅ / Node ✅ / Python ❌ / Electron ❌）。
#
# 但**默认音色是可用的**，而且这台机器的默认就是 `Microsoft Huihui Desktop`
# （zh-CN）—— 也就是我们本来想要的那个。
#
# 所以第一版把 `SelectVoice` 失败当致命错误（`exit 3`）是**错的**：
# 它让"明明能合成"变成了"一声不吭"。现在改成**退回默认音色**，
# 并把「实际用的是哪个」写进日志 —— 让状态可见，而不是静默降级。
$wanted = $Voice
$selected = ''
$reason = ''
if ($Voice) {
    try {
        $synth.SelectVoice($Voice)
        $selected = $Voice
    } catch {
        $selected = ''
        $reason = $_.Exception.Message
    }
}

$actual = ''
try { $actual = $synth.Voice.Name } catch { $actual = '' }
$culture = ''
try { $culture = $synth.Voice.Culture.Name } catch { $culture = '' }

if ($wanted -and -not $selected) {
    # 想选的没选上 —— 记清楚，但**继续合成**
    Write-Diag ("SelectVoice('$wanted') 失败，已退回默认音色 '$actual'（$culture）。原因：$reason")
    [Console]::Error.WriteLine("没能选中音色 '$wanted'，改用默认 '$actual'（$culture）")
} elseif ($wanted) {
    Write-Diag "使用指定音色：$actual（$culture）"
} else {
    Write-Diag "未指定音色，使用默认：$actual（$culture）"
}

if (-not $actual) {
    # 连默认音色都取不到 -> 这才是真的不可用
    $names = (@($synth.GetInstalledVoices()) | ForEach-Object { $_.VoiceInfo.Name }) -join ' / '
    Write-Diag "SAPI 没有可用音色（默认也取不到）。已安装：$names"
    [Console]::Error.WriteLine('SAPI 没有可用音色')
    $synth.Dispose()
    exit 3
}

# Rate 范围 -10..10，Volume 0..100（SAPI 的规定）
if ($Rate -lt -10) { $Rate = -10 }
if ($Rate -gt 10) { $Rate = 10 }
if ($Volume -lt 0) { $Volume = 0 }
if ($Volume -gt 100) { $Volume = 100 }
$synth.Rate = $Rate
$synth.Volume = $Volume

# ⚠️ 先删掉旧文件：SAPI 的 SetOutputToWaveFile 语义文档没写清，
# 删掉最保险 —— 否则第二次合成的文件里会带着上一次的音频。
if (Test-Path $OutFile) {
    Remove-Item $OutFile -Force
}

$synth.SetOutputToWaveFile($OutFile)
try {
    $synth.Speak($Text)
} finally {
    # ⚠️ 必须显式收尾，否则 WAV 头里的长度字段没被回填，文件是坏的
    $synth.SetOutputToNull()
    $synth.Dispose()
}

if (-not (Test-Path $OutFile)) {
    Write-Diag '合成结束但没有生成文件'
    [Console]::Error.WriteLine('合成结束但没有生成文件')
    exit 4
}

$size = (Get-Item $OutFile).Length
if ($size -le 44) {
    # 44 字节是纯 WAV 头、没有任何采样 —— 说明这段文字没被说出声
    Write-Diag "生成的 WAV 只有 $size 字节（没有音频数据）"
    [Console]::Error.WriteLine("生成的 WAV 只有 $size 字节（没有音频数据）")
    exit 5
}

# 成功时**什么都不输出**：调用方按退出码 + 文件判断。
# 输出任何东西都可能混进 Node 侧的解析（那边默认信任 stdout 干净）。
Write-Diag ''
exit 0

} catch {
    # 兜底：任何未预期的异常都记下来再抛（`$ErrorActionPreference='Stop'` 下
    # 这会走到这里）
    Write-Diag "未预期的错误：$($_.Exception.GetType().Name): $($_.Exception.Message)"
    throw
}
