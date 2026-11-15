"""OCI 交付物验收：标签冻结、描述符闭包与关联制品领取。

设计边界：
- 所有 HTTP 交换仍由 :class:`tender_watch.client.Client` 发起，并通过
  :class:`tender_watch.archive.Archive` 只追加留痕；相同字节按摘要复用，
  但每次观察的 URL、状态码、头部与时间各自保留。
- 标签只在首次冻结时请求一次；冻结后一切引用走 digest，恢复运行也不
  再解析标签，可选的漂移探针只记录观察结果，不改写冻结视图。
- 本模块不执行、不解包任何下载内容，只做 JSON 结构解析与摘要/长度校验。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.error import URLError

from . import oci
from .archive import Archive
from .client import Client

# 节点角色与状态：验收报告直接按这些值检索。
ROLE_ROOT = "root"
ROLE_PLATFORM_MANIFEST = "platform_manifest"
ROLE_INDEX_ENTRY = "index_entry"
ROLE_CONFIG = "config"
ROLE_LAYER = "layer"
ROLE_REFERRER_MANIFEST = "referrer_manifest"
ROLE_ARTIFACT_BLOB = "artifact_blob"

STATE_PENDING = "pending"
STATE_PRESENT = "present"
STATE_MISSING = "missing"
STATE_REJECTED = "rejected"
STATE_VISIBLE_NOT_REQUIRED = "visible_not_required"

RUNNING = "running"
ACCEPTED = "accepted"
INCOMPLETE = "incomplete"
REJECTED_STATUS = "rejected"

_DECLARED_ROOT_KIND = {
    oci.OCI_INDEX: oci.OCI_INDEX,
    oci.DOCKER_INDEX: oci.OCI_INDEX,
    oci.OCI_MANIFEST: oci.OCI_MANIFEST,
    oci.DOCKER_MANIFEST: oci.OCI_MANIFEST,
    oci.OCI_ARTIFACT: oci.OCI_ARTIFACT,
}

_DDL = """
CREATE TABLE IF NOT EXISTS acceptances (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    registry TEXT NOT NULL,
    repository TEXT NOT NULL,
    reference TEXT NOT NULL,
    policy TEXT NOT NULL,
    root_digest TEXT,
    referrers_state TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    finalized_at TEXT
);
CREATE TABLE IF NOT EXISTS acceptance_nodes (
    acceptance_id INTEGER NOT NULL REFERENCES acceptances(id),
    digest TEXT NOT NULL,
    role TEXT NOT NULL,
    media_type TEXT,
    size INTEGER,
    kind TEXT,
    repository TEXT NOT NULL,
    required INTEGER NOT NULL DEFAULT 1,
    state TEXT NOT NULL,
    url TEXT,
    capture_id INTEGER REFERENCES captures(id),
    reasons TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (acceptance_id, digest)
);
CREATE TABLE IF NOT EXISTS acceptance_edges (
    acceptance_id INTEGER NOT NULL REFERENCES acceptances(id),
    parent_digest TEXT NOT NULL,
    child_digest TEXT NOT NULL,
    relation TEXT NOT NULL,
    required INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (acceptance_id, parent_digest, child_digest, relation)
);
CREATE TABLE IF NOT EXISTS acceptance_events (
    id INTEGER PRIMARY KEY,
    acceptance_id INTEGER NOT NULL REFERENCES acceptances(id),
    sequence INTEGER NOT NULL,
    event TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
"""


class AcceptanceError(ValueError):
    """验收参数或恢复目标本身不合法。"""


@dataclass(frozen=True)
class Node:
    digest: str
    role: str
    media_type: str | None
    size: int | None
    kind: str | None
    repository: str
    required: bool
    state: str
    url: str | None
    capture_id: int | None
    reasons: list[dict[str, Any]]


class AcceptanceStore:
    """在原始归档库上管理验收视图；不持有任何网络能力。"""

    def __init__(self, archive: Archive):
        self.archive = archive
        self.archive.connection.executescript(_DDL)

    # ---- 验收单 -------------------------------------------------------
    def open(self, source: str, registry: str, repository: str, reference: str, policy: oci.Policy) -> int:
        if not source.strip() or not registry.strip() or not repository.strip() or not reference.strip():
            raise AcceptanceError("source, registry, repository and reference are required")
        if "/" not in repository or repository.startswith("/") or repository.endswith("/"):
            raise AcceptanceError("repository must look like vendor/tool")
        with self.archive.transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO acceptances(source,registry,repository,reference,policy) VALUES (?,?,?,?,?)",
                (source, registry, repository, reference, policy.to_json()),
            )
        return cursor.lastrowid

    def get(self, acceptance_id: int) -> dict[str, Any]:
        row = self.archive.connection.execute(
            "SELECT * FROM acceptances WHERE id=?", (acceptance_id,)
        ).fetchone()
        if row is None:
            raise AcceptanceError(f"unknown acceptance id: {acceptance_id}")
        data = dict(row)
        data["policy"] = oci.Policy.from_json(data["policy"])
        return data

    def list_all(self) -> list[dict[str, Any]]:
        rows = self.archive.connection.execute("SELECT * FROM acceptances ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def set_root(self, acceptance_id: int, digest: str) -> None:
        with self.archive.transaction() as connection:
            connection.execute(
                "UPDATE acceptances SET root_digest=? WHERE id=?", (digest, acceptance_id)
            )

    def set_referrers_state(self, acceptance_id: int, state: str) -> None:
        with self.archive.transaction() as connection:
            connection.execute(
                "UPDATE acceptances SET referrers_state=? WHERE id=?", (state, acceptance_id)
            )

    def finalize(self, acceptance_id: int, status: str) -> None:
        with self.archive.transaction() as connection:
            connection.execute(
                "UPDATE acceptances SET status=?, finalized_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
                (status, acceptance_id),
            )

    # ---- 事件流 -------------------------------------------------------
    def event(self, acceptance_id: int, name: str, detail: dict[str, Any] | None = None) -> None:
        with self.archive.transaction() as connection:
            sequence = connection.execute(
                "SELECT COALESCE(MAX(sequence),0)+1 FROM acceptance_events WHERE acceptance_id=?",
                (acceptance_id,),
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO acceptance_events(acceptance_id,sequence,event,detail) VALUES (?,?,?,?)",
                (acceptance_id, sequence, name, json.dumps(detail or {}, ensure_ascii=False, sort_keys=True)),
            )

    def events(self, acceptance_id: int) -> list[dict[str, Any]]:
        rows = self.archive.connection.execute(
            "SELECT * FROM acceptance_events WHERE acceptance_id=? ORDER BY sequence",
            (acceptance_id,),
        ).fetchall()
        return [
            {**dict(row), "detail": json.loads(row["detail"])}
            for row in rows
        ]

    # ---- 节点与边 -----------------------------------------------------
    def upsert_node(self, acceptance_id: int, digest: str, role: str, repository: str,
                    *, media_type: str | None = None, size: int | None = None,
                    required: bool = True, state: str = STATE_PENDING,
                    url: str | None = None, capture_id: int | None = None,
                    reasons: list[dict[str, Any]] | None = None,
                    kind: str | None = None) -> None:
        existing = self.archive.connection.execute(
            "SELECT state FROM acceptance_nodes WHERE acceptance_id=? AND digest=?",
            (acceptance_id, digest),
        ).fetchone()
        if existing is not None and existing["state"] in (STATE_REJECTED, STATE_MISSING):
            # 已有的缺口/拒收是终态观察，重复登记不得把它降级回 pending。
            state = existing["state"]
        with self.archive.transaction() as connection:
            connection.execute(
                """
                INSERT INTO acceptance_nodes(acceptance_id,digest,role,media_type,size,kind,
                                             repository,required,state,url,capture_id,reasons)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(acceptance_id,digest) DO UPDATE SET
                    role=excluded.role,
                    media_type=COALESCE(excluded.media_type, acceptance_nodes.media_type),
                    size=COALESCE(excluded.size, acceptance_nodes.size),
                    kind=COALESCE(excluded.kind, acceptance_nodes.kind),
                    repository=excluded.repository,
                    required=excluded.required,
                    state=excluded.state,
                    url=COALESCE(excluded.url, acceptance_nodes.url),
                    capture_id=COALESCE(excluded.capture_id, acceptance_nodes.capture_id),
                    reasons=excluded.reasons
                """,
                (acceptance_id, digest, role, media_type, size, kind, repository,
                 int(required), state, url, capture_id,
                 json.dumps(reasons or [], ensure_ascii=False, sort_keys=True)),
            )

    def update_node(self, acceptance_id: int, digest: str, *, state: str,
                    reasons: list[dict[str, Any]] | None = None,
                    url: str | None = None, capture_id: int | None = None,
                    kind: str | None = None) -> None:
        with self.archive.transaction() as connection:
            connection.execute(
                """
                UPDATE acceptance_nodes
                SET state=?, reasons=COALESCE(?, reasons), url=COALESCE(?,url),
                    capture_id=COALESCE(?,capture_id), kind=COALESCE(?,kind)
                WHERE acceptance_id=? AND digest=?
                """,
                (state,
                 json.dumps(reasons, ensure_ascii=False, sort_keys=True) if reasons is not None else None,
                 url, capture_id, kind, acceptance_id, digest),
            )

    def set_state(self, acceptance_id: int, digest: str, state: str) -> None:
        """只改状态，保留已累积的拒收原因与来源记录。"""
        with self.archive.transaction() as connection:
            connection.execute(
                "UPDATE acceptance_nodes SET state=? WHERE acceptance_id=? AND digest=?",
                (state, acceptance_id, digest),
            )

    def add_reason(self, acceptance_id: int, digest: str, reason: dict[str, Any]) -> None:
        row = self.archive.connection.execute(
            "SELECT reasons FROM acceptance_nodes WHERE acceptance_id=? AND digest=?",
            (acceptance_id, digest),
        ).fetchone()
        reasons = json.loads(row["reasons"]) if row else []
        if reason not in reasons:
            reasons.append(reason)
        with self.archive.transaction() as connection:
            connection.execute(
                "UPDATE acceptance_nodes SET reasons=? WHERE acceptance_id=? AND digest=?",
                (json.dumps(reasons, ensure_ascii=False, sort_keys=True), acceptance_id, digest),
            )

    def node(self, acceptance_id: int, digest: str) -> Node | None:
        row = self.archive.connection.execute(
            "SELECT * FROM acceptance_nodes WHERE acceptance_id=? AND digest=?",
            (acceptance_id, digest),
        ).fetchone()
        return self._to_node(row) if row else None

    def nodes(self, acceptance_id: int) -> list[Node]:
        rows = self.archive.connection.execute(
            "SELECT * FROM acceptance_nodes WHERE acceptance_id=? ORDER BY role, digest",
            (acceptance_id,),
        ).fetchall()
        return [self._to_node(row) for row in rows]

    def add_edge(self, acceptance_id: int, parent: str, child: str, relation: str,
                 required: bool = True) -> None:
        with self.archive.transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO acceptance_edges VALUES (?,?,?,?,?)",
                (acceptance_id, parent, child, relation, int(required)),
            )

    def edges(self, acceptance_id: int) -> list[dict[str, Any]]:
        rows = self.archive.connection.execute(
            "SELECT parent_digest,child_digest,relation,required FROM acceptance_edges "
            "WHERE acceptance_id=? ORDER BY parent_digest, relation, child_digest",
            (acceptance_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _to_node(row) -> Node:
        return Node(
            digest=row["digest"], role=row["role"], media_type=row["media_type"],
            size=row["size"], kind=row["kind"], repository=row["repository"],
            required=bool(row["required"]), state=row["state"], url=row["url"],
            capture_id=row["capture_id"], reasons=json.loads(row["reasons"]),
        )


class AcceptanceRunner:
    """执行一次（或恢复一次）验收；所有中间状态立即落库。"""

    def __init__(self, client: Client, archive: Archive):
        self.client = client
        self.archive = archive
        self.store = AcceptanceStore(archive)
        self.retry_missing = False  # 恢复运行时置真，允许重试确定性缺口。

    # ---- 公共入口 -----------------------------------------------------
    def start(self, *, source: str, registry: str, repository: str,
              reference: str, policy: oci.Policy) -> dict[str, Any]:
        acceptance_id = self.store.open(source, registry, repository, reference, policy)
        self.store.event(acceptance_id, "acceptance_opened", {
            "registry": registry, "repository": repository, "reference": reference,
        })
        return self.run(acceptance_id)

    def resume(self, acceptance_id: int) -> dict[str, Any]:
        acceptance = self.store.get(acceptance_id)
        if acceptance["status"] == ACCEPTED:
            # 已通过的冻结视图不可变：恢复只重放，不发起任何请求。
            # incomplete/rejected 允许针对冻结描述符重试缺口，但标签路径永不重访。
            return self.view(acceptance_id)
        self.retry_missing = True  # 确定性 404 缺口可能已被供方补齐。
        return self.run(acceptance_id)

    # ---- 主流程 -------------------------------------------------------
    def run(self, acceptance_id: int) -> dict[str, Any]:
        acceptance = self.store.get(acceptance_id)
        policy: oci.Policy = acceptance["policy"]
        repository = acceptance["repository"]
        try:
            self._freeze_root(acceptance_id, acceptance)
            self._collect_closure(acceptance_id, repository)
            self._check_image_manifest_platform(acceptance_id, policy)
            self._collect_referrers(acceptance_id, repository, policy)
            status = self._determine_status(acceptance_id, policy)
            if policy.drift_probe:
                self._drift_probe(acceptance_id, acceptance)
        except _TransportFailure as failure:
            # 传输层异常（连接断开等）已记录在节点/事件上，留待恢复重试。
            self.store.event(acceptance_id, "transport_interruption", {"error": str(failure)})
            status = self._determine_status(acceptance_id, policy)
        self.store.finalize(acceptance_id, status)
        self.store.event(acceptance_id, "acceptance_finalized", {"status": status})
        return self.view(acceptance_id)

    # ---- 1. 标签冻结 --------------------------------------------------
    def _freeze_root(self, acceptance_id: int, acceptance: dict[str, Any]) -> None:
        if acceptance["root_digest"]:
            return  # 已冻结：恢复路径绝不重新解析标签。
        response = self._request(
            acceptance_id,
            oci.manifest_path(acceptance["repository"], acceptance["reference"]),
            {"Accept": oci.ACCEPT_MANIFESTS},
        )
        if response is None or response.status != 200:
            status = None if response is None else response.status
            self.store.event(acceptance_id, "tag_freeze_failed", {
                "reference": acceptance["reference"], "status": status,
            })
            raise _TransportFailure(f"tag freeze failed: {acceptance['reference']}")
        root_digest = oci.digest_of(response.body)
        reasons: list[dict[str, Any]] = []
        header_digest = response.headers.get("docker-content-digest")
        if header_digest and header_digest != root_digest:
            reasons.append({"code": oci.REASON_DIGEST_HEADER_MISMATCH,
                            "header": header_digest, "observed": root_digest})
        declared = oci.base_media_type(response.headers.get("content-type"))
        try:
            document = oci.parse_document(response.body)
            kind = oci.structural_kind(document)
        except ValueError as error:
            self.store.upsert_node(
                acceptance_id, root_digest, ROLE_ROOT, acceptance["repository"],
                media_type=declared or None, size=len(response.body),
                url=response.url, capture_id=response.capture_id,
                reasons=reasons + [{"code": str(error)}], state=STATE_REJECTED,
            )
            self.store.event(acceptance_id, "root_rejected", {"digest": root_digest})
            return
        declared_kind = _DECLARED_ROOT_KIND.get(declared)
        if declared_kind is not None and declared_kind != kind:
            reasons.append({"code": oci.REASON_KIND_MISMATCH,
                            "declared": declared, "observed_kind": kind})
        self.store.upsert_node(
            acceptance_id, root_digest, ROLE_ROOT, acceptance["repository"],
            media_type=declared or None, size=len(response.body), kind=kind,
            url=response.url, capture_id=response.capture_id,
            reasons=reasons, state=STATE_PRESENT if not reasons else STATE_REJECTED,
        )
        self.store.set_root(acceptance_id, root_digest)
        self.store.event(acceptance_id, "tag_frozen", {
            "reference": acceptance["reference"], "digest": root_digest,
            "capture_id": response.capture_id, "url": response.url,
        })

    # ---- 2. 描述符闭包 ------------------------------------------------
    def _collect_closure(self, acceptance_id: int, repository: str) -> None:
        # 单次运行内每个描述符最多重试一次；恢复运行时 missing/rejected 的
        # 冻结描述符允许重新尝试（标签路径除外，它根本不在闭包内）。
        attempted: set[str] = set()
        for _ in range(1000):  # 描述符数量有限，护栏防止异常场景下死循环。
            progressed = False
            for node in self.store.nodes(acceptance_id):
                if node.state != STATE_PRESENT:
                    continue
                # 声明的 mediaType 或已解析出的文档 kind 都可作为展开依据，
                # 兼容描述符省略 mediaType 的情形。
                expandable = oci.is_manifest_media_type(node.media_type) or node.kind is not None
                if not expandable or self._has_outgoing(acceptance_id, node.digest):
                    continue
                self._expand(acceptance_id, node, repository)
                progressed = True
            for node in self.store.nodes(acceptance_id):
                if not (node.required and node.state in (STATE_PENDING, STATE_MISSING, STATE_REJECTED)
                        and node.digest not in attempted):
                    continue
                if any(reason.get("code") in self._SEMANTIC_REASONS for reason in node.reasons):
                    continue  # 语义级拒收是确定性结论，重取不会改变。
                transport_gap = any(r.get("code") == oci.REASON_TRANSPORT for r in node.reasons)
                # 已成功收到过（有 capture 且当前非缺口）的节点不重取；
                # 传输中断的缺口任何运行都可重试；
                # 确定性 404/非 200 缺口仅在恢复运行（retry_missing）时重试。
                if node.capture_id is not None and node.state != STATE_MISSING:
                    continue
                if (node.capture_id is not None and node.state == STATE_MISSING
                        and not transport_gap and not self.retry_missing):
                    continue
                attempted.add(node.digest)
                self._fetch_descriptor(acceptance_id, node)
                progressed = True
            if not progressed:
                return

    def _has_outgoing(self, acceptance_id: int, digest: str) -> bool:
        return bool(self.archive.connection.execute(
            "SELECT 1 FROM acceptance_edges WHERE acceptance_id=? AND parent_digest=? LIMIT 1",
            (acceptance_id, digest),
        ).fetchone())

    def _expand(self, acceptance_id: int, node: Node, repository: str) -> None:
        body = self.archive.body(node.capture_id)
        try:
            document = oci.parse_document(body)
            kind = oci.structural_kind(document)
        except ValueError as error:
            self.store.update_node(acceptance_id, node.digest, state=STATE_REJECTED,
                                   reasons=node.reasons + [{"code": str(error)}])
            return
        if kind == oci.OCI_INDEX:
            self._expand_index(acceptance_id, node, document, repository)
        elif kind == oci.OCI_MANIFEST:
            self._expand_image_manifest(acceptance_id, node, document, repository)
        else:
            self._expand_artifact_manifest(acceptance_id, node, document, repository)

    def _check_image_manifest_platform(self, acceptance_id: int, policy: oci.Policy) -> None:
        """单清单（无根 index）时，平台信息只能从 config 文档读出。"""
        acceptance = self.store.get(acceptance_id)
        root_digest = acceptance["root_digest"]
        if root_digest is None or not policy.platforms:
            return
        root = self.store.node(acceptance_id, root_digest)
        if root is None or root.kind != oci.OCI_MANIFEST:
            return
        config_node = next(
            (node for node in self.store.nodes(acceptance_id)
             if node.role == ROLE_CONFIG and node.state == STATE_PRESENT), None,
        )
        if config_node is None:
            return
        try:
            config = oci.parse_document(self.archive.body(config_node.capture_id))
        except ValueError:
            self.store.add_reason(acceptance_id, root_digest,
                                  {"code": oci.REASON_MALFORMED, "part": "config"})
            self.store.set_state(acceptance_id, root_digest, STATE_REJECTED)
            return
        descriptor = {"platform": {key: config.get(key) for key in ("os", "architecture", "variant")
                                   if config.get(key) is not None}}
        if not any(selector.matches(descriptor) for selector in policy.platforms):
            self.store.add_reason(acceptance_id, root_digest, {
                "code": oci.REASON_PLATFORM_NOT_FOUND,
                "selectors": [selector.as_text() for selector in policy.platforms],
                "observed": descriptor["platform"],
            })
            self.store.set_state(acceptance_id, root_digest, STATE_REJECTED)

    def _expand_index(self, acceptance_id: int, node: Node, document: dict, repository: str) -> None:
        policy = self.store.get(acceptance_id)["policy"]
        matched_selectors: set[str] = set()
        for entry in document.get("manifests", []):
            descriptor = entry if isinstance(entry, dict) else {}
            if not oci.valid_digest(descriptor.get("digest")) or not isinstance(descriptor.get("size"), int):
                self.store.event(acceptance_id, "malformed_descriptor", {
                    "parent": node.digest, "descriptor": descriptor,
                })
                self.store.add_reason(acceptance_id, node.digest,
                                      {"code": oci.REASON_MALFORMED, "descriptor": descriptor})
                self.store.update_node(acceptance_id, node.digest, state=STATE_REJECTED)
                continue
            digest = descriptor["digest"]
            has_platform = isinstance(descriptor.get("platform"), dict)
            if not has_platform:
                # 无平台条目不随本次选择领取，但保留可见性记录。
                self.store.upsert_node(
                    acceptance_id, digest, ROLE_INDEX_ENTRY, repository,
                    media_type=descriptor.get("mediaType"), size=descriptor["size"],
                    required=False, state=STATE_VISIBLE_NOT_REQUIRED,
                )
                self.store.add_edge(acceptance_id, node.digest, digest, "index_entry", False)
                continue
            required = True
            if policy.platforms:
                required = any(selector.matches(descriptor) for selector in policy.platforms)
                for selector in policy.platforms:
                    if selector.matches(descriptor):
                        matched_selectors.add(selector.as_text())
            self.store.upsert_node(
                acceptance_id, digest, ROLE_PLATFORM_MANIFEST, repository,
                media_type=descriptor.get("mediaType"), size=descriptor["size"],
                required=required,
                state=STATE_PENDING if required else STATE_VISIBLE_NOT_REQUIRED,
            )
            self.store.add_edge(acceptance_id, node.digest, digest, "platform", required)
        for selector in policy.platforms:
            if selector.as_text() not in matched_selectors:
                self.store.event(acceptance_id, "platform_not_selected", {
                    "selector": selector.as_text(), "index": node.digest,
                })
                self.store.add_reason(acceptance_id, node.digest, {
                    "code": oci.REASON_PLATFORM_NOT_FOUND, "selector": selector.as_text(),
                })
                self.store.set_state(acceptance_id, node.digest, STATE_REJECTED)

    def _expand_image_manifest(self, acceptance_id: int, node: Node,
                               document: dict, repository: str) -> None:
        for relation, descriptor, role in (
            ("config", document.get("config"), ROLE_CONFIG),
            *[("layer", item, ROLE_LAYER) for item in document.get("layers", [])],
        ):
            if not isinstance(descriptor, dict) or not oci.valid_digest(descriptor.get("digest")):
                self.store.update_node(acceptance_id, node.digest, state=STATE_REJECTED,
                                       reasons=node.reasons + [{"code": oci.REASON_MALFORMED,
                                                                "relation": relation}])
                return
            self._register_child(acceptance_id, node, descriptor, role, relation, repository)
        subject = document.get("subject")
        if isinstance(subject, dict) and subject.get("digest"):
            # subject 是反向引用，不沿其继续抓取；只登记可见性边界。
            self.store.add_edge(acceptance_id, node.digest, subject["digest"], "subject", False)
            self.store.event(acceptance_id, "subject_boundary_observed", {
                "manifest": node.digest, "subject": subject.get("digest"),
            })

    def _expand_artifact_manifest(self, acceptance_id: int, node: Node,
                                  document: dict, repository: str) -> None:
        for descriptor in document.get("blobs", []):
            if not isinstance(descriptor, dict) or not oci.valid_digest(descriptor.get("digest")):
                self.store.update_node(acceptance_id, node.digest, state=STATE_REJECTED,
                                       reasons=node.reasons + [{"code": oci.REASON_MALFORMED,
                                                                "relation": "blob"}])
                return
            self._register_child(acceptance_id, node, descriptor, ROLE_ARTIFACT_BLOB,
                                 "blob", repository)

    def _register_child(self, acceptance_id: int, parent: Node, descriptor: dict,
                        role: str, relation: str, repository: str) -> None:
        digest = descriptor["digest"]
        existing = self.store.node(acceptance_id, digest)
        self.store.upsert_node(
            acceptance_id, digest, role, repository,
            media_type=descriptor.get("mediaType"), size=descriptor.get("size"),
            required=True,
            state=existing.state if existing else STATE_PENDING,
        )
        self.store.add_edge(acceptance_id, parent.digest, digest, relation, True)
        if descriptor.get("urls"):
            # 跨仓库/外部 URL 不跟随：可见性边界必须显式留痕。
            self.store.add_reason(acceptance_id, digest,
                                  {"code": oci.REASON_EXTERNAL_URL_NOT_FOLLOWED,
                                   "urls": descriptor["urls"]})

    # ---- 3. 描述符抓取与逐字段校验 ------------------------------------
    _SEMANTIC_REASONS = frozenset({
        oci.REASON_PLATFORM_NOT_FOUND,
        oci.REASON_SUBJECT_MISMATCH,
        oci.REASON_SUBJECT_MISSING,
    })

    def _fetch_one(self, acceptance_id: int, node: Node, *, manifest_first: bool = False):
        if oci.is_manifest_media_type(node.media_type) or (manifest_first and node.media_type is None):
            path = oci.manifest_path(node.repository, node.digest)
            headers = {"Accept": oci.ACCEPT_MANIFESTS}
        else:
            path = oci.blob_path(node.repository, node.digest)
            headers = {}
        return path, self._request(acceptance_id, path, headers)

    def _fetch_descriptor(self, acceptance_id: int, node: Node) -> None:
        if any(reason.get("code") in self._SEMANTIC_REASONS for reason in node.reasons):
            # 语义级拒收是确定性结论，重取不会改变，保持拒收不再请求。
            return
        # 描述符未声明 mediaType（OCI 允许省略）时先按 manifest 路径取，
        # 404 再回退 blob 路径；两种交换都照常留痕。
        path, response = self._fetch_one(
            acceptance_id, node, manifest_first=node.media_type is None,
        )
        if response is not None and response.status == 404 and node.media_type is None:
            blob_path = oci.blob_path(node.repository, node.digest)
            blob_response = self._request(acceptance_id, blob_path, {})
            if blob_response is not None and blob_response.status == 200:
                path, response = blob_path, blob_response
        if response is None:
            # 传输层故障是瞬时状态：标记 missing 以便恢复重试，不做确定性拒收。
            self.store.update_node(
                acceptance_id, node.digest, state=STATE_MISSING,
                reasons=node.reasons + [{"code": oci.REASON_TRANSPORT, "path": path}],
                url=path,
            )
            return
        # 重试成功时清掉上一轮的传输/HTTP 缺口记录；观察类与 artifact 标记保留。
        stale = {oci.REASON_TRANSPORT, oci.REASON_HTTP_STATUS}
        reasons = [item for item in node.reasons if item.get("code") not in stale]
        if response.status == 404:
            self.store.update_node(
                acceptance_id, node.digest, state=STATE_MISSING,
                reasons=reasons + [{"code": oci.REASON_HTTP_STATUS, "status": 404,
                                    "capture_id": response.capture_id}],
                url=response.url, capture_id=response.capture_id,
            )
            self.store.event(acceptance_id, "descriptor_missing",
                             {"digest": node.digest, "capture_id": response.capture_id})
            return
        if response.status != 200:
            self.store.update_node(
                acceptance_id, node.digest, state=STATE_REJECTED,
                reasons=reasons + [{"code": oci.REASON_HTTP_STATUS, "status": response.status,
                                    "capture_id": response.capture_id}],
                url=response.url, capture_id=response.capture_id,
            )
            return
        if node.size is not None and node.size != len(response.body):
            reasons.append({"code": oci.REASON_SIZE_MISMATCH, "declared": node.size,
                            "observed": len(response.body)})
        ok, digest_reason = oci.verify_digest(node.digest, response.body)
        if not ok:
            reasons.append({"code": digest_reason, "declared": node.digest,
                            "observed": oci.digest_of(response.body)})
        header_digest = response.headers.get("docker-content-digest")
        if header_digest and header_digest != node.digest:
            reasons.append({"code": oci.REASON_DIGEST_HEADER_MISMATCH,
                            "header": header_digest, "declared": node.digest})
        observed_media = oci.base_media_type(response.headers.get("content-type"))
        if node.media_type and observed_media and observed_media != node.media_type:
            # 媒体类型不一致只作可见性告警，不单独构成拒收。
            reasons.append({"code": oci.REASON_OBSERVED_MEDIA_TYPE_DIFFERS,
                            "declared": node.media_type, "observed": observed_media})
        # 描述符省略 mediaType 时，以响应 Content-Type 作为文档类型线索。
        effective_media = node.media_type or observed_media
        kind = None
        if oci.is_manifest_media_type(effective_media):
            try:
                kind = oci.structural_kind(oci.parse_document(response.body))
                if kind != node.kind and node.kind is not None:
                    reasons.append({"code": oci.REASON_KIND_MISMATCH,
                                    "expected": node.kind, "observed": kind})
            except ValueError as error:
                reasons.append({"code": str(error)})
        hard_failure = any(item.get("code") in (
            oci.REASON_SIZE_MISMATCH, oci.REASON_DIGEST_MISMATCH,
            oci.REASON_UNKNOWN_DIGEST_ALGORITHM, oci.REASON_DIGEST_HEADER_MISMATCH,
            oci.REASON_KIND_MISMATCH, oci.REASON_MALFORMED,
        ) for item in reasons)
        self.store.update_node(
            acceptance_id, node.digest,
            state=STATE_REJECTED if hard_failure else STATE_PRESENT,
            reasons=reasons, url=response.url, capture_id=response.capture_id, kind=kind,
        )
        self.store.event(acceptance_id, "descriptor_fetched", {
            "digest": node.digest, "state": STATE_REJECTED if hard_failure else STATE_PRESENT,
            "capture_id": response.capture_id,
        })

    # ---- 4. 关联制品（referrers）--------------------------------------
    def _collect_referrers(self, acceptance_id: int, repository: str, policy: oci.Policy) -> None:
        acceptance = self.store.get(acceptance_id)
        root_digest = acceptance["root_digest"]
        if root_digest is None:
            self.store.event(acceptance_id, "referrers_skipped", {"reason": "root_not_frozen"})
            return
        prior_state = acceptance["referrers_state"]
        if prior_state in ("supported", "fallback_index", "skipped"):
            return  # 恢复时不重复发现；节点与边已在库中。
        if prior_state == "unsupported" and policy.referrers != "fallback":
            # 已确认接口缺失且本地策略不用兼容索引，结论稳定，不再请求。
            self._check_required_artifact_types(acceptance_id, policy)
            return
        # prior_state 为 None（首次）或 error（上次传输失败，恢复重试）时继续。
        if policy.referrers == "skip":
            self.store.set_referrers_state(acceptance_id, "skipped")
            self.store.event(acceptance_id, "referrers_skipped", {"reason": "local_policy"})
            self._check_required_artifact_types(acceptance_id, policy)
            return
        response = self._request(
            acceptance_id, oci.referrers_path(repository, root_digest),
            {"Accept": oci.ACCEPT_MANIFESTS},
        )
        if response is not None and response.status == 200:
            self.store.set_referrers_state(acceptance_id, "supported")
            self.store.event(acceptance_id, "referrers_api_supported",
                             {"capture_id": response.capture_id})
            self._ingest_referrer_index(acceptance_id, response, repository, policy)
            self._check_required_artifact_types(acceptance_id, policy)
            return
        if response is not None and response.status == 404:
            # 接口缺失与“确实没有关联制品”是两种状态，必须分别记录。
            self.store.set_referrers_state(acceptance_id, "unsupported")
            self.store.event(acceptance_id, "referrers_api_unsupported",
                             {"capture_id": response.capture_id})
            if policy.referrers == "fallback":
                self._fallback_referrers(acceptance_id, repository, policy)
            else:
                self._check_required_artifact_types(acceptance_id, policy)
            return
        status = None if response is None else response.status
        self.store.set_referrers_state(acceptance_id, "error")
        self.store.event(acceptance_id, "referrers_api_error", {"status": status})

    def _fallback_referrers(self, acceptance_id: int, repository: str, policy: oci.Policy) -> None:
        root_digest = self.store.get(acceptance_id)["root_digest"]
        reference = oci.fallback_reference(root_digest)
        response = self._request(
            acceptance_id, oci.manifest_path(repository, reference),
            {"Accept": oci.ACCEPT_MANIFESTS},
        )
        if response is None or response.status != 200:
            self.store.event(acceptance_id, "referrers_fallback_unavailable",
                             {"reference": reference,
                              "status": None if response is None else response.status})
            self._check_required_artifact_types(acceptance_id, policy)
            return
        self.store.set_referrers_state(acceptance_id, "fallback_index")
        self.store.event(acceptance_id, "referrers_fallback_used",
                         {"reference": reference, "capture_id": response.capture_id})
        self._ingest_referrer_index(acceptance_id, response, repository, policy)
        self._check_required_artifact_types(acceptance_id, policy)

    def _ingest_referrer_index(self, acceptance_id: int, response, repository: str,
                               policy: oci.Policy) -> None:
        root_digest = self.store.get(acceptance_id)["root_digest"]
        try:
            document = oci.parse_document(response.body)
            entries = document["manifests"]
            assert isinstance(entries, list)
        except (ValueError, KeyError, AssertionError):
            self.store.event(acceptance_id, "referrers_index_malformed",
                             {"capture_id": response.capture_id})
            return
        # 空列表是明确的“没有关联制品”，与 404 接口缺失完全不同。
        self.store.event(acceptance_id, "referrers_observed", {"count": len(entries)})
        for entry in entries:
            descriptor = entry if isinstance(entry, dict) else {}
            if not oci.valid_digest(descriptor.get("digest")):
                self.store.event(acceptance_id, "malformed_descriptor",
                                 {"parent": root_digest, "descriptor": descriptor})
                continue
            artifact_type = descriptor.get("artifactType")
            required = (not policy.required_artifact_types
                        or (artifact_type in policy.required_artifact_types))
            self.store.upsert_node(
                acceptance_id, descriptor["digest"], ROLE_REFERRER_MANIFEST, repository,
                media_type=descriptor.get("mediaType"), size=descriptor.get("size"),
                required=required,
                state=STATE_PENDING if required else STATE_VISIBLE_NOT_REQUIRED,
            )
            self.store.add_edge(acceptance_id, root_digest, descriptor["digest"],
                                "referrer", required)
            self.store.add_reason(acceptance_id, descriptor["digest"],
                                  {"artifact_type": artifact_type})
        # 先把 referrer 清单本体取回并做 subject 核验；未通过的节点不会展开，
        # 其 blob 也就不会被领取。
        for node in self.store.nodes(acceptance_id):
            if node.role == ROLE_REFERRER_MANIFEST and node.state == STATE_PENDING:
                self._fetch_descriptor(acceptance_id, node)
        self._verify_referrer_subjects(acceptance_id, repository)
        # 通过核验的 referrer 再进入同一套闭包抓取（其 blobs 描述符）。
        self._collect_closure(acceptance_id, repository)

    def _verify_referrer_subjects(self, acceptance_id: int, repository: str) -> None:
        root_digest = self.store.get(acceptance_id)["root_digest"]
        for node in self.store.nodes(acceptance_id):
            if node.role != ROLE_REFERRER_MANIFEST or node.state != STATE_PRESENT:
                continue
            body = self.archive.body(node.capture_id)
            try:
                document = oci.parse_document(body)
            except ValueError:
                self.store.update_node(acceptance_id, node.digest, state=STATE_REJECTED,
                                       reasons=node.reasons + [{"code": oci.REASON_MALFORMED}])
                continue
            subject = document.get("subject")
            if not isinstance(subject, dict) or not oci.valid_digest(subject.get("digest")):
                # subject 缺失：无法证明它属于本次验收对象，拒收且不展开其内容。
                self.store.update_node(
                    acceptance_id, node.digest, state=STATE_REJECTED,
                    reasons=node.reasons + [{"code": oci.REASON_SUBJECT_MISSING}],
                )
                self.store.event(acceptance_id, "referrer_rejected",
                                 {"digest": node.digest, "code": oci.REASON_SUBJECT_MISSING})
                continue
            if subject["digest"] != root_digest:
                # 串入其他验收对象的交付资料，拒收。
                self.store.update_node(
                    acceptance_id, node.digest, state=STATE_REJECTED,
                    reasons=node.reasons + [{
                        "code": oci.REASON_SUBJECT_MISMATCH,
                        "subject": subject["digest"], "expected": root_digest,
                    }],
                )
                self.store.event(acceptance_id, "referrer_rejected", {
                    "digest": node.digest, "code": oci.REASON_SUBJECT_MISMATCH,
                    "subject": subject["digest"],
                })

    def _check_required_artifact_types(self, acceptance_id: int, policy: oci.Policy) -> None:
        if not policy.required_artifact_types:
            return
        present_types = {
            reason.get("artifact_type")
            for node in self.store.nodes(acceptance_id)
            if node.role == ROLE_REFERRER_MANIFEST and node.state == STATE_PRESENT
            for reason in node.reasons
        }
        for artifact_type in sorted(policy.required_artifact_types):
            if artifact_type not in present_types:
                self.store.event(acceptance_id, "required_artifact_absent",
                                 {"artifact_type": artifact_type,
                                  "code": oci.REASON_REQUIRED_ARTIFACT_ABSENT})

    # ---- 5. 判定与漂移探针 --------------------------------------------
    def _determine_status(self, acceptance_id: int, policy: oci.Policy) -> str:
        nodes = self.store.nodes(acceptance_id)
        if any(node.state == STATE_REJECTED and node.required for node in nodes):
            return REJECTED_STATUS
        acceptance = self.store.get(acceptance_id)
        if acceptance["root_digest"] is None:
            return INCOMPLETE
        if any(node.required and node.state != STATE_PRESENT for node in nodes):
            return INCOMPLETE
        if policy.required_artifact_types:
            present_types = {
                reason.get("artifact_type")
                for node in nodes
                if node.role == ROLE_REFERRER_MANIFEST and node.state == STATE_PRESENT
                for reason in node.reasons
            }
            if not policy.required_artifact_types.issubset(present_types):
                return INCOMPLETE
        if acceptance["referrers_state"] == "error":
            return INCOMPLETE
        return ACCEPTED

    def _drift_probe(self, acceptance_id: int, acceptance: dict[str, Any]) -> None:
        # 只观察、只留痕；无论标签是否移动，冻结视图与验收结论都不变。
        frozen_digest = self.store.get(acceptance_id)["root_digest"]
        response = self._request(
            acceptance_id,
            oci.manifest_path(acceptance["repository"], acceptance["reference"]),
            {"Accept": oci.ACCEPT_MANIFESTS},
        )
        if response is None or response.status != 200:
            self.store.event(acceptance_id, "tag_drift_probe_failed",
                             {"status": None if response is None else response.status})
            return
        observed = oci.digest_of(response.body)
        self.store.event(acceptance_id, "tag_drift_observed", {
            "reference": acceptance["reference"], "frozen": frozen_digest,
            "observed": observed, "moved": observed != frozen_digest,
            "capture_id": response.capture_id,
        })

    # ---- 报告视图 -----------------------------------------------------
    def view(self, acceptance_id: int) -> dict[str, Any]:
        acceptance = self.store.get(acceptance_id)
        policy: oci.Policy = acceptance["policy"]
        nodes = self.store.nodes(acceptance_id)
        frozen_event = next(
            (event for event in self.store.events(acceptance_id)
             if event["event"] == "tag_frozen"), None,
        )
        return {
            "acceptance_id": acceptance_id,
            "source": acceptance["source"],
            "registry": acceptance["registry"],
            "repository": acceptance["repository"],
            "reference": acceptance["reference"],
            "status": acceptance["status"],
            "created_at": acceptance["created_at"],
            "finalized_at": acceptance["finalized_at"],
            "policy": json.loads(policy.to_json()),
            "root_digest": acceptance["root_digest"],
            "referrers_state": acceptance["referrers_state"],
            "frozen": frozen_event["detail"] if frozen_event else None,
            "platforms": [selector.as_text() for selector in policy.platforms],
            "nodes": [self._node_view(acceptance_id, node) for node in nodes],
            "edges": self.store.edges(acceptance_id),
            "missing": [node.digest for node in nodes
                        if node.required and node.state == STATE_MISSING],
            "rejected": [node.digest for node in nodes
                         if node.required and node.state == STATE_REJECTED],
            "events": self.store.events(acceptance_id),
        }

    def _node_view(self, acceptance_id: int, node: Node) -> dict[str, Any]:
        provenance = None
        if node.capture_id is not None:
            row = self.archive.connection.execute(
                "SELECT c.url,c.status,c.headers,c.received_at,c.digest AS content_digest "
                "FROM captures c WHERE c.id=?",
                (node.capture_id,),
            ).fetchone()
            if row is not None:
                provenance = {
                    "capture_id": node.capture_id,
                    "url": row["url"],
                    "status": row["status"],
                    "received_at": row["received_at"],
                    "content_digest": row["content_digest"],
                    "headers": json.loads(row["headers"]),
                }
        return {
            "digest": node.digest,
            "role": node.role,
            "media_type": node.media_type,
            "size": node.size,
            "kind": node.kind,
            "required": node.required,
            "state": node.state,
            "reasons": node.reasons,
            "repository": node.repository,
            "provenance": provenance,
        }

    # ---- 原始留痕：每个 HTTP 交换都进归档 -----------------------------
    def _request(self, acceptance_id: int, path: str, headers: dict | None = None):
        try:
            response = self.client.fetch(path, headers)
        except (URLError, OSError) as error:
            self.store.event(acceptance_id, "request_failed",
                             {"path": path, "error": type(error).__name__})
            return None
        capture_id = self.archive.append(
            self.store.get(acceptance_id)["source"], response.url, response.status,
            response.headers, response.body,
        )
        return _ArchivedResponse(response.url, response.status, response.headers,
                                 response.body, capture_id)


@dataclass(frozen=True)
class _ArchivedResponse:
    url: str
    status: int
    headers: dict
    body: bytes
    capture_id: int


class _TransportFailure(Exception):
    pass
