# 快速更新反代服务（不做全量构建）
#
# 用途：只改了 reverse-proxy/ 下的 Python 或 admin/static/ 下的网页时，
#       用它代替 scripts/build-win-x64.sh。约 31 秒，而不是十几分钟。
#
# 用法：
#   pwsh -ExecutionPolicy Bypass -File scripts/quick-proxy.ps1
#   pwsh -ExecutionPolicy Bypass -File scripts/quick-proxy.ps1 -NoInstall
#
# 安全约定：**本脚本绝不杀死任何进程。** 目标目录里的反代可能正在为用户服务。
#           文件被占用时就停下并提示，由用户自己决定何时停服务。
param([switch]$NoInstall)

$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Py   = 'C:\Users\10086\.workbuddy\binaries\python\envs\wbs-plus\Scripts\python.exe'
$Src  = Join-Path $Root 'reverse-proxy\dist\wb-reverse-proxy.exe'
$Inst = Join-Path $env:LOCALAPPDATA 'WB-Switch-Plus'
$Dest = Join-Path $Inst 'wb-reverse-proxy.exe'

if (-not (Test-Path $Py)) { throw "找不到 Python: $Py" }

Write-Host '==> 1/2 重打反代 exe' -ForegroundColor Cyan
$sw = [Diagnostics.Stopwatch]::StartNew()
Push-Location (Join-Path $Root 'reverse-proxy')
try {
    # PyInstaller 往 stderr 写 INFO 日志；在 $ErrorActionPreference='Stop' 下
    # 那会被当成 ErrorRecord 直接中断。这里临时放宽，只看退出码——它才是成败判据。
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $log = & $Py build-exe.py 2>&1
    $code = $LASTEXITCODE
    $ErrorActionPreference = $prev
    $log | Select-Object -Last 3 | ForEach-Object { Write-Host "    $_" }
    if ($code -ne 0) { throw "build-exe.py 失败，EXIT=$code" }
} finally { Pop-Location }
$sw.Stop()
$secs = [math]::Round($sw.Elapsed.TotalSeconds, 1)
Write-Host "    OK  $secs 秒" -ForegroundColor Green

if ($NoInstall) {
    Write-Host '==> 跳过安装（-NoInstall）' -ForegroundColor Yellow
    exit 0
}

Write-Host '==> 2/2 覆盖到已安装目录' -ForegroundColor Cyan
if (-not (Test-Path $Inst)) {
    Write-Host "    已安装目录不存在，跳过：$Inst" -ForegroundColor Yellow
    exit 0
}

# 文件锁探测：真正在跑的 exe 会被独占。比查端口可靠。
$locked = $false
if (Test-Path $Dest) {
    try {
        $fs = [System.IO.File]::Open($Dest, 'Open', 'ReadWrite', 'None')
        $fs.Close()
    } catch {
        $locked = $true
    }
}

if ($locked) {
    Write-Host ''
    Write-Host '  [停止] 反代服务正在运行，文件被占用。' -ForegroundColor Yellow
    Write-Host '         请先在桌面端点「停止反代服务」，或退出桌面端，然后重跑本脚本。' -ForegroundColor Yellow
    Write-Host '         本脚本不会替你杀掉它，那可能是你正在用的服务。' -ForegroundColor Yellow
    Write-Host ''
    Write-Host "         新 exe 已备好，在：$Src" -ForegroundColor Cyan
    exit 2
}

Copy-Item $Src $Dest -Force
$st = Get-Item $Dest
$mb = [math]::Round($st.Length / 1MB, 2)
Write-Host "    OK  $Dest  $mb MB" -ForegroundColor Green
Write-Host ''
Write-Host '完成。重启桌面端即可看到新版本。' -ForegroundColor Green