"""OCI Distribution 只读接口的常量、摘要规则与选择策略。

本模块只处理协议层面的纯函数：路径构造、摘要校验、媒体类型判定、
平台选择。所有网络访问由 :class:`tender_watch.client.Client` 完成，
这里不发起任何请求，也不解释合成字节之外的内容。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

OCI_INDEX = "application/vnd.oci.image.index.v1+json"
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
OCI_ARTIFACT = "application/vnd.oci.artifact.manifest.v1+json"
OCI_CONFIG = "application/vnd.oci.image.config.v1+json"
DOCKER_INDEX = "application/vnd.docker.distribution.manifest.list.v2+json"
DOCKER_MANIFEST = "application/vnd.docker.distribution.manifest.v2+json"

# 走 /manifests/ 端点的文档类型；其余描述符一律走 /blobs/。
MANIFEST_MEDIA_TYPES = frozenset({
    OCI_INDEX, OCI_MANIFEST, OCI_ARTIFACT,
    DOCKER_INDEX, DOCKER_MANIFEST,
})
ACCEPT_MANIFESTS = ", ".join(sorted(MANIFEST_MEDIA_TYPES))

# 拒收/缺口原因码，集中声明，便于验收组按码检索。
REASON_HTTP_STATUS = "http_error_status"
REASON_TRANSPORT = "transport_error"
REASON_SIZE_MISMATCH = "size_mismatch"
REASON_DIGEST_MISMATCH = "digest_mismatch"
REASON_DIGEST_HEADER_MISMATCH = "digest_header_mismatch"
REASON_MALFORMED = "malformed_json"
REASON_KIND_MISMATCH = "document_kind_mismatch"
REASON_UNKNOWN_DIGEST_ALGORITHM = "unknown_digest_algorithm"
REASON_OBSERVED_MEDIA_TYPE_DIFFERS = "observed_media_type_differs"
REASON_SUBJECT_MISSING = "subject_missing"
REASON_SUBJECT_MISMATCH = "subject_mismatch"
REASON_PLATFORM_NOT_FOUND = "platform_not_found"
REASON_REQUIRED_ARTIFACT_ABSENT = "required_artifact_absent"
REASON_EXTERNAL_URL_NOT_FOLLOWED = "external_url_not_followed"

_DIGEST_RE = re.compile(r"^[a-z0-9]+(?:[.+_-][a-z0-9]+)*:[a-zA-Z0-9=_-]+$")
_KNOWN_ALGORITHMS = {"sha256": hashlib.sha256, "sha512": hashlib.sha512}


def valid_digest(digest: Any) -> bool:
    """判断是否为 OCI 规范形式的 digest（algorithm:encoded）。"""
    if not isinstance(digest, str) or not _DIGEST_RE.match(digest):
        return False
    algorithm, encoded = digest.split(":", 1)
    if algorithm not in _KNOWN_ALGORITHMS:
        return False
    if algorithm == "sha256":
        return re.fullmatch(r"[0-9a-f]{64}", encoded) is not None
    if algorithm == "sha512":
        return re.fullmatch(r"[0-9a-f]{128}", encoded) is not None
    return False


def digest_of(body: bytes, algorithm: str = "sha256") -> str:
    return algorithm + ":" + _KNOWN_ALGORITHMS[algorithm](body).hexdigest()


def verify_digest(digest: str, body: bytes) -> tuple[bool, str | None]:
    """返回（是否通过, 失败原因码）。未知算法本身即拒收原因。"""
    if not valid_digest(digest):
        algorithm = digest.split(":", 1)[0] if isinstance(digest, str) and ":" in digest else ""
        if algorithm not in _KNOWN_ALGORITHMS:
            return False, REASON_UNKNOWN_DIGEST_ALGORITHM
        return False, REASON_DIGEST_MISMATCH
    if digest_of(body, digest.split(":", 1)[0]) != digest:
        return False, REASON_DIGEST_MISMATCH
    return True, None


def manifest_path(repository: str, reference: str) -> str:
    return f"/v2/{repository}/manifests/{reference}"


def blob_path(repository: str, digest: str) -> str:
    return f"/v2/{repository}/blobs/{digest}"


def referrers_path(repository: str, digest: str) -> str:
    return f"/v2/{repository}/referrers/{digest}"


def fallback_reference(digest: str) -> str:
    """OCI Distribution 规范的兼容 tag：把冒号换成短横线。"""
    return digest.replace(":", "-", 1)


def is_manifest_media_type(media_type: str | None) -> bool:
    return media_type in MANIFEST_MEDIA_TYPES


def structural_kind(document: dict[str, Any]) -> str:
    """按文档结构判定种类，不信任单一声明字段。"""
    if isinstance(document.get("manifests"), list):
        return OCI_INDEX
    if isinstance(document.get("config"), dict) or isinstance(document.get("layers"), list):
        return OCI_MANIFEST
    if isinstance(document.get("blobs"), list) or "artifactType" in document:
        return OCI_ARTIFACT
    raise ValueError("unrecognized OCI document structure")


def parse_document(body: bytes) -> dict[str, Any]:
    try:
        document = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(REASON_MALFORMED) from error
    if not isinstance(document, dict):
        raise ValueError(REASON_MALFORMED)
    return document


def base_media_type(content_type: str | None) -> str:
    """去掉 Content-Type 的参数部分，只保留媒体类型本体。"""
    if not content_type:
        return ""
    return content_type.split(";", 1)[0].strip().lower()


@dataclass(frozen=True)
class Platform:
    """验收请求的一个平台选择器，形如 linux/amd64 或 linux/arm64/v8。"""

    os: str
    architecture: str
    variant: str | None = None

    @classmethod
    def parse(cls, text: str) -> "Platform":
        parts = text.strip().split("/")
        if len(parts) not in (2, 3) or not all(parts):
            raise ValueError(f"platform must look like os/architecture[/variant]: {text!r}")
        return cls(parts[0], parts[1], parts[2] if len(parts) == 3 else None)

    def matches(self, descriptor: dict[str, Any]) -> bool:
        platform = descriptor.get("platform")
        if not isinstance(platform, dict):
            return False
        if platform.get("os") != self.os or platform.get("architecture") != self.architecture:
            return False
        if self.variant is not None and platform.get("variant") != self.variant:
            return False
        return True

    def as_text(self) -> str:
        return "/".join((self.os, self.architecture, self.variant or "")).rstrip("/")


@dataclass(frozen=True)
class Policy:
    """一次验收的本地策略。

    platforms 为空时，index 中所有带 platform 的条目都必须领取；
    required_artifact_types 声明必须随附的关联制品类型（如交付说明）；
    referrers 控制 referrers API 缺失时是否使用兼容 tag 索引。
    """

    platforms: tuple[Platform, ...] = ()
    required_artifact_types: frozenset[str] = frozenset()
    referrers: str = "api"  # api | fallback | skip
    drift_probe: bool = False

    def __post_init__(self) -> None:
        if self.referrers not in ("api", "fallback", "skip"):
            raise ValueError("referrers policy must be one of api, fallback, skip")

    def to_json(self) -> str:
        return json.dumps({
            "platforms": [platform.as_text() for platform in self.platforms],
            "required_artifact_types": sorted(self.required_artifact_types),
            "referrers": self.referrers,
            "drift_probe": self.drift_probe,
        }, ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "Policy":
        data = json.loads(text)
        return cls(
            platforms=tuple(Platform.parse(item) for item in data.get("platforms", [])),
            required_artifact_types=frozenset(data.get("required_artifact_types", [])),
            referrers=data.get("referrers", "api"),
            drift_probe=bool(data.get("drift_probe", False)),
        )
