# AegisForge 独立桌面软件一键打包脚本（生成原生 Windows GUI 可执行程序 AegisForge.exe）。
# 用法：powershell -ExecutionPolicy Bypass -File scripts/build_desktop_app.ps1

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path $Python)) { $Python = 'python' }

Write-Host '== 1/3 初始化 .env 与 BYOK Fernet 主密钥 =='
& $Python -m apps.desktop.launcher --prepare-only
if ($LASTEXITCODE -ne 0) { throw '桌面环境预检失败' }

Write-Host '== 2/3 编译 Windows 原生桌面客户端 (AegisForge.exe) =='
$Csc = 'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path $Csc)) {
    throw "未找到 Windows .NET 编译器: $Csc"
}

$DistDir = Join-Path $Root 'dist'
if (-not (Test-Path $DistDir)) { New-Item -ItemType Directory -Path $DistDir | Out-Null }

$CsSource = Join-Path $Root 'apps\desktop\NativeShell.cs'
$DistExe  = Join-Path $DistDir 'AegisForge.exe'
$RootExe  = Join-Path $Root 'AegisForge.exe'

& $Csc /nologo /codepage:65001 /target:winexe /optimize+ /out:$DistExe /r:System.Windows.Forms.dll /r:System.Drawing.dll $CsSource
if ($LASTEXITCODE -ne 0) { throw 'csc.exe 编译失败' }

Copy-Item -Path $DistExe -Destination $RootExe -Force

# 清理更名前旧可执行文件，避免目录中出现双份混乱
$OldRootExe = Join-Path $Root 'DBGuardian.exe'
$OldDistExe = Join-Path $DistDir 'DBGuardian.exe'
if (Test-Path $OldRootExe) { Remove-Item $OldRootExe -Force -ErrorAction SilentlyContinue }
if (Test-Path $OldDistExe) { Remove-Item $OldDistExe -Force -ErrorAction SilentlyContinue }

Write-Host '== 3/3 校验打包产物 =='
$Info = Get-Item $RootExe
Write-Host "  已生成桌面可执行程序: $($Info.FullName) ($($Info.Length) bytes)"
Write-Host "  已生成分发目录副本:   $DistExe"
Write-Host "PASS: 双击 $RootExe 即可直接启动独立桌面软件"
exit 0
