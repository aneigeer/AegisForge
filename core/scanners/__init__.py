"""V4.1 Track A：本地仓库安全扫描器包。

对外仅暴露 :mod:`core.scanners.repo_scanner` 的采集与扫描入口，
内部不实现任何检测规则——规则一律复用 ``AppSecProbe`` / ``K8sProbe``。
"""

from core.scanners.repo_scanner import (
    DEFAULT_MAX_FILE_BYTES,
    DEFAULT_MAX_FILES,
    EXCLUDED_DIRS,
    RepoInventory,
    RepoScanConfig,
    RepoScanResult,
    collect_repo_inventory,
    priority_from_cvss,
    scan_repository,
)

__all__ = [
    "DEFAULT_MAX_FILE_BYTES",
    "DEFAULT_MAX_FILES",
    "EXCLUDED_DIRS",
    "RepoInventory",
    "RepoScanConfig",
    "RepoScanResult",
    "collect_repo_inventory",
    "priority_from_cvss",
    "scan_repository",
]
