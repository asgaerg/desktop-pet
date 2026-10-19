# Rebuild the desktop shortcuts for the pet (2026-10-19).
#
# Context: the project moved from F:\ai项目 to F:\ai, so the old shortcuts
# pointed at a path that no longer exists. 启动宠物.vbs derives its root from
# its own location, so only the shortcut itself has to change.
#
# NOTE: keep this file ASCII-only. Windows PowerShell 5.1 reads .ps1 using the
# system ANSI codepage (GBK here), so non-ASCII text inside gets mangled and
# breaks the parser. Chinese shortcut names are built with [char] codes below.

$ErrorActionPreference = 'Stop'

$root = 'F:\ai'
$desk = [Environment]::GetFolderPath('Desktop')
$icon = Join-Path $root 'apps\desktop\assets\pet.ico'

# U+684C U+9762 U+5BA0 U+7269 = desktop pet
$petName = [string]([char]0x684C + [char]0x9762 + [char]0x5BA0 + [char]0x7269)
# U+8C03 U+8BD5 = debug
$dbgTag = [string]([char]0x8C03 + [char]0x8BD5)

$vbs = Join-Path $root ([string]([char]0x542F + [char]0x52A8 + [char]0x5BA0 + [char]0x7269) + '.vbs')

$targets = @(
    @{
        Name  = "$petName.lnk"
        File  = $vbs
        Target = (Join-Path $env:WINDIR 'System32\wscript.exe')
        # //B = batch mode. Without it wscript pops an "Open File - Security
        # Warning" dialog for a .vbs outside a trusted zone; because the launcher
        # is supposed to be invisible, that dialog just sits there and the pet
        # never starts.
        Args  = '//B "' + $vbs + '"'
        Desc  = 'Desktop pet (no console window)'
        Icon  = $icon
    },
    @{
        Name  = "$petName ($dbgTag).lnk"
        File  = (Join-Path $root 'dev.cmd')
        Target = (Join-Path $root 'dev.cmd')
        Args  = ''
        Desc  = 'Desktop pet (keeps console, shows errors)'
        Icon  = $icon
    }
)

$shell = New-Object -ComObject WScript.Shell

foreach ($t in $targets) {
    if (-not (Test-Path -LiteralPath $t.File)) {
        Write-Output ("  [skip] missing: " + $t.File)
        continue
    }

    $lnkPath = Join-Path $desk $t.Name
    $lnk = $shell.CreateShortcut($lnkPath)
    $lnk.TargetPath = $t.Target
    $lnk.Arguments = $t.Args
    $lnk.WorkingDirectory = $root
    $lnk.Description = $t.Desc
    if (Test-Path -LiteralPath $t.Icon) { $lnk.IconLocation = $t.Icon + ',0' }
    $lnk.WindowStyle = 1
    $lnk.Save()

    $check = $shell.CreateShortcut($lnkPath)
    $exists = Test-Path -LiteralPath $lnkPath
    Write-Output ("  [ok]   " + $t.Name + "   exists=" + $exists)
    Write-Output ("         target=" + $check.TargetPath)
    if ($check.Arguments) { Write-Output ("         args=" + $check.Arguments) }
    Write-Output ("         icon=" + $check.IconLocation)
}

[void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($shell)
Write-Output '  done'
