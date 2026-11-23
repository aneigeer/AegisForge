# W01 本地 CI 检查：编译、单元测试、Compose 静态校验、密钥扫描。
# 用法： powershell -ExecutionPolicy Bypass -File scripts/ci_check.ps1
# Docker 不可用时 Compose 校验标记 SKIPPED，不影响其他检查。

$ErrorActionPreference = 'Continue'
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path $Python)) { $Python = 'python' }
$Failures = @()
$Skipped = @()

Write-Host '== 1/4 compileall =='
& $Python -m compileall -q (Join-Path $Root 'apps'), (Join-Path $Root 'core'), (Join-Path $Root 'database'), (Join-Path $Root 'scripts')
if ($LASTEXITCODE -ne 0) { $Failures += 'compileall' }

Write-Host '== 2/4 unit tests =='
Push-Location $Root
& $Python -m pytest tests/unit -q
if ($LASTEXITCODE -ne 0) { $Failures += 'pytest tests/unit' }
Pop-Location

Write-Host '== 3/4 docker compose config =='
$Docker = Get-Command docker -ErrorAction SilentlyContinue
if ($null -eq $Docker) {
    $Skipped += 'docker compose config（未安装 docker）'
    Write-Host 'SKIPPED: docker not installed'
} else {
    & docker info 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        $Skipped += 'docker compose config（daemon 不可达）'
        Write-Host 'SKIPPED: docker daemon unavailable'
    } else {
        Push-Location $Root
        & docker compose config | Out-Null
        if ($LASTEXITCODE -ne 0) { $Failures += 'docker compose config' }
        Pop-Location
    }
}

Write-Host '== 4/4 secret scan =='
$Extensions = @('.py', '.toml', '.yml', '.yaml', '.json', '.md', '.ps1', '.sql', '.txt', '.example', '.cfg', '.ini')
$ExcludedPrefixes = @(
    (Join-Path $Root '.venv'),
    (Join-Path $Root '.git'),
    (Join-Path $Root '项目规划'),
    (Join-Path $Root 'runs'),
    (Join-Path $Root 'deploy\pg_lab'),
    (Join-Path $Root '.pytest_cache'),
    (Join-Path $Root '.screenshots'),
    (Join-Path $Root 'dbguardian.egg-info')
)
$Patterns = @(
    'BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY',
    'AKIA[0-9A-Z]{16}',
    'DG_RO_DSN=mysql://[^:\s]+:(?!CHANGE_ME)[^@\s]+@',
    'DG_RW_DSN=mysql://[^:\s]+:(?!CHANGE_ME)[^@\s]+@',
    'DG_PG_RO_DSN=postgresql://[^:\s]+:(?!CHANGE_ME)[^@\s]+@',
    'DG_ADMIN_DSN=mysql://[^:\s]+:(?!CHANGE_ME)[^@\s]+@'
)
$ExcludedFiles = @(
    (Join-Path $Root 'scripts\ci_check.ps1'),
    # W09 脱敏回归夹具：三处 PEM 标记是**截断的假密钥**（正文 14 字符、不含真实 RSA 材料），
    # 供 redaction engine 断言"能识别并替换私钥"用；已人工复核，非泄漏。
    # 2026-11-19 登记（发布件同步卡：ci 必须在干净工作树上 exit 0）。
    (Join-Path $Root 'tests\unit\test_w09_redaction_engine.py'),
    (Join-Path $Root 'tests\unit\test_w09_redaction_gate.py'),
    (Join-Path $Root 'tests\unit\test_w09_redaction_wiring.py')
)
$Hits = @()
Get-ChildItem -Path $Root -Recurse -File | Where-Object {
    $file = $_
    foreach ($prefix in $ExcludedPrefixes) {
        if ($file.FullName.StartsWith($prefix)) { return $false }
    }
    if ($ExcludedFiles -contains $file.FullName) { return $false }
    return ($Extensions -contains $file.Extension) -or ($file.Name -like '*.example')
} | ForEach-Object {
    $file = $_
    foreach ($pattern in $Patterns) {
        $match = Select-String -Path $file.FullName -Pattern $pattern -AllMatches -ErrorAction SilentlyContinue
        if ($match) { $Hits += "$($file.FullName.Substring($Root.Length)):$($match[0].LineNumber)" }
    }
}
if ($Hits.Count -gt 0) {
    $Failures += 'secret scan'
    Write-Host '命中（仅文件:行号，不输出内容）：'
    $Hits | Sort-Object -Unique | ForEach-Object { Write-Host "  $_" }
} else {
    Write-Host 'PASS: 未发现明显的私钥/真实 DSN 密码'
}

Write-Host ''
Write-Host '== 汇总 =='
if ($Skipped.Count -gt 0) { $Skipped | ForEach-Object { Write-Host "SKIPPED: $_" } }
if ($Failures.Count -gt 0) {
    $Failures | ForEach-Object { Write-Host "FAIL: $_" }
    exit 1
}
Write-Host 'PASS: 本地 CI 检查通过'
exit 0



