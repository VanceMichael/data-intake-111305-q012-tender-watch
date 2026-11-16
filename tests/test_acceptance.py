"""OCI 交付验收的领域契约测试。

场景在测试内用合成字节即时构建，不依赖固定摘要；内容均为 JSON 与
不可执行字节，不启动容器、不执行负载、不做漏洞扫描。
"""
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from tender_watch.acceptance import (
    EXPECTED,
    MISSING,
    REJECTED,
    RUN_COMPLETE,
    RUN_INCOMPLETE,
    RUN_REJECTED,
    SKIPPED,
    VERIFIED,
    Acceptance,
    FreezeError,
    Journal,
    PlatformSelector,
    Policy,
)
from tender_watch.archive import Archive
from tender_watch.client import Client
from tender_watch.origin import serve
from tender_watch.registry import RegistryClient, digest_of, fallback_tag, manifest_path

REPOSITORY = "vendor/tool"
LAYER_MEDIA = "application/vnd.oci.image.layer.v1.tar"
MANIFEST_MEDIA = "application/vnd.oci.image.manifest.v1+json"
INDEX_MEDIA = "application/vnd.oci.image.index.v1+json"
NOTE_TYPE = "application/vnd.example.delivery-note.v1+json"


def sha(body):
    return "sha256:" + hashlib.sha256(body).hexdigest()


def descriptor(body, media_type, **extra):
    item = {"mediaType": media_type, "size": len(body), "digest": sha(body)}
    item.update(extra)
    return item


def manifest(media_type, **payload):
    return json.dumps({"schemaVersion": 2, "mediaType": media_type, **payload},
                      separators=(",", ":")).encode()


class ScenarioBuilder:
    """按 OCI 路径约定拼装一个只读来源场景。"""

    def __init__(self):
        self.routes = {}
        self.blobs = {}

    def add_blob(self, key, body, media_type=LAYER_MEDIA, content_type=None):
        if isinstance(body, str):
            body = body.encode()
        self.blobs[key] = (body, media_type)
        self.routes[f"/v2/{REPOSITORY}/blobs/{sha(body)}"] = {
            "body": body.decode("utf-8"),
            "content_type": content_type or "application/octet-stream",
        }
        return descriptor(body, media_type)

    def add_manifest(self, reference, body, media_type):
        self.routes[manifest_path(REPOSITORY, reference)] = {
            "body": body.decode("utf-8"), "content_type": media_type,
        }
        return body

    def tag(self, name, responses):
        normalized = []
        for response in responses:
            response = dict(response)
            if isinstance(response.get("body"), bytes):
                response["body"] = response["body"].decode("utf-8")
            normalized.append(response)
        self.routes[manifest_path(REPOSITORY, name)] = normalized

    def referrers(self, digest, route):
        route = dict(route)
        if isinstance(route.get("body"), bytes):
            route["body"] = route["body"].decode("utf-8")
        self.routes[f"/v2/{REPOSITORY}/referrers/{digest}"] = route

    def remove(self, path, not_found=True):
        if not_found:
            self.routes[path] = {"status": 404, "json": {"errors": [{"code": "NOT_FOUND"}]}}
        else:
            self.routes.pop(path, None)

    def scenario(self):
        return {"identity_fields": ["registry", "repository", "reference"],
                "portal": {"csrf": "x"}, "source_records": [], "routes": copy.deepcopy(self.routes)}


class Harness:
    def __init__(self, builder, policy=None):
        self.builder = builder
        self.policy = policy or Policy()
        self.temporary = tempfile.TemporaryDirectory()
        self.server_cm = serve(builder.scenario())
        self.server = self.server_cm.__enter__()
        self.archive = Archive(Path(self.temporary.name) / "accept.sqlite3")
        self.registry = RegistryClient(Client(self.server.base_url), self.archive, "fixture")
        self.journal = Journal(self.archive)

    def accept(self, reference="accepted", policy=None):
        acceptance = Acceptance(self.registry, self.journal, REPOSITORY,
                                reference, policy or self.policy)
        return acceptance.start()

    def resume(self, run_id, reference="accepted"):
        acceptance = Acceptance(self.registry, self.journal, REPOSITORY,
                                reference, self.policy)
        return acceptance.resume(run_id)

    def restore_route(self, path, route):
        # 来源在启动时深拷贝了场景，恢复演练要改服务器持有的那份。
        self.server.scenario["routes"][path] = copy.deepcopy(route)

    def report(self, run_id):
        return self.journal.report(run_id)

    def close(self):
        self.archive.close()
        self.server_cm.__exit__(None, None, None)
        self.temporary.cleanup()


def complete_multiplatform():
    """标准双平台 + 关联制品的完整场景，返回 builder 与关键摘要。"""
    builder = ScenarioBuilder()
    cfg_a = builder.add_blob("cfg-a", b'{"os":"linux","architecture":"amd64"}',
                             "application/vnd.oci.image.config.v1+json",
                             content_type="application/vnd.oci.image.config.v1+json")
    cfg_b = builder.add_blob("cfg-b", b'{"os":"linux","architecture":"arm64"}',
                             "application/vnd.oci.image.config.v1+json",
                             content_type="application/vnd.oci.image.config.v1+json")
    layer_body = b"synthetic not executable\n"
    layer = builder.add_blob("layer", layer_body)
    man_a = manifest(MANIFEST_MEDIA, config=cfg_a, layers=[layer])
    man_b = manifest(MANIFEST_MEDIA, config=cfg_b, layers=[layer])
    desc_a = descriptor(man_a, MANIFEST_MEDIA, platform={"os": "linux", "architecture": "amd64"})
    desc_b = descriptor(man_b, MANIFEST_MEDIA, platform={"os": "linux", "architecture": "arm64"})
    builder.add_manifest(desc_a["digest"], man_a, MANIFEST_MEDIA)
    builder.add_manifest(desc_b["digest"], man_b, MANIFEST_MEDIA)
    index = manifest(INDEX_MEDIA, manifests=[desc_a, desc_b])
    builder.tag("accepted", [{"body": index, "content_type": INDEX_MEDIA}])
    builder.add_manifest(sha(index), index, INDEX_MEDIA)
    return builder, {"index": index, "index_digest": sha(index),
                     "man_a": man_a, "man_b": man_b, "layer": layer_body,
                     "desc_a": desc_a, "desc_b": desc_b}


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.harnesses = []

    def tearDown(self):
        for harness in self.harnesses:
            harness.close()

    def use(self, builder, policy=None):
        harness = Harness(builder, policy)
        self.harnesses.append(harness)
        return harness

    def test_full_closure_verifies_every_descriptor_with_provenance(self):
        builder, parts = complete_multiplatform()
        harness = self.use(builder)
        run_id = harness.accept()
        report = harness.report(run_id)
        self.assertEqual(report["state"], RUN_COMPLETE)
        self.assertEqual(report["closure"],
                         {"total": 6, "verified": 6, "missing": 0, "rejected": 0,
                          "skipped": 0, "pending": 0})
        # 每个节点的媒体类型、长度、摘要都来自描述符并逐字节核对。
        for node in report["nodes"]:
            self.assertEqual(node["state"], VERIFIED)
            self.assertTrue(node["media_type"])
            self.assertGreaterEqual(node["size"], 0)
            entries = report["provenance"][node["digest"]]
            self.assertTrue(entries)
            for entry in entries:
                self.assertEqual(entry["status"], 200)
                self.assertEqual(entry["observed_digest"], node["digest"])
        relations = {(edge["relation"], edge["child_digest"]) for edge in report["edges"]}
        self.assertIn(("index_member", parts["desc_a"]["digest"]), relations)
        self.assertIn(("layer", sha(parts["layer"])), relations)

    def test_tag_move_during_walk_keeps_frozen_view(self):
        builder, parts = complete_multiplatform()
        # 第二次标签响应换成另一份 index；其成员清单在来源里根本不存在。
        other_cfg = builder.add_blob("cfg-x", b'{"other":true}',
                                     "application/vnd.oci.image.config.v1+json",
                                     content_type="application/vnd.oci.image.config.v1+json")
        other_man = manifest(MANIFEST_MEDIA, config=other_cfg, layers=[])
        other_desc = descriptor(other_man, MANIFEST_MEDIA,
                                platform={"os": "linux", "architecture": "amd64"})
        moved_index = manifest(INDEX_MEDIA, manifests=[other_desc])
        builder.tag("accepted", [
            {"body": parts["index"], "content_type": INDEX_MEDIA},
            {"body": moved_index, "content_type": INDEX_MEDIA},
        ])
        harness = self.use(builder)
        run_id = harness.accept()
        report = harness.report(run_id)
        self.assertEqual(report["root"]["digest"], parts["index_digest"])
        self.assertEqual(report["state"], RUN_COMPLETE)
        # 新标签指向的清单从未进入这次验收视图。
        self.assertNotIn(sha(other_man), report["provenance"])
        tag_fetches = [
            entry for entries in report["provenance"].values() for entry in entries
            if entry["url"].endswith("/manifests/accepted")
        ]
        self.assertEqual(len(tag_fetches), 1)

    def test_resume_appends_evidence_without_following_new_tag(self):
        builder, parts = complete_multiplatform()
        # 先让一个 blob 缺失，恢复时再补回。
        layer_digest = sha(parts["layer"])
        missing_route = builder.routes.pop(f"/v2/{REPOSITORY}/blobs/{layer_digest}")
        harness = self.use(builder)
        run_id = harness.accept()
        report = harness.report(run_id)
        self.assertEqual(report["state"], RUN_INCOMPLETE)
        self.assertEqual(report["closure"]["missing"], 1)
        missing = [n for n in report["nodes"] if n["state"] == MISSING]
        self.assertEqual(missing[0]["digest"], layer_digest)
        codes = {problem["code"] for problem in report["problems"]}
        self.assertIn("content_not_found", codes)
        # 标签同时已移动；恢复必须沿用冻结摘要而不是重新解析标签。
        moved = manifest(INDEX_MEDIA, manifests=[])
        tag_path = manifest_path(REPOSITORY, "accepted")
        with harness.server.state_lock:
            harness.server.scenario["routes"][tag_path] = [
                {"body": parts["index"].decode("utf-8"), "content_type": INDEX_MEDIA},
                {"body": moved.decode("utf-8"), "content_type": INDEX_MEDIA},
            ]
            harness.server.offsets[tag_path] = 1  # 标签已停在新摘要
        harness.restore_route(
            f"/v2/{REPOSITORY}/blobs/{layer_digest}", missing_route
        )
        harness.resume(run_id)
        report = harness.report(run_id)
        self.assertEqual(report["state"], RUN_COMPLETE)
        self.assertEqual(report["root"]["digest"], parts["index_digest"])
        # 同一描述符的失败与成功两次来源观察都保留。
        entries = report["provenance"][layer_digest]
        self.assertEqual([entry["status"] for entry in entries], [404, 200])
        tag_fetches = [
            entry for entries in report["provenance"].values() for entry in entries
            if entry["url"].endswith("/manifests/accepted")
        ]
        self.assertEqual(len(tag_fetches), 1)

    def test_missing_platform_manifest_is_named_in_closure_report(self):
        builder, parts = complete_multiplatform()
        builder.remove(manifest_path(REPOSITORY, parts["desc_b"]["digest"]))
        harness = self.use(builder)
        run_id = harness.accept()
        report = harness.report(run_id)
        self.assertEqual(report["state"], RUN_INCOMPLETE)
        missing = [n for n in report["nodes"] if n["state"] == MISSING]
        self.assertEqual([n["digest"] for n in missing], [parts["desc_b"]["digest"]])
        problem = next(p for p in report["problems"] if p["code"] == "content_not_found")
        self.assertIn(parts["desc_b"]["digest"], problem["detail"])
        self.assertIn("platform", problem["detail"])

    def test_digest_and_size_mismatch_are_rejected_not_guessed(self):
        builder, parts = complete_multiplatform()
        tampered = parts["man_a"] + b" "
        builder.routes[manifest_path(REPOSITORY, parts["desc_a"]["digest"])]["body"] = tampered
        harness = self.use(builder)
        run_id = harness.accept()
        report = harness.report(run_id)
        self.assertEqual(report["state"], RUN_REJECTED)
        node = next(n for n in report["nodes"] if n["digest"] == parts["desc_a"]["digest"])
        self.assertEqual(node["state"], REJECTED)
        # 被拒收清单的 config/layer 不会被当作已验收内容。
        self.assertIn("digest_mismatch", {p["code"] for p in report["problems"]})
        # 恢复重试仍是拒收，证据继续追加。
        harness.resume(run_id)
        report = harness.report(run_id)
        self.assertEqual(report["state"], RUN_REJECTED)

        builder2, parts2 = complete_multiplatform()
        bad_desc = dict(parts2["desc_a"])
        bad_desc["size"] = parts2["desc_a"]["size"] + 10
        bad_index = manifest(INDEX_MEDIA, manifests=[bad_desc, parts2["desc_b"]])
        builder2.tag("accepted", [{"body": bad_index, "content_type": INDEX_MEDIA}])
        builder2.add_manifest(sha(bad_index), bad_index, INDEX_MEDIA)
        harness2 = self.use(builder2)
        run2 = harness2.accept()
        report2 = harness2.report(run2)
        self.assertEqual(report2["state"], RUN_REJECTED)
        self.assertIn("size_mismatch", {p["code"] for p in report2["problems"]})

    def test_platform_selector_skips_unselected_but_keeps_descriptor_registered(self):
        builder, parts = complete_multiplatform()
        policy = Policy(platforms=(PlatformSelector("linux", "amd64"),))
        harness = self.use(builder, policy)
        run_id = harness.accept()
        report = harness.report(run_id)
        self.assertEqual(report["state"], RUN_COMPLETE)
        skipped = [n for n in report["nodes"] if n["state"] == SKIPPED]
        self.assertEqual([n["digest"] for n in skipped], [parts["desc_b"]["digest"]])
        # 跳过的平台描述符仍登记在 index_member 边上，可审计。
        self.assertIn(parts["desc_b"]["digest"],
                      {edge["child_digest"] for edge in report["edges"]})
        self.assertNotIn(parts["desc_b"]["digest"], report["provenance"])
        self.assertIn("platform_not_selected", {p["code"] for p in report["problems"]})

    def test_referrers_404_is_distinct_from_empty_and_fallback_is_policy_gated(self):
        builder, parts = complete_multiplatform()
        note_cfg = builder.add_blob("note-cfg", b'{"note":true}',
                                    "application/vnd.oci.image.config.v1+json",
                                    content_type="application/vnd.oci.image.config.v1+json")
        note_layer = builder.add_blob("note-layer", b"note bytes, not executable\n",
                                      NOTE_TYPE)
        note = manifest(MANIFEST_MEDIA, config=note_cfg, layers=[note_layer],
                        subject={"mediaType": INDEX_MEDIA, "size": len(parts["index"]),
                                 "digest": parts["index_digest"]},
                        artifactType=NOTE_TYPE)
        note_desc = descriptor(note, MANIFEST_MEDIA, artifactType=NOTE_TYPE)
        builder.add_manifest(note_desc["digest"], note, MANIFEST_MEDIA)
        referrers_index = manifest(INDEX_MEDIA, manifests=[note_desc])
        builder.referrers(parts["index_digest"],
                          {"status": 404, "json": {"errors": [{"code": "UNSUPPORTED"}]}})
        tag = fallback_tag(parts["index_digest"])
        builder.routes[manifest_path(REPOSITORY, tag)] = {
            "body": referrers_index, "content_type": INDEX_MEDIA,
        }

        # 默认策略：不使用兼容索引，关联制品可见性标记为不支持但验收仍可完成。
        harness = self.use(builder)
        run_id = harness.accept()
        report = harness.report(run_id)
        self.assertEqual(report["referrers"]["state"], "api_unsupported")
        self.assertNotIn(note_desc["digest"], report["provenance"])
        self.assertIn("referrers_unsupported", {p["code"] for p in report["problems"]})
        self.assertEqual(report["state"], RUN_COMPLETE)

        # 要求关联制品可见但未启用兼容索引：记为问题。
        strict = Policy(require_referrers_visibility=True)
        harness_strict = self.use(builder, strict)
        run_strict = harness_strict.accept()
        report_strict = harness_strict.report(run_strict)
        self.assertIn("referrers_visibility_required",
                      {p["code"] for p in report_strict["problems"]})

        # 显式启用兼容索引后领取关联制品，并闭合其 config/layer。
        allowed = Policy(allow_fallback_referrers=True,
                         required_artifact_types=frozenset({NOTE_TYPE}))
        harness_fb = self.use(builder, allowed)
        run_fb = harness_fb.accept()
        report_fb = harness_fb.report(run_fb)
        self.assertEqual(report_fb["referrers"]["state"], "fallback_tag")
        referrer_nodes = [n for n in report_fb["nodes"] if n["role"] == "referrer"]
        self.assertEqual(referrer_nodes[0]["state"], VERIFIED)
        self.assertEqual(report_fb["state"], RUN_COMPLETE)

    def test_referrer_with_wrong_subject_is_rejected(self):
        builder, parts = complete_multiplatform()
        foreign = "sha256:" + "0" * 64
        note_cfg = builder.add_blob("ncfg", b'{"note":true}',
                                    "application/vnd.oci.image.config.v1+json",
                                    content_type="application/vnd.oci.image.config.v1+json")
        note = manifest(MANIFEST_MEDIA, config=note_cfg, layers=[],
                        subject={"mediaType": INDEX_MEDIA, "size": 11, "digest": foreign},
                        artifactType=NOTE_TYPE)
        note_desc = descriptor(note, MANIFEST_MEDIA, artifactType=NOTE_TYPE)
        builder.add_manifest(note_desc["digest"], note, MANIFEST_MEDIA)
        referrers_index = manifest(INDEX_MEDIA, manifests=[note_desc])
        builder.referrers(parts["index_digest"],
                          {"body": referrers_index, "content_type": INDEX_MEDIA})
        harness = self.use(builder)
        run_id = harness.accept()
        report = harness.report(run_id)
        self.assertEqual(report["state"], RUN_REJECTED)
        node = next(n for n in report["nodes"] if n["digest"] == note_desc["digest"])
        self.assertEqual(node["state"], REJECTED)
        problem = next(p for p in report["problems"] if p["code"] == "subject_mismatch")
        self.assertIn(parts["index_digest"], problem["detail"])
        # 被串入清单的 config 不进入验收闭包。
        self.assertNotIn(note_cfg["digest"], report["provenance"])

    def test_required_artifact_type_absent_fails_acceptance(self):
        builder, parts = complete_multiplatform()
        builder.referrers(parts["index_digest"],
                          {"body": manifest(INDEX_MEDIA, manifests=[]),
                           "content_type": INDEX_MEDIA})
        policy = Policy(required_artifact_types=frozenset({NOTE_TYPE}))
        harness = self.use(builder, policy)
        run_id = harness.accept()
        report = harness.report(run_id)
        self.assertIn("required_referrer_missing", {p["code"] for p in report["problems"]})
        self.assertEqual(report["state"], RUN_REJECTED)

    def test_content_reuse_does_not_lose_repository_or_reference_edges(self):
        builder, parts = complete_multiplatform()  # 两个平台共享同一 layer
        harness = self.use(builder)
        run_id = harness.accept()
        report = harness.report(run_id)
        layer_edges = [e for e in report["edges"]
                       if e["child_digest"] == sha(parts["layer"])]
        self.assertEqual(len(layer_edges), 2)  # 两条 layer 引用边分别留痕
        distinct_digests = len({n["digest"] for n in report["nodes"]})
        # 每个验收节点的字节都在 bodies 中有且仅有一份（共享 layer 已去重）。
        stored = {row[0] for row in harness.archive.connection.execute(
            "SELECT digest FROM bodies")}
        node_digests = {n["digest"].split(":", 1)[1] for n in report["nodes"]}
        self.assertTrue(node_digests.issubset(stored))
        self.assertEqual(len(node_digests), distinct_digests)
        entries = report["provenance"][sha(parts["layer"])]
        # 共享字节只抓取一次、按摘要复用；两条引用边保证引用关系不丢。
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["observed_digest"], sha(parts["layer"]))
        self.assertEqual({e["url"] for e in entries},
                         {f"{harness.server.base_url}/v2/{REPOSITORY}/blobs/{sha(parts['layer'])}"})

    def test_freeze_failure_records_evidence_and_cannot_be_resumed(self):
        builder = ScenarioBuilder()
        builder.routes[manifest_path(REPOSITORY, "accepted")] = {"status": 503, "body": "down"}
        harness = self.use(builder)
        with self.assertRaises(FreezeError):
            harness.accept()
        run = harness.journal.list_runs()[0]
        self.assertEqual(run["state"], "failed")
        self.assertIsNone(run["root_digest"])
        # 原始响应仍已归档，可查失败原因。
        self.assertTrue(harness.archive.captures())
        with self.assertRaisesRegex(ValueError, "never reached a frozen root"):
            harness.resume(run["id"])

    def test_digest_reference_returning_other_content_is_rejected(self):
        builder, parts = complete_multiplatform()
        target = parts["desc_a"]["digest"]
        other = parts["man_b"]
        builder.routes[manifest_path(REPOSITORY, target)]["body"] = other
        harness = self.use(builder)
        with self.assertRaises(FreezeError):
            harness.accept(target)

    def test_unknown_manifest_media_type_is_not_silently_parsed(self):
        builder, parts = complete_multiplatform()
        weird = json.dumps({"schemaVersion": 2, "mediaType": "application/x-weird",
                            "manifests": []}).encode()
        weird_desc = descriptor(weird, "application/x-weird",
                                platform={"os": "linux", "architecture": "amd64"})
        weird_index = manifest(INDEX_MEDIA, manifests=[weird_desc])
        builder.add_manifest(weird_desc["digest"], weird, "application/x-weird")
        builder.tag("accepted", [{"body": weird_index, "content_type": INDEX_MEDIA}])
        builder.add_manifest(sha(weird_index), weird_index, INDEX_MEDIA)
        harness = self.use(builder)
        run_id = harness.accept()
        report = harness.report(run_id)
        self.assertEqual(report["state"], RUN_REJECTED)
        self.assertIn("unsupported_media_type", {p["code"] for p in report["problems"]})

    def test_visibility_boundary_stays_inside_declared_repository(self):
        from tender_watch.registry import blob_path, validate_repository
        with self.assertRaises(ValueError):
            blob_path("../escape", "sha256:" + "0" * 64)
        with self.assertRaises(ValueError):
            validate_repository("vendor/tool/../other")
        client = Client("http://127.0.0.1:1")
        for path in ("//host/v2/x", "https://host/v2/x", "relative"):
            with self.assertRaises(ValueError):
                client.fetch(path)


if __name__ == "__main__":
    unittest.main()
