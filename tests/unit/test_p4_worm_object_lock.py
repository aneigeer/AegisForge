"""P4-T2 单元测试：S3/MinIO Object Lock WORM 归档与 SigV4 签名。"""

from __future__ import annotations

import base64
import hashlib
import json

import httpx
import pytest

from core.audit.worm import (
    ArchiveRef,
    AuditArchivePort,
    ObjectStorageArchiveSink,
    S3ObjectLockArchiveSink,
    S3ObjectLockConfig,
)
from core.errors import RequestInvalidError


class _InMemoryObjectLockTransport(httpx.BaseTransport):
    """模拟支持 S3 Object Lock COMPLIANCE 保留期拒绝删除的对象存储。"""

    def __init__(self) -> None:
        self.buckets: dict[str, bool] = {}
        self.objects: dict[str, dict] = {}
        self.seen_requests: list[tuple[str, str, dict[str, str]]] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.lstrip("/")
        headers = {k.lower(): v for k, v in request.headers.items()}
        self.seen_requests.append((request.method, path, headers))
        assert "authorization" in headers and headers["authorization"].startswith("AWS4-HMAC-SHA256 ")
        assert "x-amz-date" in headers
        assert "x-amz-content-sha256" in headers

        if "/" not in path:
            if request.method == "PUT":
                self.buckets[path] = headers.get("x-amz-bucket-object-lock-enabled") == "true"
                return httpx.Response(200)
            return httpx.Response(404)

        if request.method == "PUT":
            body = request.content
            expected_md5 = base64.b64encode(hashlib.md5(body).digest()).decode("ascii")
            if headers.get("content-md5") != expected_md5:
                return httpx.Response(400, text="BadDigest")
            self.objects[path] = {
                "body": body,
                "mode": headers.get("x-amz-object-lock-mode", ""),
                "retain": headers.get("x-amz-object-lock-retain-until-date", ""),
                "version_id": "v1-immutable",
            }
            return httpx.Response(200, headers={"x-amz-version-id": "v1-immutable"})

        obj = self.objects.get(path)
        if obj is None:
            return httpx.Response(404)

        if request.method == "HEAD":
            return httpx.Response(
                200,
                headers={
                    "x-amz-object-lock-mode": obj["mode"],
                    "x-amz-object-lock-retain-until-date": obj["retain"],
                    "x-amz-version-id": obj["version_id"],
                },
            )

        if request.method == "GET":
            return httpx.Response(200, content=obj["body"])

        if request.method == "DELETE":
            if obj["mode"] in ("COMPLIANCE", "GOVERNANCE") and obj["retain"]:
                return httpx.Response(403, text="AccessDenied: Object Lock active")
            del self.objects[path]
            return httpx.Response(204)

        return httpx.Response(405)


def _cfg() -> S3ObjectLockConfig:
    return S3ObjectLockConfig(
        endpoint_url="http://127.0.0.1:9000",
        bucket="dg-audit-worm",
        access_key="minioadmin",
        secret_key="minioadmin",
        retention_mode="COMPLIANCE",
        retention_days=30,
    )


def test_placeholder_sink_still_raises_not_implemented() -> None:
    with pytest.raises(NotImplementedError):
        ObjectStorageArchiveSink()


def test_s3_object_lock_append_verify_and_delete_blocked() -> None:
    transport = _InMemoryObjectLockTransport()
    sink = S3ObjectLockArchiveSink(_cfg(), transport=transport)
    assert isinstance(sink, AuditArchivePort)

    assert sink.ensure_bucket_with_object_lock() == 200
    assert transport.buckets["dg-audit-worm"] is True

    ref0 = sink.append([{"event_id": "e1", "seq": 1, "action": "APPROVAL_CREATED"}])
    ref1 = sink.append([{"event_id": "e2", "seq": 2, "action": "APPROVAL_CONSUMED"}])

    assert ref0.sequence == 0
    assert ref1.sequence == 1
    assert ref1.previous_root == ref0.root
    assert ref0.location.startswith("s3://dg-audit-worm/audit-worm/batch-00000000.json")

    assert sink.verify(ref0) is True
    assert sink.verify(ref1) is True
    # 合规保留期内 DELETE 必须被 403 拒绝
    assert sink.attempt_delete(ref0) == 403
    assert sink.verify(ref0) is True
    sink.close()


def test_s3_object_lock_verify_fails_on_tamper_or_missing_retention() -> None:
    transport = _InMemoryObjectLockTransport()
    sink = S3ObjectLockArchiveSink(_cfg(), transport=transport)
    ref = sink.append([{"event_id": "e1", "seq": 1}])
    key = "dg-audit-worm/audit-worm/batch-00000000.json"

    # 1) 移除保留期头 → 校验失败
    saved_retain = transport.objects[key]["retain"]
    transport.objects[key]["retain"] = ""
    assert sink.verify(ref) is False
    transport.objects[key]["retain"] = saved_retain
    assert sink.verify(ref) is True

    # 2) 篡改正文 → 哈希链根失配 → 校验失败
    tampered = json.loads(transport.objects[key]["body"].decode("utf-8"))
    tampered["records"][0]["seq"] = 999
    transport.objects[key]["body"] = json.dumps(tampered).encode("utf-8")
    assert sink.verify(ref) is False
    sink.close()


def test_s3_object_lock_rejects_invalid_config_and_empty_batch() -> None:
    with pytest.raises(RequestInvalidError):
        S3ObjectLockConfig(
            endpoint_url="http://127.0.0.1:9000",
            bucket="b",
            access_key="k",
            secret_key="s",
            retention_days=0,
        )
    sink = S3ObjectLockArchiveSink(_cfg(), transport=_InMemoryObjectLockTransport())
    with pytest.raises(RequestInvalidError):
        sink.append([])
    assert sink.verify(ArchiveRef("s3://dg-audit-worm/missing.json", 99, 1, "0" * 64, None)) is False
    sink.close()
