# 启动本机隔离 LAB（MySQL 使用已有服务，PostgreSQL 使用项目内数据目录）。
# 用法： powershell -ExecutionPolicy Bypass -File scripts/start_lab.ps1
# 不会修改系统服务；PG 进程随当前会话结束可能退出，正式环境请使用 Compose。

$Root = Split-Path -Parent $PSScriptRoot
if ($env:DG_PG_BIN) {
    $PgBin = $env:DG_PG_BIN
} else {
    $PgBin = 'D:\bigplan\tools\postgresql-12.18-1\bin'
}
$PgData = Join-Path $Root 'deploy\pg_lab\data'
$PgLog = Join-Path $Root 'deploy\pg_lab\postgres.log'
$PgPort = 55432

function TestPort {
    param([string]$HostName, [int]$Port)
    try {
        $result = Test-NetConnection -ComputerName $HostName -Port $Port -InformationLevel Quiet -WarningAction SilentlyContinue
        return [bool]$result
    } catch {
        return $false
    }
}

Write-Host "[1/2] MySQL 127.0.0.1:3306 ..."
if (TestPort -HostName '127.0.0.1' -Port 3306) {
    Write-Host '  MySQL: AVAILABLE'
} else {
    Write-Host '  MySQL: UNAVAILABLE（请启动本机 MySQL 8 服务）'
}

Write-Host "[2/2] PostgreSQL 127.0.0.1:$PgPort ..."
if (TestPort -HostName '127.0.0.1' -Port $PgPort) {
    Write-Host '  PostgreSQL: AVAILABLE（已在运行）'
} else {
    $pgCtl = Join-Path $PgBin 'pg_ctl.exe'
    if (-not (Test-Path $pgCtl)) {
        Write-Host "  PostgreSQL: 未找到 pg_ctl（设置 DG_PG_BIN 或安装 PG）"
        exit 1
    }
    if (-not (Test-Path (Join-Path $PgData 'PG_VERSION'))) {
        Write-Host "  PostgreSQL: 数据目录未初始化：$PgData"
        Write-Host "  请先运行: & '$PgBin\initdb.exe' -D '$PgData' -U dg_admin --auth=trust --encoding=UTF8 --no-locale"
        exit 1
    }
    & $pgCtl -D $PgData -l $PgLog -o "-p $PgPort -h 127.0.0.1" -w start
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  PostgreSQL: 启动失败，日志见 $PgLog"
        exit 1
    }
    Start-Sleep -Seconds 2
    if (TestPort -HostName '127.0.0.1' -Port $PgPort) {
        Write-Host '  PostgreSQL: AVAILABLE（已启动）'
    } else {
        Write-Host "  PostgreSQL: 启动后端口不可达，日志见 $PgLog"
        exit 1
    }
}

Write-Host ''
Write-Host '初始化（如 .env 提供管理员凭据则自动执行，幂等）：'
$EnvFile = Join-Path $Root '.env'
if (Test-Path $EnvFile) {
    Get-Content $EnvFile | ForEach-Object {
        if ($_ -match '^\s*DG_[A-Z0-9_]+\s*=') {
            $parts = $_ -split '=', 2
            [Environment]::SetEnvironmentVariable($parts[0].Trim(), $parts[1].Trim(), 'Process')
        }
    }
}

$Python = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path $Python)) {
    Write-Host '  未找到 .venv，跳过自动初始化'
    exit 0
}

if ($env:DG_ADMIN_DSN) {
    & $Python (Join-Path $Root 'scripts\bootstrap_mysql.py')
    if ($LASTEXITCODE -ne 0) { Write-Host 'MySQL 初始化失败'; exit 1 }
} else {
    Write-Host '  跳过 MySQL 初始化：未设置 DG_ADMIN_DSN'
}

if ($env:DG_PG_ADMIN_DSN) {
    & $Python (Join-Path $Root 'scripts\bootstrap_pg_lab.py')
    if ($LASTEXITCODE -ne 0) { Write-Host 'PostgreSQL 初始化失败'; exit 1 }
} else {
    Write-Host '  跳过 PostgreSQL 初始化：未设置 DG_PG_ADMIN_DSN'
}

& $Python (Join-Path $Root 'scripts\environment_manifest.py')
if ($LASTEXITCODE -ne 0) { Write-Host '环境 Manifest 生成失败'; exit 1 }
exit 0



