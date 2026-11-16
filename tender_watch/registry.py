"""OCI Distribution 只读接口的路径约定与传输封装。

传输层只负责按 origin 相对路径发起 GET 并把原始响应交给归档，
不解释业务语义，也不跟随标签之外的任何重写。路径拼接保持显式，
使调用方无法借描述符跳出声明的仓库（可见性边界在 walker 层强制）。
"""
import re
from dataclasses import dataclass

from .client import Client

OCI_INDEX_MEDIA_TYPE = "application/vnd.oci.image.index.v1+json"
OCI_MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"
DOCKER_INDEX_MEDIA_TYPE = "application/vnd.docker.distribution.manifest.list.v2+json"
DOCKER_MANIFEST_MEDIA_TYPE = "application/vnd.docker.distribution.manifest.v2+json"
MANIFEST_MEDIA_TYPES = frozenset({
    OCI_INDEX_MEDIA_TYPE,
    OCI_MANIFEST_MEDIA_TYPE,
    DOCKER_INDEX_MEDIA_TYPE,
    DOCKER_MANIFEST_MEDIA_TYPE,
})
# 只接受这两种清单结构；未知媒体类型的负载不会被猜测解析。
INDEX_MEDIA_TYPES = frozenset({OCI_INDEX_MEDIA_TYPE, DOCKER_INDEX_MEDIA_TYPE})
IMAGE_MEDIA_TYPES = frozenset({OCI_MANIFEST_MEDIA_TYPE, DOCKER_MANIFEST_MEDIA_TYPE})

REFERRERS_ACCEPT = (
    "application/vnd.oci.image.index.v1+json, "
    "application/vnd.docker.distribution.manifest.list.v2+json"
)
MANIFEST_ACCEPT = ", ".join(sorted(MANIFEST_MEDIA_TYPES))

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
# OCI 仓库名：小写段，允许点、下划线，段间单斜杠，整体不超过 255 字符。
_REPO_RE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*$")


def digest_of(body):
    """按 OCI 摘要口径计算字节负载的 sha256 摘要。"""
    import hashlib
    return "sha256:" + hashlib.sha256(body).hexdigest()


def parse_digest(reference):
    if not isinstance(reference, str) or not _DIGEST_RE.match(reference):
        raise ValueError("invalid sha256 digest: " + repr(reference))
    return reference


def validate_tag(tag):
    if not isinstance(tag, str) or not _TAG_RE.match(tag):
        raise ValueError("invalid tag: " + repr(tag))
    return tag


def validate_repository(repository):
    if not isinstance(repository, str) or len(repository) > 255 or not _REPO_RE.match(repository):
        raise ValueError("invalid repository: " + repr(repository))
    return repository


def manifest_path(repository, reference):
    validate_repository(repository)
    if reference.startswith("sha256:"):
        parse_digest(reference)
    else:
        validate_tag(reference)
    return f"/v2/{repository}/manifests/{reference}"


def blob_path(repository, digest):
    validate_repository(repository)
    parse_digest(digest)
    return f"/v2/{repository}/blobs/{digest}"


def referrers_path(repository, digest, artifact_type=None):
    validate_repository(repository)
    parse_digest(digest)
    path = f"/v2/{repository}/referrers/{digest}"
    if artifact_type:
        from urllib.parse import quote
        path += "?artifactType=" + quote(artifact_type, safe="")
    return path


def fallback_tag(digest):
    """兼容引用清单的标签约定：sha256-<hex>。"""
    parse_digest(digest)
    return "sha256-" + digest.split(":", 1)[1]


def normalize_content_type(value):
    return (value or "").partition(";")[0].strip().lower()


@dataclass(frozen=True)
class Fetched:
    """一次 GET 的结果：路径、最终 URL、状态、头部与原始字节。"""
    path: str
    url: str
    status: int
    headers: dict
    body: bytes
    capture_id: int


class RegistryClient:
    """在既有 Client/Archive 之上的只读 OCI 传输封装。

    每次 GET 的原始响应都经 Archive 追加保存，按字节摘要复用 body，
    但每次来源观察（路径、状态、头部、时间）独立留痕，仓库来源不丢。
    """

    def __init__(self, client, archive, source):
        self.client = client
        self.archive = archive
        self.source = source

    def get(self, path, headers=None):
        response = self.client.fetch(path, headers=headers)
        capture_id = self.archive.append(
            self.source, response.url, response.status, response.headers, response.body
        )
        return Fetched(
            path=path,
            url=response.url,
            status=response.status,
            headers=response.headers,
            body=response.body,
            capture_id=capture_id,
        )

    def get_manifest(self, repository, reference):
        return self.get(manifest_path(repository, reference),
                        headers={"Accept": MANIFEST_ACCEPT})

    def get_blob(self, repository, digest):
        return self.get(blob_path(repository, digest))

    def get_referrers(self, repository, digest, artifact_type=None):
        return self.get(referrers_path(repository, digest, artifact_type),
                        headers={"Accept": REFERRERS_ACCEPT})
