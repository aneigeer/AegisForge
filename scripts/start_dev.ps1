# 一条命令启动本地开发环境（读取 .env -> 初始化 MySQL -> 启动 API）
# 用法：powershell -ExecutionPolicy Bypass -File scripts/start_dev.ps1
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$EnvFile = Join-Path $Root ".env"
$Python = Join-Path $Root ".venv\Scripts\python.exe"

if (-not (Test-Path $EnvFile)) {
    throw "缺少 .env：先执行 Copy-Item .env.example .env 并填写本机值"
}
if (-not (Test-Path $Python)) {
    throw "缺少虚拟环境：先执行 uv venv .venv --python 3.14 并安装依赖"
}

Get-Content $EnvFile | ForEach-Object {
    if ($_ -match '^([^#=]+)=(.*)$') {
        Set-Item -Path ("env:" + $matches[1]) -Value $matches[2]
    }
}

& $Python (Join-Path $Root "scripts\bootstrap_mysql.py")
& $Python -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8000
