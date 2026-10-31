"""自签名代码签名证书生成 + 对安装包签名（2026-10-30）。

## 为什么需要这个

打包出来的 exe 一直是 `NotSigned`（实测 0.1.0 ~ 0.2.2 全部如此），
所以每次双击都会弹「Windows 已保护你的电脑 / 未知发布者」。

## 自签名能解决什么、不能解决什么（**先说清楚，别抱错期望**）

**能解决**：在**装了本证书的机器上**，双击不再弹 SmartScreen。
够用于「自己用 + 少数朋友装」。

**不能解决**：陌生用户首次下载仍会被拦。SmartScreen 的声誉绑定
**商业 CA 签发的证书**，自签名的根不在微软信任列表里，陌生人机器上
和完全没签名没有区别。

## 用法

    # ① 生成证书（只需一次，证书 5 年有效）
    python tools/make-signing-cert.py create

    # ② 导出给朋友的安装证书的脚本
    python tools/make-signing-cert.py export

    # ③ 给已打好的包签名
    python tools/make-signing-cert.py sign

## ⚠️ 为什么要独立脚本而不是塞进 electron-builder

`electron-builder` 的自动签名要求证书在**当前用户的证书存储**里能被
按 subject 匹配到，行为随版本变化，出错时的报错信息很难懂
（`Cannot find valid certificate`）。独立脚本 + 显式参数可控得多，
而且**签名时机可以放在打包之后**，不影响已经验证过的打包流程。
"""

from __future__ import annotations

import argparse
import base64
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "build" / "signing"
PFX_PATH = OUT_DIR / "desktoppet-selfsigned.pfx"
CER_PATH = OUT_DIR / "desktoppet-selfsigned.cer"
PS1_PATH = OUT_DIR / "install-cert.ps1"
README_PATH = OUT_DIR / "说明.txt"

# 证书主题。**必须和产品对得上** —— 用户在 SmartScreen 弹窗里看到的就是它。
SUBJECT = "CN=Tianyi DesktopPet (Self-Signed), O=Tianyi, C=CN"
FRIENDLY = "DesktopPet 自签名代码签名证书"

# PFX 密码：导出时用，安装脚本里也用它。**不是机密** —— 证书就在仓库外的
# build/signing/ 下，且只在本机用。但别把它当成身份凭据到处传。
PFX_PASSWORD = "desktoppet"

SIGNTOOL_CANDIDATES = [
    r"C:\Program Files (x86)\Windows Kits\10\bin\10.0.19041.0\x64\signtool.exe",
    r"C:\Program Files (x86)\Windows Kits\10\bin\10.0.16299.0\x64\signtool.exe",
]

INSTALL_PS1 = r"""# DesktopPet 代码签名证书安装脚本
#
# 用途：让这台机器信任 DesktopPet 的发布者，双击安装包时不再弹
#       「Windows 已保护你的电脑 / 未知发布者」。
#
# ⚠️ 只在你信任这个软件的情况下运行。装根证书 = 让系统信任**这个证书
#    签发的任何东西**，所以只该装你确信来源的证书。
#
# 用法：右键本文件 →「使用 PowerShell 运行」，
#       或在本目录打开 PowerShell 执行：
#           powershell -ExecutionPolicy Bypass -File .\install-cert.ps1

$ErrorActionPreference = 'Stop'
$cer = Join-Path $PSScriptRoot 'desktoppet-selfsigned.cer'

if (-not (Test-Path $cer)) {
    Write-Host "找不到证书文件：$cer" -ForegroundColor Red
    Write-Host "请把 desktoppet-selfsigned.cer 和本脚本放在同一个目录。" -ForegroundColor Yellow
    exit 1
}

Write-Host "正在安装 DesktopPet 代码签名证书…" -ForegroundColor Cyan

# 装到「受信任的根证书颁发机构」——**必须**是这一档，
# 装到「受信任的发布者」只能解决"发布者是谁"，SmartScreen 仍会拦。
$root = [System.Security.Cryptography.X509Certificates.StoreName]::Root
$store = New-Object System.Security.Cryptography.X509Certificates.X509Store($root, 'CurrentUser')
$store.Open('ReadWrite')
$cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2($cer)
$store.Add($cert)
$store.Close()

Write-Host ""
Write-Host "✅ 完成。证书已装入「当前用户 → 受信任的根证书颁发机构」。"  -ForegroundColor Green
Write-Host "   现在双击 DesktopPet 安装包不会再报「未知发布者」。" -ForegroundColor Green
Write-Host ""
Write-Host "卸载方法：certmgr.msc → 受信任的根证书颁发机构 → 证书 →" -ForegroundColor Gray
Write-Host "        删除「$($cert.Subject)」" -ForegroundColor Gray
"""

README_TXT = r"""DesktopPet 自签名证书 —— 说明
================================================

## 这是什么

一个**自己签的**代码签名证书，用来替代花钱买的商业证书。

## 为什么会有它

DesktopPet 没有代码签名证书，所以 Windows 每次双击都弹
「已保护你的电脑 / 未知发布者」。买商业证书要 1500~4000 元/年，
自己用和少数朋友用不值当，于是用自签名解决。

## 在你机器上装一次，之后永久生效

1. 把 `desktoppet-selfsigned.cer` 和 `install-cert.ps1` 放同一个目录
2. 右键 `install-cert.ps1` →「使用 PowerShell 运行」
   （或在该目录执行 `powershell -ExecutionPolicy Bypass -File .\install-cert.ps1`）
3. 看到 ✅ 就好了

之后**所有版本**的 DesktopPet 安装包都不再弹提示，不用每次装。

## ⚠️ 装之前请确认

装证书等于让系统信任这个证书签发的所有程序。所以：

· 只装你**确实信任来源**的证书
· 这个脚本只写「当前用户」的证书存储，**不需要管理员权限**，
  也不会改动系统级设置
· 卸载：`certmgr.msc` → 受信任的根证书颁发机构 → 证书 →
  删除「CN=Tianyi DesktopPet (Self-Signed)」

## 另一个更简单的办法

用**绿色包**（`*-portable.zip`）。zip 解压后运行不会走安装器的检查，
很多情况下根本不弹提示。代价是配置和数据存在用户目录，换机器要重配。

## 想要真的对陌生人不报警

自签名做不到 —— 陌生人的机器上没有这个根证书，行为和没签名一样。
那需要买 **EV 代码签名证书**（约 2500~4000 元/年，还要硬件 U 盾），
它能让 SmartScreen 立即建立信任。OV 证书则仍需积累下载量，通常要等几个月。
"""


def find_signtool() -> str:
    """找一个能用的 signtool.exe。"""
    for path in SIGNTOOL_CANDIDATES:
        if Path(path).exists():
            return path
    # 退一步：在整个 SDK 目录里翻（版本号会变）
    base = Path(r"C:\Program Files (x86)\Windows Kits\10\bin")
    if base.exists():
        for p in sorted(base.glob("*/x64/signtool.exe"), reverse=True):
            return str(p)
    raise SystemExit("❌ 找不到 signtool.exe。装了 Windows SDK 才有。")


def run(
    cmd: list[str], *, check: bool = True, quiet: bool = False
) -> subprocess.CompletedProcess[str]:
    """跑一个命令。

    ⚠️ `quiet=True` 时**不打印**命令行 —— 像 `status` 这种要列一堆包的命令，
    每条前面都跟一行 `$ powershell …` 会把输出淹掉（第一版就是那样）。
    """
    if not quiet:
        print("   $", " ".join(cmd[:3]), "…" if len(cmd) > 3 else "")
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def run_ps(script: str, *, quiet: bool = False) -> subprocess.CompletedProcess[str]:
    """跑一段 PowerShell。

    ## 🚨 从 Python 子进程创建证书**做不到**，这是本工具最反直觉的一点

    2026-10-30 实测，同一段脚本、同一个 `powershell.exe`：

        pwsh 工具直接执行        -> ✅ OK，拿到指纹
        python subprocess 执行    -> ❌ CertEnroll::…: Access denied
                                     0x80090010 (NTE_PERM)

    `NTE_PERM` = "调用者无权访问该密钥容器"。换过 `-Command` / `-File` /
    `-EncodedCommand`、换过工作目录（`%TEMP%` / `F:\\ai` / `C:\\`）、
    换过环境变量（显式 `APPDATA`）、试过 `schtasks` 委托 —— **全部失败**。
    这是当前执行环境对子进程的限制，不是代码能修的。

    所以本工具的分工是：

      · `create`  —— 需要**用户在自己开的管理员/普通 PowerShell 里**跑，
                     或由 agent 用 pwsh 工具直接执行。脚本会把该跑的命令打出来。
      · `sign`    —— 只需要读 PFX，**子进程可以做**（signtool 不碰密钥容器）。
      · `status`  —— 纯查询，子进程可以做。

    ## ⚠️ 另两个坑（都验证过）

    **① 不要用 `-Command` 传多行脚本**：引号经 shell 层会被吃掉/错配，
    PowerShell 收到残缺脚本，报错莫名其妙（`The term 'ERR' is not recognized`）。

    **② 必须设 `$ProgressPreference = 'SilentlyContinue'`**：不设的话
    PowerShell 把进度信息以 **CLIXML** 塞进 stderr，混在真正的错误里。
    """
    if not quiet:
        print("   $ powershell -EncodedCommand …")
    prelude = "$ProgressPreference = 'SilentlyContinue'\n"
    encoded = base64.b64encode((prelude + script).encode("utf-16-le")).decode("ascii")
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        capture_output=True,
        text=True,
        check=False,
    )


def cmd_create(ns: argparse.Namespace) -> int:
    """生成自签名证书 + 导出 PFX / CER / 安装脚本 / 说明。"""
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    if PFX_PATH.exists() and not ns.force:
        print(f"⚠️ 已存在：{PFX_PATH}")
        print("   要重新生成请加 --force（旧签名会失效，已发的包要重签）。")
        return 1

    print("① 生成自签名代码签名证书…")
    ps = f"""
$ErrorActionPreference = 'Stop'
$cert = New-SelfSignedCertificate `
    -Type CodeSigningCert `
    -Subject '{SUBJECT}' `
    -FriendlyName '{FRIENDLY}' `
    -CertStoreLocation 'Cert:\\CurrentUser\\My' `
    -KeyExportPolicy Exportable `
    -KeySpec Signature `
    -KeyLength 2048 `
    -KeyAlgorithm RSA `
    -HashAlgorithm SHA256 `
    -NotAfter (Get-Date).AddYears(5)
$pw = ConvertTo-SecureString -String '{PFX_PASSWORD}' -Force -AsPlainText
Export-PfxCertificate -Cert $cert -FilePath '{PFX_PATH}' -Password $pw | Out-Null
Export-Certificate -Cert $cert -FilePath '{CER_PATH}' | Out-Null
Write-Output $cert.Thumbprint
"""
    r = run_ps(ps)

    thumbprint = ""
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        if len(line) == 40 and all(c in "0123456789ABCDEFabcdef" for c in line):
            thumbprint = line

    if not thumbprint:
        # 这就是 run_ps 文档里说的那个限制。**不要重试、不要换方式** ——
        # 已经穷举过 -Command/-File/-EncodedCommand/换目录/换环境变量/schtasks，
        # 全部失败。给用户一条能直接粘贴的命令最有用。
        print("   ⚠️ 从当前进程创建证书被系统拒绝（NTE_PERM）。")
        print("      这是执行环境的限制，不是脚本的问题。")
        print()
        print("   请**手动**在 PowerShell 里跑下面这段（会弹一次安全确认，点「是」）：")
        print()
        print("   " + "-" * 72)
        for line in ps.strip().splitlines():
            print("   " + line)
        print("   " + "-" * 72)
        print()
        print("   跑完再执行本脚本的 `export` 和 `sign`。")
        return 1

    print(f"   指纹：{thumbprint}")
    print(f"   PFX：{PFX_PATH}")
    print(f"   CER：{CER_PATH}")

    print("② 写安装脚本和说明…")
    PS1_PATH.write_text(INSTALL_PS1, encoding="utf-8-sig")
    README_PATH.write_text(README_TXT, encoding="utf-8")
    print(f"   {PS1_PATH.name} / {README_PATH.name}")

    print()
    print("✅ 证书就绪。接下来：")
    print("   签名现有安装包：python tools/make-signing-cert.py sign")
    print("   发给朋友：python tools/make-signing-cert.py export")
    return 0


def cmd_export(_: argparse.Namespace) -> int:
    """把要发给朋友的文件收进一个干净的目录。"""
    if not CER_PATH.exists():
        print("❌ 还没生成证书，先跑 create")
        return 1
    dest = OUT_DIR / "发给朋友"
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(CER_PATH, dest / CER_PATH.name)
    PS1_PATH.write_text(INSTALL_PS1, encoding="utf-8-sig")
    shutil.copy2(PS1_PATH, dest / PS1_PATH.name)
    (dest / "说明.txt").write_text(README_TXT, encoding="utf-8")
    print(f"✅ 已收进：{dest}")
    for f in sorted(dest.iterdir()):
        print(f"   {f.name}  ({f.stat().st_size} 字节)")
    print()
    print("   整个目录打包发给朋友，让他们先跑 install-cert.ps1 再装 DesktopPet。")
    return 0


def cmd_sign(ns: argparse.Namespace) -> int:
    """用证书给 exe 签名。默认签 release 下所有安装包。"""
    if not PFX_PATH.exists():
        print("❌ 还没生成证书，先跑 create")
        return 1
    signtool = find_signtool()

    targets: list[Path] = []
    if ns.paths:
        targets = [Path(p).resolve() for p in ns.paths]
    else:
        targets = sorted((ROOT / "release").glob("*.exe"))
        # 便携 zip 里的 exe 单独处理（见下面 cmd_sign 的说明）
    if not targets:
        print("❌ 没找到要签的文件。传路径，或先打包。")
        return 1

    failed = 0
    for exe in targets:
        if not exe.exists():
            print(f"⚠️ 跳过（不存在）：{exe}")
            continue
        print(f"签名：{exe.name}")
        r = run(
            [
                signtool,
                "sign",
                "/f", str(PFX_PATH),
                "/p", PFX_PASSWORD,
                "/fd", "sha256",
                "/tr", "http://timestamp.digicert.com",
                "/td", "sha256",
                str(exe),
            ],
            check=False,
        )
        if r.returncode != 0:
            print("    ❌ 失败：", (r.stdout or r.stderr)[:400])
            failed += 1
            continue
        v = run([signtool, "verify", "/pa", "/v", str(exe)], check=False)
        ok = v.returncode == 0
        print("    ✅ 已签名" if ok else "    ⚠️ 签名成功但校验异常")
        if not ok:
            print("      ", (v.stdout or v.stderr)[:300])

    print()
    if failed:
        print(f"❌ {failed} 个失败")
        return 1
    print("✅ 全部完成")
    print()
    print("⚠️ 注意：**便携版 zip 是打包前就压好的**，里面的 exe 没签名。")
    print("   要让便携版也带签名，顺序必须是：")
    print("     1. npx electron-builder …   （生成 release/win-unpacked）")
    print("     2. 本脚本给 win-unpacked 里的 exe 签名")
    print("     3. tar -a -c -f … 打 zip")
    print("   见 docs/打包说明.md 的「签名」一节。")
    return 0


def cmd_status(_: argparse.Namespace) -> int:
    """看看现在有没有证书、release 里的包签没签。"""
    print("── 证书 ──")
    if PFX_PATH.exists():
        print(f"   PFX 存在：{PFX_PATH}")
    else:
        print("   没有 PFX（还没 create）")

    ps = (
        "Get-ChildItem Cert:\\CurrentUser\\My | "
        f"Where-Object {{ $_.Subject -like '*DesktopPet*' }} | "
        "ForEach-Object { $_.Subject + ' | ' + $_.NotAfter.ToString('yyyy-MM-dd') }"
    )
    r = run(["powershell", "-NoProfile", "-Command", ps], check=False, quiet=True)
    body = (r.stdout or "").strip()
    print("   证书存储里：", body if body else "无")

    print()
    print("── release 里的安装包 ──")
    for exe in sorted((ROOT / "release").glob("*.exe")):
        v = run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-AuthenticodeSignature '{exe}').Status"],
            check=False,
            quiet=True,
        )
        print(f"   {exe.name:<44} {(v.stdout or '').strip()}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="DesktopPet 自签名证书工具")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_create = sub.add_parser("create", help="生成自签名证书（一次）")
    p_create.add_argument("--force", action="store_true", help="覆盖已存在的证书")
    p_create.set_defaults(func=cmd_create)
    sub.add_parser("export", help="收集发给朋友的文件").set_defaults(func=cmd_export)
    p_sign = sub.add_parser("sign", help="给 exe 签名")
    p_sign.add_argument("paths", nargs="*", help="要签的 exe；不给就签 release/*.exe")
    p_sign.set_defaults(func=cmd_sign)
    sub.add_parser("status", help="查看证书和包的签名状态").set_defaults(func=cmd_status)

    ns = ap.parse_args()
    return int(ns.func(ns))


if __name__ == "__main__":
    raise SystemExit(main())
