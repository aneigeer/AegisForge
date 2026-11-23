"""审计归档端口与本地追加式实现（ADR-0006 §5）。

**口径（对外表述必须照抄）**：本模块只是"本地追加式 + 每批哈希链"，**不是 WORM**，
不具备任何对象锁、保留期强制或第三方合规认证。`ObjectStorageArchiveSink` 仅为
预留占位（未实测），默认不装配，避免"看起来支持对象存储"的假象。

接口预留的目的：W07 之后接入 S3/MinIO Object Lock 时**不需要改审计业务代码**。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, Mapping, Protocol, Sequence, runtime_checkable
from urllib.parse import quote, urlsplit

import httpx

from core.errors import RequestInvalidError
from core.logging import get_logger
from core.security.canonical import CanonicalizationError, canonical_json, canonical_digest, sha256_hex

logger = get_logger(__name__)

ARCHIVE_RECORD_VERSION = "w06-v1"


@dataclass(frozen=True)
class ArchiveRef:
    """一次追加产生的引用；`root` 是该批次累计链根，可用于外部锚定。"""

    location: str
    sequence: int
    count: int
    root: str
    previous_root: str | None


@runtime_checkable
class AuditArchivePort(Protocol):
    """归档端口：只追加，不可改写；`verify` 用于事后核对。"""

    def append(self, records: Sequence[Mapping[str, Any]]) -> ArchiveRef:
        """追加一批审计记录并返回链根引用。"""
        ...

    def verify(self, ref: ArchiveRef) -> bool:
        """核对归档内容是否仍与 `ref` 记录的链根一致。"""
        ...


def _reject_datetime(value: Any) -> Any:
    """把 datetime 归一化为 ISO-8601 字符串：规范化的输入纪律要求先转字符串。"""
    if isinstance(value, datetime):
        from core.security.canonical import normalize_timestamp

        return normalize_timestamp(value)
    return value


class LocalArchiveSink:
    """本地追加式文件归档：每批一行 JSON，行内保存链根，可离线校验。

    - 只以 `a` 模式追加，不提供任何改写/删除方法；
    - 每批 `root = sha256(canonical_json({version, sequence, previous_root, records}))`；
    - 该实现**不阻止**有文件系统权限的人改写文件——它只保证"篡改可发现"，
      因此不得表述为 WORM（ADR-0006 已知限制 1/6）。
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    # ---- 端口实现 ------------------------------------------------------
    def append(self, records: Sequence[Mapping[str, Any]]) -> ArchiveRef:
        if not records:
            raise RequestInvalidError("归档批次不能为空")
        batch = [self._normalize(record) for record in records]
        sequence, previous_root = self._head()
        root = self._root(sequence=sequence, previous_root=previous_root, records=batch)
        node = {
            "archive_version": ARCHIVE_RECORD_VERSION,
            "sequence": sequence,
            "previous_root": previous_root,
            "root": root,
            "count": len(batch),
            "records": batch,
        }
        line = canonical_json(node)
        with self._path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return ArchiveRef(
            location=str(self._path),
            sequence=sequence,
            count=len(batch),
            root=root,
            previous_root=previous_root,
        )

    def verify(self, ref: ArchiveRef) -> bool:
        """重算链根并与 `ref` 比对；任何结构损坏都返回 False（fail closed）。"""
        nodes = self._read_nodes()
        if ref.sequence < 0 or ref.sequence >= len(nodes):
            return False
        previous_root: str | None = None
        for index, node in enumerate(nodes):
            records = node.get("records")
            if not isinstance(records, list):
                return False
            expected = self._root(
                sequence=index, previous_root=previous_root, records=records
            )
            if node.get("root") != expected or node.get("previous_root") != previous_root:
                return False
            if index == ref.sequence:
                return node.get("root") == ref.root and node.get("count") == ref.count
            previous_root = expected
        return False

    # ---- 内部 ----------------------------------------------------------
    @staticmethod
    def _normalize(record: Mapping[str, Any]) -> dict[str, Any]:
        """落盘前把 datetime 转字符串；其余非法类型由规范化器直接拒绝。"""
        normalized: dict[str, Any] = {}
        for key, value in record.items():
            if not isinstance(key, str):
                raise RequestInvalidError("归档记录键必须是字符串")
            normalized[key] = _reject_datetime(value)
        return normalized

    @staticmethod
    def _root(
        *, sequence: int, previous_root: str | None, records: Sequence[Mapping[str, Any]]
    ) -> str:
        try:
            return canonical_digest(
                {
                    "archive_version": ARCHIVE_RECORD_VERSION,
                    "sequence": sequence,
                    "previous_root": previous_root,
                    "records": list(records),
                }
            )
        except CanonicalizationError as exc:
            raise RequestInvalidError(
                "归档记录无法规范化，拒绝写入", details={"reason": exc.message}
            ) from None

    def _read_nodes(self) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        nodes: list[dict[str, Any]] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                parsed = json.loads(line)
            except ValueError:
                return nodes + [{"records": None}]
            if not isinstance(parsed, dict):
                return nodes + [{"records": None}]
            nodes.append(parsed)
        return nodes

    def _head(self) -> tuple[int, str | None]:
        nodes = self._read_nodes()
        if not nodes:
            return 0, None
        last = nodes[-1]
        root = last.get("root")
        return len(nodes), str(root) if root else None


class ObjectStorageArchiveSink:
    """对象存储（S3 Object Lock / MinIO）归档**占位实现**。

    默认构造不装配，直接抛 `NotImplementedError` 保持 ADR-0006 §5 既有边界。
    已配置 S3/MinIO Object Lock 时请显式使用 :class:`S3ObjectLockArchiveSink`。
    """

    def __init__(self, *_: Any, **__: Any) -> None:
        raise NotImplementedError(
            "ObjectStorageArchiveSink 默认占位不装配；请使用显式配置的 S3ObjectLockArchiveSink"
        )


ObjectLockMode = Literal["GOVERNANCE", "COMPLIANCE"]


@dataclass(frozen=True)
class S3ObjectLockConfig:
    """S3/MinIO Object Lock 归档配置（P4-T2）。"""

    endpoint_url: str
    bucket: str
    access_key: str
    secret_key: str
    region: str = "us-east-1"
    prefix: str = "audit-worm"
    retention_mode: ObjectLockMode = "COMPLIANCE"
    retention_days: int = 30
    timeout_s: float = 10.0

    def __post_init__(self) -> None:
        if not self.endpoint_url.strip():
            raise RequestInvalidError("endpoint_url 不能为空")
        if not self.bucket.strip():
            raise RequestInvalidError("bucket 不能为空")
        if self.retention_mode not in ("GOVERNANCE", "COMPLIANCE"):
            raise RequestInvalidError(
                "retention_mode 仅支持 GOVERNANCE 或 COMPLIANCE",
                details={"retention_mode": self.retention_mode},
            )
        if self.retention_days < 1:
            raise RequestInvalidError("retention_days 必须 >= 1")


def _sign_sigv4(
    *,
    method: str,
    url: str,
    headers: Mapping[str, str],
    payload: bytes,
    access_key: str,
    secret_key: str,
    region: str,
    service: str = "s3",
    now: datetime | None = None,
) -> dict[str, str]:
    """AWS Signature Version 4（零第三方 SDK，纯标准库 hmac/hashlib）。"""
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    amz_date = moment.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = moment.strftime("%Y%m%d")

    parsed = urlsplit(url)
    canonical_uri = quote(parsed.path or "/", safe="/~")
    canonical_querystring = parsed.query
    payload_hash = hashlib.sha256(payload).hexdigest()

    merged: dict[str, str] = {k.lower(): str(v).strip() for k, v in headers.items()}
    merged["host"] = parsed.netloc
    merged["x-amz-date"] = amz_date
    merged["x-amz-content-sha256"] = payload_hash

    sorted_keys = sorted(merged)
    canonical_headers = "".join(f"{k}:{merged[k]}\n" for k in sorted_keys)
    signed_headers = ";".join(sorted_keys)
    canonical_request = "\n".join(
        [
            method.upper(),
            canonical_uri,
            canonical_querystring,
            canonical_headers,
            signed_headers,
            payload_hash,
        ]
    )

    credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            credential_scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )

    def _hmac(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

    k_date = _hmac(f"AWS4{secret_key}".encode("utf-8"), date_stamp)
    k_region = _hmac(k_date, region)
    k_service = _hmac(k_region, service)
    k_signing = _hmac(k_service, "aws4_request")
    signature = hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    out = dict(headers)
    out["Host"] = parsed.netloc
    out["x-amz-date"] = amz_date
    out["x-amz-content-sha256"] = payload_hash
    out["Authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return out


class S3ObjectLockArchiveSink:
    """P4-T2：S3/MinIO Object Lock 合规 WORM 归档实现。

    每批写入带 `Content-MD5`、`x-amz-object-lock-mode` 与
    `x-amz-object-lock-retain-until-date`；`verify()` 同时校验远端对象
    哈希链根与 Object Lock 保留期响应头，任一缺失/篡改即返回 False。
    """

    def __init__(
        self,
        config: S3ObjectLockConfig,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._cfg = config
        self._endpoint = config.endpoint_url.rstrip("/")
        self._prefix = config.prefix.strip("/")
        self._client = httpx.Client(timeout=config.timeout_s, transport=transport)
        self._sequence = 0
        self._previous_root: str | None = None

    def close(self) -> None:
        self._client.close()

    def _object_key(self, sequence: int) -> str:
        name = f"batch-{sequence:08d}.json"
        return f"{self._prefix}/{name}" if self._prefix else name

    def _object_url(self, key: str) -> str:
        return f"{self._endpoint}/{self._cfg.bucket}/{key.lstrip('/')}"

    def _bucket_url(self) -> str:
        return f"{self._endpoint}/{self._cfg.bucket}"

    def _request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        content: bytes = b"",
    ) -> httpx.Response:
        signed = _sign_sigv4(
            method=method,
            url=url,
            headers=headers or {},
            payload=content,
            access_key=self._cfg.access_key,
            secret_key=self._cfg.secret_key,
            region=self._cfg.region,
        )
        return self._client.request(method, url, headers=signed, content=content)

    def ensure_bucket_with_object_lock(self) -> int:
        """创建启用 Object Lock 的桶（幂等：200/409 均视为可用）。"""
        resp = self._request(
            "PUT",
            self._bucket_url(),
            headers={"x-amz-bucket-object-lock-enabled": "true"},
        )
        if resp.status_code not in (200, 204, 409):
            raise RequestInvalidError(
                f"创建 Object Lock 桶失败：HTTP {resp.status_code}",
                details={"status_code": resp.status_code, "bucket": self._cfg.bucket},
            )
        return resp.status_code

    def append(self, records: Sequence[Mapping[str, Any]]) -> ArchiveRef:
        if not records:
            raise RequestInvalidError("归档批次不能为空")
        batch = [LocalArchiveSink._normalize(record) for record in records]
        sequence = self._sequence
        previous_root = self._previous_root
        root = LocalArchiveSink._root(
            sequence=sequence, previous_root=previous_root, records=batch
        )
        node = {
            "archive_version": ARCHIVE_RECORD_VERSION,
            "sequence": sequence,
            "previous_root": previous_root,
            "root": root,
            "count": len(batch),
            "records": batch,
        }
        body = (canonical_json(node) + "\n").encode("utf-8")
        content_md5 = base64.b64encode(hashlib.md5(body).digest()).decode("ascii")
        retain_until = (
            datetime.now(timezone.utc) + timedelta(days=self._cfg.retention_days)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

        key = self._object_key(sequence)
        url = self._object_url(key)
        resp = self._request(
            "PUT",
            url,
            headers={
                "Content-Type": "application/json",
                "Content-MD5": content_md5,
                "x-amz-object-lock-mode": self._cfg.retention_mode,
                "x-amz-object-lock-retain-until-date": retain_until,
            },
            content=body,
        )
        if resp.status_code not in (200, 201):
            raise RequestInvalidError(
                f"WORM 归档写入失败：HTTP {resp.status_code}",
                details={"status_code": resp.status_code, "key": key},
            )

        self._sequence = sequence + 1
        self._previous_root = root
        return ArchiveRef(
            location=f"s3://{self._cfg.bucket}/{key}",
            sequence=sequence,
            count=len(batch),
            root=root,
            previous_root=previous_root,
        )

    def _url_from_ref(self, ref: ArchiveRef) -> str:
        prefix = f"s3://{self._cfg.bucket}/"
        if ref.location.startswith(prefix):
            return self._object_url(ref.location[len(prefix):])
        return self._object_url(self._object_key(ref.sequence))

    def verify(self, ref: ArchiveRef) -> bool:
        """核对远端对象内容链根与 Object Lock 保留期头均有效。"""
        url = self._url_from_ref(ref)
        head = self._request("HEAD", url)
        if head.status_code != 200:
            return False
        mode = head.headers.get("x-amz-object-lock-mode", "").upper()
        retain = head.headers.get("x-amz-object-lock-retain-until-date", "")
        if mode != self._cfg.retention_mode or not retain:
            return False

        get_resp = self._request("GET", url)
        if get_resp.status_code != 200:
            return False
        try:
            node = json.loads(get_resp.text)
        except ValueError:
            return False
        if not isinstance(node, dict) or not isinstance(node.get("records"), list):
            return False
        recomputed = LocalArchiveSink._root(
            sequence=int(node.get("sequence", -1)),
            previous_root=node.get("previous_root"),
            records=node["records"],
        )
        return (
            recomputed == ref.root
            and node.get("root") == ref.root
            and node.get("previous_root") == ref.previous_root
            and int(node.get("count", -1)) == ref.count
        )

    def attempt_delete(self, ref: ArchiveRef) -> int:
        """合规负测辅助：尝试删除保留期内对象（Object Lock 生效时应返回 403）。"""
        url = self._url_from_ref(ref)
        # 带 versionId（若远端开启版本化）才能触发真正的永久删除拦截；无版本则普通 DELETE。
        head = self._request("HEAD", url)
        version_id = head.headers.get("x-amz-version-id")
        target_url = f"{url}?versionId={quote(version_id, safe='')}" if version_id else url
        resp = self._request("DELETE", target_url)
        return resp.status_code


def root_digest_of(records: Sequence[Mapping[str, Any]]) -> str:
    """对一组记录取链根摘要（供外部锚定使用），统一走规范化入口。"""
    return sha256_hex(canonical_json([dict(record) for record in records]))


__all__ = [
    "ARCHIVE_RECORD_VERSION",
    "ArchiveRef",
    "AuditArchivePort",
    "LocalArchiveSink",
    "ObjectStorageArchiveSink",
    "S3ObjectLockArchiveSink",
    "S3ObjectLockConfig",
    "root_digest_of",
]
