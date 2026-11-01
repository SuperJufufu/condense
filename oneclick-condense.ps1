<#
.SYNOPSIS
    condense 一键入口：把长文档/大日志压成摘要，并复制到剪贴板。

.DESCRIPTION
    用法一（推荐）：把文件**拖到** condense-dragdrop.bat 上。
    用法二：pwsh -File oneclick-condense.ps1 大文件.log
    用法三（只试跑，不调模型）：pwsh -File oneclick-condense.ps1 -DryRun 大文件.log
    用法四（从管道喂）：type 大文件.log | pwsh -File oneclick-condense.ps1 -

    输出：摘要写到大文件.log.digest.md，同时复制到剪贴板 —— 直接粘给云端模型。
#>
param(
    [switch]$DryRun,
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$Files
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$script = Join-Path $root 'condense.py'
if (-not (Test-Path -LiteralPath $script -PathType Leaf)) { Write-Host "找不到 condense.py：$script"; exit 1 }

$pythonCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCmd) { $pythonCmd = Get-Command py -ErrorAction SilentlyContinue }
if (-not $pythonCmd) { Write-Host "找不到 python，请先装 Python 或用 Ollama 自带的 python"; exit 1 }

if (-not $Files -or $Files.Count -eq 0) {
    Write-Host "用法：把文件拖到 condense-dragdrop.bat 上，或："
    Write-Host "  pwsh -File oneclick-condense.ps1 大文件.log"
    exit 1
}

$done = 0
foreach ($f in $Files) {
    $isStdin = ($f -eq '-')
    if (-not $isStdin -and -not (Test-Path -LiteralPath $f -PathType Leaf)) {
        Write-Host "跳过（找不到文件）：$f"
        continue
    }
    $full = if ($isStdin) { '-' } else { (Resolve-Path -LiteralPath $f).Path }
    $out = if ($isStdin) { Join-Path $root 'stdin.digest.md' } else { "$full.digest.md" }

    Write-Host ""
    Write-Host "=== 压缩：$full ==="
    $cliArgs = @($script, $full, '--auto', '--out', $out)
    if ($DryRun) { $cliArgs += '--dry-run' }
    & $pythonCmd.Source @cliArgs
    if ($LASTEXITCODE -ne 0) {
        Write-Host "❌ 失败（exit $LASTEXITCODE）：$full   —— 先看看 Ollama 在不在（ollama list）"
        continue
    }
    if ($DryRun) { $done++; continue }

    $digest = Get-Content -LiteralPath $out -Raw -Encoding UTF8
    try {
        Set-Clipboard -Value $digest
        Write-Host "📋 摘要已复制到剪贴板（$($digest.Length) 字符）→ 直接粘给云端模型"
    } catch {
        Write-Host "⚠ 复制剪贴板失败（$($_.Exception.Message)），手动打开这个文件："
    }
    Write-Host "   输出文件：$out"
    $done++
}

Write-Host ""
Write-Host "完成：$done/$($Files.Count)"
