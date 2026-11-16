"""OCI 交付验收：标签冻结、描述符闭包采集与验收判定。

一次验收（run）从标签观察开始，立刻把观察结果冻结为摘要；此后所有
抓取只按摘要寻址，标签在过程中移动也不会改写这次冻结视图。每个描述符
的媒体类型、长度、摘要都与实际收到的字节核对；引用关系（index 成员、
config、layer、referrer/subject）逐边留痕；相同字节按摘要复用，但每次
来源观察的仓库、URL、状态与时间分别记录，恢复重试只会追加证据。
"""
import json
from dataclasses import dataclass, field

from .registry import (
    INDEX_MEDIA_TYPES,
    IMAGE_MEDIA_TYPES,
    MANIFEST_ACCEPT,
    MANIFEST_MEDIA_TYPES,
    REFERRERS_ACCEPT,
    RegistryClient,
    blob_path,
    digest_of,
    fallback_tag,
    manifest_path,
    normalize_content_type,
    parse_digest,
    referrers_path,
    validate_repository,
)

# 节点状态
EXPECTED = "expected"      # 已由父描述符声明，尚未成功领取
VERIFIED = "verified"      # 字节与描述符三要素核对通过
MISSING = "missing"        # 来源明确没有（404）或无法取得
REJECTED = "rejected"      # 收到了但核对失败，或引用关系非法
SKIPPED = "skipped"        # 按本地平台策略不领取，描述符仍登记在案

# 验收运行状态
RUN_FROZEN = "frozen"
RUN_INCOMPLETE = "incomplete"
RUN_REJECTED = "rejected"
RUN_COMPLETE = "complete"
RUN_FAILED = "failed"

# 关联制品接口的可见性
REF_UNKNOWN = "unknown"
REF_API = "api_available"
REF_UNSUPPORTED = "api_unsupported"
REF_FALLBACK = "fallback_tag"

BLOB_ROLES = frozenset({"config", "layer", "referrer_config", "referrer_layer"})
MANIFEST_ROLES = frozenset({"root", "platform", "referrer"})


class FreezeError(RuntimeError):
    """标签无法冻结为摘要，一次验收尚未成立。"""


@dataclass(frozen=True)
class PlatformSelector:
    os: str
    architecture: str
    variant: str | None = None

    def matches(self, platform):
        if not isinstance(platform, dict):
            return False
        if platform.get("os") != self.os or platform.get("architecture") != self.architecture:
            return False
        if self.variant is not None and platform.get("variant") != self.variant:
            return False
        return True


@dataclass(frozen=True)
class Policy:
    """本地验收策略：平台选择、关联制品要求与兼容索引开关。"""
    platforms: tuple[PlatformSelector, ...] = ()
    required_artifact_types: frozenset[str] = frozenset()
    allow_fallback_referrers: bool = False
    require_referrers_visibility: bool = False

    def to_json(self):
        return json.dumps({
            "platforms": [vars(item) for item in self.platforms],
            "required_artifact_types": sorted(self.required_artifact_types),
            "allow_fallback_referrers": self.allow_fallback_referrers,
            "require_referrers_visibility": self.require_referrers_visibility,
        }, ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, encoded):
        payload = json.loads(encoded)
        return cls(
            platforms=tuple(PlatformSelector(**item) for item in payload["platforms"]),
            required_artifact_types=frozenset(payload["required_artifact_types"]),
            allow_fallback_referrers=payload["allow_fallback_referrers"],
            require_referrers_visibility=payload["require_referrers_visibility"],
        )


class Journal:
    """验收台账：与原始响应归档共用一个 SQLite 连接，只追加、不覆盖。"""

    def __init__(self, archive):
        self.archive = archive
        self.connection = archive.connection
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS acceptance_runs (
                id INTEGER PRIMARY KEY,
                source TEXT NOT NULL,
                base_url TEXT NOT NULL,
                repository TEXT NOT NULL,
                reference TEXT NOT NULL,
                policy TEXT NOT NULL,
                state TEXT NOT NULL,
                root_digest TEXT,
                root_media_type TEXT,
                root_size INTEGER,
                root_capture_id INTEGER,
                referrers_state TEXT NOT NULL DEFAULT 'unknown',
                referrers_capture_id INTEGER,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            CREATE TABLE IF NOT EXISTS acceptance_nodes (
                run_id INTEGER NOT NULL REFERENCES acceptance_runs(id),
                ordinal INTEGER NOT NULL,
                digest TEXT NOT NULL,
                role TEXT NOT NULL,
                media_type TEXT NOT NULL,
                size INTEGER NOT NULL,
                platform TEXT,
                artifact_type TEXT,
                state TEXT NOT NULL,
                PRIMARY KEY (run_id, digest, role)
            );
            CREATE TABLE IF NOT EXISTS acceptance_edges (
                run_id INTEGER NOT NULL,
                parent_digest TEXT NOT NULL,
                child_digest TEXT NOT NULL,
                relation TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                position INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (run_id, parent_digest, child_digest, relation, position)
            );
            CREATE TABLE IF NOT EXISTS acceptance_problems (
                id INTEGER PRIMARY KEY,
                run_id INTEGER NOT NULL,
                node_digest TEXT,
                severity TEXT NOT NULL,
                code TEXT NOT NULL,
                detail TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS acceptance_provenance (
                run_id INTEGER NOT NULL,
                node_digest TEXT NOT NULL,
                capture_id INTEGER NOT NULL,
                PRIMARY KEY (run_id, node_digest, capture_id)
            );
        """)

    def create_run(self, source, base_url, repository, reference, policy):
        with self.connection:
            cursor = self.connection.execute(
                "INSERT INTO acceptance_runs(source,base_url,repository,reference,policy,state)"
                " VALUES (?,?,?,?,?,?)",
                (source, base_url, repository, reference, policy.to_json(), RUN_FROZEN),
            )
        return cursor.lastrowid

    def get_run(self, run_id):
        row = self.connection.execute(
            "SELECT * FROM acceptance_runs WHERE id=?", (run_id,)
        ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return dict(row)

    def list_runs(self):
        rows = self.connection.execute("SELECT * FROM acceptance_runs ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def touch(self, run_id, state):
        self.connection.execute(
            "UPDATE acceptance_runs SET state=?, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
            (state, run_id),
        )

    def set_root(self, run_id, digest, media_type, size, capture_id):
        self.connection.execute(
            "UPDATE acceptance_runs SET root_digest=?,root_media_type=?,root_size=?,"
            "root_capture_id=?,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
            (digest, media_type, size, capture_id, run_id),
        )

    def set_referrers(self, run_id, state, capture_id=None):
        self.connection.execute(
            "UPDATE acceptance_runs SET referrers_state=?,referrers_capture_id=?,"
            "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
            (state, capture_id, run_id),
        )

    def next_ordinal(self, run_id):
        row = self.connection.execute(
            "SELECT coalesce(max(ordinal),-1)+1 FROM acceptance_nodes WHERE run_id=?", (run_id,)
        ).fetchone()
        return row[0]

    def enqueue(self, run_id, descriptor, role, state=EXPECTED, platform=None, artifact_type=None):
        digest = descriptor["digest"]
        existing = self.connection.execute(
            "SELECT 1 FROM acceptance_nodes WHERE run_id=? AND digest=? AND role=?",
            (run_id, digest, role),
        ).fetchone()
        if existing is not None:
            return False
        self.connection.execute(
            "INSERT INTO acceptance_nodes(run_id,ordinal,digest,role,media_type,size,platform,"
            "artifact_type,state) VALUES (?,?,?,?,?,?,?,?,?)",
            (run_id, self.next_ordinal(run_id), digest, role, descriptor["mediaType"],
             int(descriptor["size"]),
             json.dumps(platform, ensure_ascii=False, sort_keys=True) if platform else None,
             artifact_type or descriptor.get("artifactType"), state),
        )
        return True

    def add_edge(self, run_id, parent, child, relation, position=0):
        row = self.connection.execute(
            "SELECT coalesce(max(ordinal),-1)+1 FROM acceptance_edges WHERE run_id=?", (run_id,)
        ).fetchone()
        self.connection.execute(
            "INSERT OR IGNORE INTO acceptance_edges VALUES (?,?,?,?,?,?)",
            (run_id, parent, child, relation, row[0], position),
        )

    def update_node(self, run_id, digest, role, state):
        self.connection.execute(
            "UPDATE acceptance_nodes SET state=? WHERE run_id=? AND digest=? AND role=?",
            (state, run_id, digest, role),
        )

    def node(self, run_id, digest, role):
        row = self.connection.execute(
            "SELECT * FROM acceptance_nodes WHERE run_id=? AND digest=? AND role=?",
            (run_id, digest, role),
        ).fetchone()
        return dict(row) if row else None

    def pending_nodes(self, run_id):
        rows = self.connection.execute(
            "SELECT * FROM acceptance_nodes WHERE run_id=? AND state=? ORDER BY ordinal",
            (run_id, EXPECTED),
        ).fetchall()
        return [dict(row) for row in rows]

    def retry_failed_nodes(self, run_id):
        """恢复时把上一轮 missing/rejected 的节点重新置为待领取。

        每次 resume 只翻转一次；若再次失败会回到 missing/rejected 而不会
        在同一次遍历中无限重入，保证遍历必然终止。
        """
        cursor = self.connection.execute(
            "UPDATE acceptance_nodes SET state=? WHERE run_id=? AND state IN (?,?)",
            (EXPECTED, run_id, MISSING, REJECTED),
        )
        return cursor.rowcount

    def problem(self, run_id, code, detail, severity="fatal", node_digest=None):
        self.connection.execute(
            "INSERT INTO acceptance_problems(run_id,node_digest,severity,code,detail) VALUES (?,?,?,?,?)",
            (run_id, node_digest, severity, code, detail),
        )

    def provenance(self, run_id, digest, capture_id):
        self.connection.execute(
            "INSERT OR IGNORE INTO acceptance_provenance VALUES (?,?,?)",
            (run_id, digest, capture_id),
        )

    def report(self, run_id):
        run = self.get_run(run_id)
        nodes = [dict(row) for row in self.connection.execute(
            "SELECT * FROM acceptance_nodes WHERE run_id=? ORDER BY ordinal", (run_id,)
        )]
        for node in nodes:
            node["platform"] = json.loads(node["platform"]) if node["platform"] else None
        edges = [dict(row) for row in self.connection.execute(
            "SELECT parent_digest,child_digest,relation,ordinal,position FROM acceptance_edges "
            "WHERE run_id=? ORDER BY ordinal", (run_id,)
        )]
        problems = [dict(row) for row in self.connection.execute(
            "SELECT id,node_digest,severity,code,detail FROM acceptance_problems "
            "WHERE run_id=? ORDER BY id", (run_id,)
        )]
        provenance_rows = self.connection.execute(
            "SELECT p.node_digest,p.capture_id,c.url,c.status,c.headers,c.digest AS observed_digest "
            "FROM acceptance_provenance p JOIN captures c ON c.id=p.capture_id "
            "WHERE p.run_id=? ORDER BY p.capture_id", (run_id,)
        ).fetchall()
        provenance = {}
        for row in provenance_rows:
            provenance.setdefault(row["node_digest"], []).append({
                "capture_id": row["capture_id"],
                "url": row["url"],
                "status": row["status"],
                "content_type": json.loads(row["headers"]).get("content-type", ""),
                "observed_digest": "sha256:" + row["observed_digest"],
            })
        counts = {state: 0 for state in (EXPECTED, VERIFIED, MISSING, REJECTED, SKIPPED)}
        for node in nodes:
            counts[node["state"]] += 1
        return {
            "run_id": run["id"],
            "source": run["source"],
            "base_url": run["base_url"],
            "repository": run["repository"],
            "reference": run["reference"],
            "policy": json.loads(run["policy"]),
            "state": run["state"],
            "root": None if run["root_digest"] is None else {
                "digest": run["root_digest"],
                "media_type": run["root_media_type"],
                "size": run["root_size"],
                "capture_id": run["root_capture_id"],
            },
            "referrers": {
                "state": run["referrers_state"],
                "capture_id": run["referrers_capture_id"],
            },
            "closure": {
                "total": len(nodes),
                "verified": counts[VERIFIED],
                "missing": counts[MISSING],
                "rejected": counts[REJECTED],
                "skipped": counts[SKIPPED],
                "pending": counts[EXPECTED],
            },
            "nodes": nodes,
            "edges": edges,
            "problems": problems,
            "provenance": provenance,
        }


class Acceptance:
    """执行一次验收：冻结、闭包遍历、关联制品、判定与恢复。"""

    def __init__(self, registry, journal, repository, reference, policy):
        validate_repository(repository)
        self.registry = registry
        self.journal = journal
        self.archive = registry.archive
        self.repository = repository
        self.reference = reference
        self.policy = policy
        self.run_id = None
        self.run = None

    # ---- 入口 ----------------------------------------------------------

    def start(self):
        self.run_id = self.journal.create_run(
            self.registry.source, self.registry.client.base_url,
            self.repository, self.reference, self.policy,
        )
        # 不用单个外层事务包住全程：任何一步失败都要留下已冻结视图和已追加
        # 的原始响应，便于事后追查与恢复，不能因回滚抹掉证据。
        try:
            with self.archive.transaction():
                self._freeze()
            with self.archive.transaction():
                self._walk_content()
            with self.archive.transaction():
                self._walk_referrers()
            with self.archive.transaction():
                self._finalize()
        except FreezeError:
            # _freeze 内的事务随异常回滚，这里在新事务里固化 failed 结论；
            # 原始响应已由 Archive 独立提交，证据不会丢失。
            with self.archive.transaction():
                self.journal.touch(self.run_id, RUN_FAILED)
            raise
        except Exception:
            state = self._current_state()
            if state not in (RUN_REJECTED, RUN_FAILED):
                with self.archive.transaction():
                    self.journal.touch(self.run_id, RUN_INCOMPLETE)
            raise
        return self.run_id

    def resume(self, run_id):
        self.run_id = run_id
        self.run = self.journal.get_run(run_id)
        if self.run["repository"] != self.repository or self.run["reference"] != self.reference:
            raise ValueError("run does not match repository/reference; a tag is never re-followed")
        if self.run["state"] == RUN_FROZEN or self.run["root_digest"] is None:
            raise ValueError("run never reached a frozen root; start a new acceptance instead")
        if self.run["state"] == RUN_COMPLETE:
            return run_id  # 已完成的验收不产生新抓取
        # 恢复沿用创建运行时固化的本地策略，不接受中途换策略改写视图。
        self.policy = Policy.from_json(self.run["policy"])
        with self.archive.transaction():
            retried = self.journal.retry_failed_nodes(run_id)
        if retried or self.journal.pending_nodes(run_id):
            with self.archive.transaction():
                self._walk_content()
        with self.archive.transaction():
            if self.run["referrers_state"] == REF_UNKNOWN:
                self._walk_referrers()
        with self.archive.transaction():
            self._finalize()
        return run_id

    # ---- 标签冻结 ------------------------------------------------------

    def _fail_freeze(self, code, detail, message, node_digest=None):
        # 冻结失败要把问题与 failed 状态独立提交，避免随外层事务回滚丢证据。
        with self.archive.transaction():
            self.journal.problem(self.run_id, code, detail, node_digest=node_digest)
            self.journal.touch(self.run_id, RUN_FAILED)
        raise FreezeError(message)

    def _freeze(self):
        is_digest = self.reference.startswith("sha256:")
        if is_digest:
            parse_digest(self.reference)
            path = manifest_path(self.repository, self.reference)
        else:
            from .registry import validate_tag
            validate_tag(self.reference)
            path = manifest_path(self.repository, self.reference)
        fetched = self.registry.get(path, headers={"Accept": MANIFEST_ACCEPT})
        if fetched.status != 200:
            self._fail_freeze(
                "freeze_failed",
                f"标签观察返回 {fetched.status}，无法冻结摘要（{path}）",
                f"cannot freeze {self.repository}:{self.reference}: HTTP {fetched.status}",
            )
        observed = digest_of(fetched.body)
        if is_digest and observed != self.reference:
            self._fail_freeze(
                "digest_mismatch",
                f"摘要引用 {self.reference} 实际返回 {observed}，拒收",
                "digest reference returned different content",
                node_digest=self.reference,
            )
        try:
            parsed = json.loads(fetched.body)
        except ValueError as error:
            self._fail_freeze(
                "invalid_manifest", f"根清单不是合法 JSON：{error}",
                "root manifest is not JSON",
            )
        media_type = parsed.get("mediaType") or normalize_content_type(
            fetched.headers.get("content-type", "")
        )
        if media_type not in MANIFEST_MEDIA_TYPES:
            self._fail_freeze(
                "unsupported_media_type", f"根清单媒体类型不受支持：{media_type}",
                f"unsupported root media type: {media_type}", node_digest=observed,
            )
        descriptor = {"digest": observed, "size": len(fetched.body), "mediaType": media_type}
        self.journal.enqueue(self.run_id, descriptor, "root", VERIFIED)
        self.journal.provenance(self.run_id, observed, fetched.capture_id)
        self.journal.set_root(self.run_id, observed, media_type, len(fetched.body), fetched.capture_id)
        self.run = self.journal.get_run(self.run_id)
        self._expand_manifest(observed, "root", parsed)

    # ---- 闭包遍历 ------------------------------------------------------

    def _walk_content(self):
        while True:
            pending = self.journal.pending_nodes(self.run_id)
            if not pending:
                return
            for node in pending:
                self._fetch_node(node)

    def _fetch_node(self, node):
        digest, role = node["digest"], node["role"]
        headers = None
        if role in BLOB_ROLES:
            path = blob_path(self.repository, digest)
        else:
            # 恢复与继续都只按冻结摘要寻址，绝不重新解析标签。
            path = manifest_path(self.repository, digest)
            headers = {"Accept": MANIFEST_ACCEPT}
        fetched = self.registry.get(path, headers=headers)
        self.journal.provenance(self.run_id, digest, fetched.capture_id)
        if fetched.status == 404:
            self.journal.update_node(self.run_id, digest, role, MISSING)
            self.journal.problem(
                self.run_id, "content_not_found",
                f"{role} 描述符 {digest} 在来源返回 404（{path}）", node_digest=digest,
            )
            return
        if fetched.status != 200:
            self.journal.update_node(self.run_id, digest, role, REJECTED)
            self.journal.problem(
                self.run_id, "http_error",
                f"{role} 描述符 {digest} 抓取失败：HTTP {fetched.status}（{path}）",
                node_digest=digest,
            )
            return
        observed = digest_of(fetched.body)
        if observed != digest:
            self.journal.update_node(self.run_id, digest, role, REJECTED)
            self.journal.problem(
                self.run_id, "digest_mismatch",
                f"{role} 期望 {digest}，实际收到 {observed}", node_digest=digest,
            )
            return
        if len(fetched.body) != node["size"]:
            self.journal.update_node(self.run_id, digest, role, REJECTED)
            self.journal.problem(
                self.run_id, "size_mismatch",
                f"{role} {digest} 声明长度 {node['size']}，实际 {len(fetched.body)}",
                node_digest=digest,
            )
            return
        content_type = normalize_content_type(fetched.headers.get("content-type", ""))
        if content_type and content_type != node["media_type"]:
            # 摘要与长度是硬门槛；媒体类型不一致保留为可追查的警告，不静默改写。
            self.journal.problem(
                self.run_id, "media_type_mismatch",
                f"{role} {digest} 描述符声明 {node['media_type']}，响应 Content-Type 为 {content_type}",
                severity="warning", node_digest=digest,
            )
        if role in MANIFEST_ROLES:
            self._verify_manifest(node, fetched.body, path)
        else:
            self.journal.update_node(self.run_id, digest, role, VERIFIED)

    def _verify_manifest(self, node, body, path):
        digest, role = node["digest"], node["role"]
        try:
            parsed = json.loads(body)
        except ValueError as error:
            self.journal.update_node(self.run_id, digest, role, REJECTED)
            self.journal.problem(
                self.run_id, "invalid_manifest", f"{digest} 不是合法 JSON：{error}",
                node_digest=digest,
            )
            return
        if not isinstance(parsed, dict) or parsed.get("schemaVersion") != 2:
            self.journal.update_node(self.run_id, digest, role, REJECTED)
            self.journal.problem(
                self.run_id, "invalid_manifest", f"{digest} 缺少 schemaVersion=2",
                node_digest=digest,
            )
            return
        media_type = parsed.get("mediaType", node["media_type"])
        if media_type in INDEX_MEDIA_TYPES:
            self.journal.update_node(self.run_id, digest, role, VERIFIED)
            self._expand_index(digest, parsed)
        elif media_type in IMAGE_MEDIA_TYPES:
            if role == "referrer" and self.run["root_digest"] is not None:
                subject = parsed.get("subject")
                if not isinstance(subject, dict) or subject.get("digest") != self.run["root_digest"]:
                    self.journal.update_node(self.run_id, digest, role, REJECTED)
                    self.journal.problem(
                        self.run_id, "subject_mismatch",
                        f"关联制品 {digest} 的 subject={subject if isinstance(subject, dict) else None}，"
                        f"必须与验收摘要 {self.run['root_digest']} 一致，拒绝串入",
                        node_digest=digest,
                    )
                    return
            self.journal.update_node(self.run_id, digest, role, VERIFIED)
            self._expand_image(digest, parsed, role)
        else:
            self.journal.update_node(self.run_id, digest, role, REJECTED)
            self.journal.problem(
                self.run_id, "unsupported_media_type",
                f"{digest} 清单媒体类型无法识别：{media_type}（{path}）", node_digest=digest,
            )

    # ---- 描述符展开与平台选择 ------------------------------------------

    def _expand_manifest(self, digest, role, parsed):
        media_type = parsed.get("mediaType", "")
        if media_type in INDEX_MEDIA_TYPES:
            self._expand_index(digest, parsed)
        elif media_type in IMAGE_MEDIA_TYPES:
            self._expand_image(digest, parsed, role)

    def _expand_index(self, parent_digest, parsed):
        entries = parsed.get("manifests")
        if not isinstance(entries, list):
            self.journal.problem(
                self.run_id, "invalid_manifest", f"index {parent_digest} 缺少 manifests 数组",
                node_digest=parent_digest,
            )
            return
        for position, descriptor in enumerate(entries):
            if not self._valid_descriptor(descriptor, parent_digest, position):
                continue
            platform = descriptor.get("platform")
            child_role = "referrer" if self._is_referrer_index(parent_digest) else "platform"
            if not self._selected(platform):
                self.journal.enqueue(
                    self.run_id, descriptor, child_role, SKIPPED,
                    platform=platform, artifact_type=descriptor.get("artifactType"),
                )
                self.journal.add_edge(self.run_id, parent_digest, descriptor["digest"],
                                      "index_member", position)
                self.journal.problem(
                    self.run_id, "platform_not_selected",
                    f"平台 {platform or '（未声明）'} 不在本地选择策略内，跳过但保留描述符登记",
                    severity="warning", node_digest=descriptor["digest"],
                )
                continue
            self.journal.enqueue(
                self.run_id, descriptor, child_role, EXPECTED,
                platform=platform, artifact_type=descriptor.get("artifactType"),
            )
            self.journal.add_edge(self.run_id, parent_digest, descriptor["digest"],
                                  "index_member", position)

    def _is_referrer_index(self, parent_digest):
        row = self.journal.connection.execute(
            "SELECT role FROM acceptance_nodes WHERE run_id=? AND digest=?",
            (self.run_id, parent_digest),
        ).fetchone()
        return row is not None and row["role"] == "referrer"

    def _expand_image(self, parent_digest, parsed, parent_role):
        prefix = "referrer_" if parent_role == "referrer" else ""
        config = parsed.get("config")
        if config is not None:
            if self._valid_descriptor(config, parent_digest, "config"):
                self.journal.enqueue(self.run_id, config, prefix + "config")
                self.journal.add_edge(self.run_id, parent_digest, config["digest"], "config")
        layers = parsed.get("layers")
        if not isinstance(layers, list):
            self.journal.problem(
                self.run_id, "invalid_manifest", f"清单 {parent_digest} 缺少 layers 数组",
                node_digest=parent_digest,
            )
            return
        for position, descriptor in enumerate(layers):
            if not self._valid_descriptor(descriptor, parent_digest, f"layer[{position}]"):
                continue
            self.journal.enqueue(self.run_id, descriptor, prefix + "layer")
            self.journal.add_edge(self.run_id, parent_digest, descriptor["digest"],
                                  "layer", position)

    def _valid_descriptor(self, descriptor, parent_digest, position):
        if not isinstance(descriptor, dict):
            self.journal.problem(
                self.run_id, "invalid_descriptor",
                f"{parent_digest} 的 {position} 不是描述符对象", node_digest=parent_digest,
            )
            return False
        problems = []
        try:
            parse_digest(descriptor.get("digest", ""))
        except ValueError:
            problems.append("digest 非法")
        size = descriptor.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            problems.append("size 非法")
        if not isinstance(descriptor.get("mediaType"), str) or not descriptor["mediaType"].strip():
            problems.append("mediaType 缺失")
        if problems:
            self.journal.problem(
                self.run_id, "invalid_descriptor",
                f"{parent_digest} 的 {position} 描述符无效：{'、'.join(problems)}",
                node_digest=parent_digest,
            )
            return False
        return True

    def _selected(self, platform):
        if not self.policy.platforms:
            return True  # 默认领取 index 声明的全部平台
        if not isinstance(platform, dict):
            return True  # 无平台声明的成员（如证明清单）默认领取
        return any(selector.matches(platform) for selector in self.policy.platforms)

    # ---- 关联制品 ------------------------------------------------------

    def _walk_referrers(self):
        root = self.run["root_digest"]
        fetched = self.registry.get(referrers_path(self.repository, root),
                                    headers={"Accept": REFERRERS_ACCEPT})
        if fetched.status == 200:
            self.journal.set_referrers(self.run_id, REF_API, fetched.capture_id)
            self._ingest_referrer_index(root, fetched.body, "referrers API")
        elif fetched.status == 404:
            # 接口不存在与“确实没有关联制品”是两件事：404 只证明接口不可见。
            self.journal.set_referrers(self.run_id, REF_UNSUPPORTED, fetched.capture_id)
            self.journal.problem(
                self.run_id, "referrers_unsupported",
                f"referrers 接口返回 404（{fetched.path}），关联制品可见性未知",
                severity="warning",
            )
            if self.policy.allow_fallback_referrers:
                self._fallback_referrers(root)
            elif self.policy.require_referrers_visibility:
                self.journal.problem(
                    self.run_id, "referrers_visibility_required",
                    "本地策略要求关联制品可见，但接口不支持且未启用兼容索引",
                )
        else:
            self.journal.set_referrers(self.run_id, REF_UNKNOWN, fetched.capture_id)
            self.journal.problem(
                self.run_id, "referrers_http_error",
                f"referrers 接口返回 {fetched.status}，关联制品可见性未知",
            )
            if self.policy.require_referrers_visibility:
                self.journal.problem(
                    self.run_id, "referrers_visibility_required",
                    "本地策略要求关联制品可见，但本次无法取得 referrers 响应",
                )
        self._check_required_artifact_types(root)

    def _fallback_referrers(self, root):
        tag = fallback_tag(root)
        path = manifest_path(self.repository, tag)
        fetched = self.registry.get(path, headers={"Accept": MANIFEST_ACCEPT})
        if fetched.status == 404:
            # 兼容标签也不存在：在当前可见范围内确实没有登记的关联制品。
            self.journal.problem(
                self.run_id, "fallback_tag_empty",
                f"兼容索引标签 {tag} 不存在，按无关联制品处理", severity="warning",
            )
            return
        if fetched.status != 200:
            self.journal.problem(
                self.run_id, "fallback_tag_error",
                f"兼容索引标签 {tag} 返回 {fetched.status}",
            )
            return
        self.journal.set_referrers(self.run_id, REF_FALLBACK, fetched.capture_id)
        self._ingest_referrer_index(root, fetched.body, f"兼容标签 {tag}")

    def _ingest_referrer_index(self, root, body, source_name):
        try:
            parsed = json.loads(body)
        except ValueError as error:
            self.journal.problem(
                self.run_id, "invalid_manifest",
                f"{source_name} 返回的引用索引不是合法 JSON：{error}",
            )
            return
        if parsed.get("mediaType") not in INDEX_MEDIA_TYPES or not isinstance(parsed.get("manifests"), list):
            self.journal.problem(
                self.run_id, "invalid_manifest",
                f"{source_name} 返回的不是 OCI index 结构",
            )
            return
        for position, descriptor in enumerate(parsed["manifests"]):
            if not self._valid_descriptor(descriptor, root, "referrer"):
                continue
            self.journal.enqueue(
                self.run_id, descriptor, "referrer", EXPECTED,
                artifact_type=descriptor.get("artifactType"),
            )
            self.journal.add_edge(self.run_id, root, descriptor["digest"],
                                  "referrer", position)
        # 关联清单及其 config/layer 同样纳入闭包遍历。
        self._walk_content()

    def _check_required_artifact_types(self, root):
        if not self.policy.required_artifact_types:
            return
        rows = self.journal.connection.execute(
            "SELECT artifact_type,state FROM acceptance_nodes WHERE run_id=? AND role='referrer'",
            (self.run_id,),
        ).fetchall()
        seen = {row["artifact_type"] for row in rows if row["state"] == VERIFIED}
        for artifact_type in sorted(self.policy.required_artifact_types):
            if artifact_type not in seen:
                self.journal.problem(
                    self.run_id, "required_referrer_missing",
                    f"要求的关联制品类型 {artifact_type} 未通过验收（subject 必须为 {root}）",
                )

    # ---- 判定 ----------------------------------------------------------

    def _current_state(self):
        return self.journal.get_run(self.run_id)["state"]

    def _finalize(self):
        states = {row["state"] for row in self.journal.connection.execute(
            "SELECT DISTINCT state FROM acceptance_nodes WHERE run_id=?", (self.run_id,)
        ).fetchall()}
        # 判定以节点状态为准：REJECTED 是内容/引用关系非法；MISSING/EXPECTED
        # 只是闭包尚未领齐，属于不完整而不是拒收。运行级策略问题（要求的关联
        # 制品缺失、可见性必须确定等）单独构成拒收。
        run_level_reject = self.journal.connection.execute(
            "SELECT 1 FROM acceptance_problems WHERE run_id=? AND severity='fatal' "
            "AND code IN ('required_referrer_missing','referrers_visibility_required') LIMIT 1",
            (self.run_id,),
        ).fetchone()
        if REJECTED in states or run_level_reject:
            state = RUN_REJECTED
        elif EXPECTED in states or MISSING in states:
            state = RUN_INCOMPLETE
        else:
            state = RUN_COMPLETE
        self.journal.touch(self.run_id, state)
