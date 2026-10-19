# Repoint junctions from the old project root (non-ASCII name) to F:\ai.
# ASCII-only source on purpose: PowerShell 5.1 reads .ps1 as ANSI, so any CJK
# literal here would be mangled and every comparison would fail.

$new = 'F:\ai'
$bases = @(
  (Join-Path $new 'node_modules'),
  (Join-Path $new 'apps\desktop\node_modules')
)

$paths = New-Object System.Collections.Generic.List[string]
foreach ($base in $bases) {
  if (-not (Test-Path $base)) { continue }
  $out = & cmd.exe /c "dir /al /s /b `"$base`" 2>nul"
  foreach ($line in $out) {
    if ($line -and $line -match '^[A-Za-z]:\\') { $paths.Add($line.Trim()) }
  }
}

$fixed = 0
$skipped = 0
$failed = 0

foreach ($p in $paths) {
  $item = Get-Item $p -Force -ErrorAction SilentlyContinue
  if (-not $item) { $skipped++; continue }
  if ($item.LinkType -ne 'Junction') { $skipped++; continue }

  $target = @($item.Target)[0]
  if (-not $target) { $skipped++; continue }

  # target looks like  F:\<oldname>\node_modules\...   -> take everything after 2nd backslash
  if ($target.Length -lt 8) { $skipped++; continue }
  $idx = $target.IndexOf('\', 3)
  if ($idx -lt 0) { $skipped++; continue }
  $newTarget = $new + $target.Substring($idx)

  if (-not (Test-Path $newTarget)) { $skipped++; continue }
  if ($newTarget -eq $target) { $skipped++; continue }

  & cmd.exe /c "rmdir `"$p`"" 2>$null | Out-Null
  & cmd.exe /c "mklink /J `"$p`" `"$newTarget`"" 2>$null | Out-Null
  if (Test-Path $p) { $fixed++ } else { $failed++ }
}

Write-Host "  relinked: $fixed   skipped: $skipped   failed: $failed"
$electron = Join-Path $new 'apps\desktop\node_modules\electron\package.json'
$proto = Join-Path $new 'apps\desktop\node_modules\@pet\protocol\package.json'
Write-Host "  electron ok: $(Test-Path $electron)"
Write-Host "  protocol ok: $(Test-Path $proto)"
