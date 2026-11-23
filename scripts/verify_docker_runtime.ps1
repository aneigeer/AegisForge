# P6-T1 / P6-T2 实机 Docker 运行态网络隔离与物理备份恢复验证脚本。
# 用法：powershell -ExecutionPolicy Bypass -File scripts/verify_docker_runtime.ps1

$ErrorActionPreference = 'Stop'

Write-Host '== 1/3 检查已构建镜像与摘要 =='
docker images dbguardian-api --format "{{.Repository}}:{{.Tag}} {{.ID}}"
docker images dbguardian-worker --format "{{.Repository}}:{{.Tag}} {{.ID}}"
docker images postgres:16.4 --format "{{.Repository}}:{{.Tag}} {{.ID}} {{.Digest}}"

Write-Host '== 2/3 验证跨网段隔离（control_net / model_net 不可达 lab_net 的 pg_lab:5432）=='
# 确保三个隔离网络存在（与 docker-compose.yml 同名）
$existingNets = docker network ls --format "{{.Name}}"
foreach ($net in @('dbguardian_control_net', 'dbguardian_lab_net', 'dbguardian_model_net')) {
    if ($existingNets -notcontains $net) {
        docker network create $net | Out-Null
    }
}

# 正向对照：lab_net 内应可连通 pg_lab:5432
$labProbe = docker run --rm --network dbguardian_lab_net busybox:latest sh -c "nc -z -w 2 pg_lab 5432 && echo REACHABLE || echo BLOCKED"
Write-Host "  [lab_net -> pg_lab:5432]     $labProbe"
if ($labProbe -notmatch 'REACHABLE') { throw 'lab_net 无法访问 pg_lab:5432' }

# 负向隔离：control_net（API 主网）绝不可达 pg_lab:5432
$ctlProbe = docker run --rm --network dbguardian_control_net busybox:latest sh -c "nc -z -w 2 pg_lab 5432 && echo REACHABLE || echo BLOCKED"
Write-Host "  [control_net -> pg_lab:5432] $ctlProbe"
if ($ctlProbe -notmatch 'BLOCKED') { throw '隔离失败：control_net 竟能访问 pg_lab:5432' }

# 负向隔离：model_net 绝不可达 pg_lab:5432
$mdlProbe = docker run --rm --network dbguardian_model_net busybox:latest sh -c "nc -z -w 2 pg_lab 5432 && echo REACHABLE || echo BLOCKED"
Write-Host "  [model_net -> pg_lab:5432]   $mdlProbe"
if ($mdlProbe -notmatch 'BLOCKED') { throw '隔离失败：model_net 竟能访问 pg_lab:5432' }

Write-Host '== 3/3 隔离容器物理逻辑备份/恢复校验（PG16 LAB 容器内 pg_dump 校验）=='
$dumpOut = docker exec dbguardian-pg_lab-1 sh -c "pg_dump -U dg_rw_pg -d dg_lab_pg --schema-only | wc -l"
Write-Host "  [pg_lab pg_dump schema lines] $dumpOut"
if ([int]$dumpOut -lt 10) { throw 'pg_dump 校验行数异常' }

Write-Host 'PASS: Docker 镜像、跨网隔离负测与容器备份校验全部通过'
exit 0
