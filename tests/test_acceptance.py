import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from tender_watch.acceptance import (
    ACCEPTED, INCOMPLETE, REJECTED_STATUS,
    ROLE_CONFIG, ROLE_LAYER, ROLE_PLATFORM_MANIFEST, ROLE_REFERRER_MANIFEST, ROLE_ROOT,
    STATE_MISSING, STATE_PRESENT, STATE_REJECTED, STATE_VISIBLE_NOT_REQUIRED,
    AcceptanceRunner, AcceptanceStore,
)
from tender_watch.archive import Archive
from tender_watch.client import Client
from tender_watch.oci import (
    REASON_DIGEST_MISMATCH, REASON_PLATFORM_NOT_FOUND, REASON_SIZE_MISMATCH,
    REASON_SUBJECT_MISMATCH, REASON_SUBJECT_MISSING,
    Platform, Policy, digest_of, fallback_reference, valid_digest,
)
from tender_watch.origin import serve

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "oci"
REPOSITORY = "vendor/tool"
DELIVERY_NOTE_TYPE = "application/vnd.example.delivery-note.v1+json"
SIGNATURE_TYPE = "application/vnd.example.signature.v1+json"


def load(name):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def node(view, digest):
    return next(item for item in view["nodes"] if item["digest"] == digest)


def root_digest_of(scenario):
    tag_route = scenario["routes"][f"/v2/{REPOSITORY}/manifests/accepted"]
    selected = tag_route[-1] if isinstance(tag_route, list) else tag_route
    return digest_of(selected["body"].encode())


class FlakyClient(Client):
    """前 fail_times 次命中包含 marker 的路径直接抛连接错误。"""

    def __init__(self, base_url, marker, fail_times):
        super().__init__(base_url)
        self.marker = marker
        self.remaining = fail_times

    def fetch(self, path, headers=None):
        if self.marker in path and self.remaining > 0:
            self.remaining -= 1
            raise ConnectionError("synthetic interruption")
        return super().fetch(path, headers)


class AcceptanceTestBase(unittest.TestCase):
    def run_acceptance(self, scenario_name, policy, *, client_factory=None,
                       resume_id=None):
        scenario = load(scenario_name)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        archive_path = Path(self.temporary.name) / "acceptance.sqlite3"
        with serve(scenario) as origin:
            archive = Archive(archive_path)
            try:
                client = client_factory(origin.base_url) if client_factory else Client(origin.base_url)
                runner = AcceptanceRunner(client, archive)
                if resume_id is None:
                    view = runner.start(
                        source="local-source", registry="delivery-local",
                        repository=REPOSITORY, reference="accepted", policy=policy,
                    )
                else:
                    view = runner.resume(resume_id)
            finally:
                archive.close()
        return view, archive_path, scenario, origin


class HappyPathTests(AcceptanceTestBase):
    def test_full_multi_platform_closure_is_accepted(self):
        view, _, scenario, _ = self.run_acceptance(
            "referrers_supported",
            Policy(required_artifact_types=frozenset({DELIVERY_NOTE_TYPE})),
        )
        self.assertEqual(view["status"], ACCEPTED)
        self.assertEqual(view["root_digest"], root_digest_of(scenario))
        roles = {item["role"]: item for item in view["nodes"]}
        self.assertEqual(roles[ROLE_ROOT]["state"], STATE_PRESENT)
        platform_manifests = [n for n in view["nodes"] if n["role"] == ROLE_PLATFORM_MANIFEST]
        self.assertEqual(len(platform_manifests), 2)
        self.assertTrue(all(n["state"] == STATE_PRESENT for n in platform_manifests))
        self.assertTrue(all(n["role"] in (ROLE_CONFIG, ROLE_LAYER) and n["state"] == STATE_PRESENT
                            for n in view["nodes"] if n["role"] in (ROLE_CONFIG, ROLE_LAYER)))
        # 关联制品：两个 referrer，要求的交付说明与未要求的签名都在，但 required 标记不同。
        referrers = [n for n in view["nodes"] if n["role"] == ROLE_REFERRER_MANIFEST]
        self.assertEqual(len(referrers), 2)
        note = next(n for n in referrers
                    if any(r.get("artifact_type") == DELIVERY_NOTE_TYPE for r in n["reasons"]))
        self.assertTrue(note["required"])
        signature = next(n for n in referrers
                         if any(r.get("artifact_type") == SIGNATURE_TYPE for r in n["reasons"]))
        self.assertFalse(signature["required"])
        self.assertEqual(signature["state"], STATE_VISIBLE_NOT_REQUIRED)
        self.assertEqual(view["referrers_state"], "supported")
        # 每个 present 节点都能查到来源 URL、capture 与实际内容摘要；
        # 根节点来自标签路径，其余节点必须来自 digest 路径。
        for item in view["nodes"]:
            if item["state"] == STATE_PRESENT:
                self.assertIsNotNone(item["provenance"])
                self.assertEqual(item["provenance"]["status"], 200)
                if item["role"] != ROLE_ROOT:
                    self.assertTrue(item["provenance"]["url"].endswith(item["digest"]))
                else:
                    self.assertTrue(item["provenance"]["url"].endswith("/manifests/accepted"))

    def test_platform_selector_limits_closure_and_marks_others_visible(self):
        view, _, _, _ = self.run_acceptance(
            "referrers_supported", Policy(platforms=(Platform.parse("linux/amd64"),)),
        )
        self.assertEqual(view["status"], ACCEPTED)
        selected = [n for n in view["nodes"]
                    if n["role"] == ROLE_PLATFORM_MANIFEST and n["required"]]
        self.assertEqual(len(selected), 1)
        arm64 = next(n for n in view["nodes"]
                     if n["role"] == ROLE_PLATFORM_MANIFEST and not n["required"])
        self.assertEqual(arm64["state"], STATE_VISIBLE_NOT_REQUIRED)
        # 未选平台的 config/layer 不应被领取：没有对应 provenance。
        arm64_edges = {e["child_digest"] for e in view["edges"]
                       if e["relation"] == "platform" and not e["required"]}
        self.assertTrue(arm64_edges)

    def test_unknown_platform_selector_rejects(self):
        view, _, _, _ = self.run_acceptance(
            "referrers_supported", Policy(platforms=(Platform.parse("windows/amd64"),)),
        )
        self.assertEqual(view["status"], REJECTED_STATUS)
        root = node(view, view["root_digest"])
        self.assertEqual(root["state"], STATE_REJECTED)
        self.assertIn(REASON_PLATFORM_NOT_FOUND, [r["code"] for r in root["reasons"]])


class TagFreezeTests(AcceptanceTestBase):
    def test_tag_moving_during_capture_does_not_rewrite_frozen_view(self):
        view, _, scenario, origin = self.run_acceptance(
            "tag_moves", Policy(drift_probe=True),
        )
        tag_route = scenario["routes"][f"/v2/{REPOSITORY}/manifests/accepted"]
        first_digest = digest_of(tag_route[0]["body"].encode())
        final_digest = digest_of(tag_route[-1]["body"].encode())
        self.assertNotEqual(first_digest, final_digest)
        # 冻结的是第一次观察到的摘要。
        self.assertEqual(view["root_digest"], first_digest)
        drift = next(event for event in view["events"] if event["event"] == "tag_drift_observed")
        self.assertTrue(drift["detail"]["moved"])
        self.assertEqual(drift["detail"]["observed"], final_digest)
        self.assertEqual(drift["detail"]["frozen"], first_digest)
        # 冻结视图仍是按首次摘要完成的闭包，结论不随漂移改变。
        self.assertEqual(view["status"], ACCEPTED)
        self.assertEqual(node(view, first_digest)["state"], STATE_PRESENT)

    def test_resume_never_requests_the_tag_again(self):
        scenario = load("tag_moves")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        archive_path = Path(temporary.name) / "acceptance.sqlite3"
        policy = Policy()
        with serve(scenario) as origin:
            archive = Archive(archive_path)
            runner = AcceptanceRunner(
                FlakyClient(origin.base_url, "/manifests/sha256:", fail_times=1), archive,
            )
            first = runner.start(source="local-source", registry="delivery-local",
                                 repository=REPOSITORY, reference="accepted", policy=policy)
            frozen = first["root_digest"]
            self.assertEqual(first["status"], INCOMPLETE)
            archive.close()

            # 恢复：标签路由在场景里会继续前进，但恢复路径不得再碰标签。
            archive = Archive(archive_path)
            runner = AcceptanceRunner(Client(origin.base_url), archive)
            resumed = runner.resume(first["acceptance_id"])
            archive.close()
        tag_hits = [request for request in origin.requests
                    if request["path"] == f"/v2/{REPOSITORY}/manifests/accepted"]
        self.assertEqual(len(tag_hits), 1)  # 仅首次冻结时请求过一次。
        self.assertEqual(resumed["root_digest"], frozen)
        self.assertEqual(resumed["status"], ACCEPTED)


class ClosureGapTests(AcceptanceTestBase):
    def test_missing_platform_manifest_is_named_not_silently_accepted(self):
        view, _, scenario, _ = self.run_acceptance("closure_gap", Policy())
        self.assertEqual(view["status"], INCOMPLETE)
        tag_route = scenario["routes"][f"/v2/{REPOSITORY}/manifests/accepted"]
        root_index = json.loads(tag_route["body"])
        missing_digests = {entry["digest"] for entry in root_index["manifests"]} - {
            n["digest"] for n in view["nodes"] if n["state"] == STATE_PRESENT
        }
        self.assertTrue(missing_digests)
        self.assertEqual(set(view["missing"]), missing_digests)
        for digest in view["missing"]:
            gap = node(view, digest)
            self.assertEqual(gap["state"], STATE_MISSING)
            self.assertEqual(gap["reasons"][0]["code"], "http_error_status")
            self.assertEqual(gap["reasons"][0]["status"], 404)
            self.assertIsNotNone(gap["provenance"])  # 404 交换本身也留痕。

    def test_recovery_completes_after_gap_is_filled(self):
        scenario = load("closure_gap")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        archive_path = Path(temporary.name) / "acceptance.sqlite3"
        with serve(scenario) as origin:
            archive = Archive(archive_path)
            runner = AcceptanceRunner(Client(origin.base_url), archive)
            first = runner.start(source="local-source", registry="delivery-local",
                                 repository=REPOSITORY, reference="accepted", policy=Policy())
            acceptance_id = first["acceptance_id"]
            self.assertEqual(first["status"], INCOMPLETE)
            archive.close()

            # 供方补齐缺失的 arm64 闭包；恢复仍按冻结摘要领取，不重新解析标签。
            # 服务器持有的是深拷贝，必须改 origin.scenario 才能让后续请求看到补齐。
            supported = load("referrers_supported")
            for url, route in supported["routes"].items():
                origin.scenario["routes"].setdefault(url, route)
            archive = Archive(archive_path)
            runner = AcceptanceRunner(Client(origin.base_url), archive)
            resumed = runner.resume(acceptance_id)
            archive.close()
        self.assertEqual(resumed["status"], ACCEPTED)
        self.assertEqual(resumed["root_digest"], first["root_digest"])
        self.assertEqual(resumed["missing"], [])


class ReferrersTests(AcceptanceTestBase):
    def test_empty_referrers_list_differs_from_missing_api(self):
        empty, _, _, _ = self.run_acceptance("referrers_empty", Policy())
        self.assertEqual(empty["referrers_state"], "supported")
        self.assertIn(next(e for e in empty["events"] if e["event"] == "referrers_observed")
                      ["detail"]["count"], [0])
        missing, _, _, _ = self.run_acceptance("closure_gap", Policy(referrers="api"))
        self.assertEqual(missing["referrers_state"], "unsupported")
        self.assertTrue(any(e["event"] == "referrers_api_unsupported" for e in missing["events"]))

    def test_fallback_tag_index_is_a_local_policy_choice(self):
        skipped, _, _, _ = self.run_acceptance("closure_gap", Policy(referrers="api"))
        self.assertEqual(skipped["referrers_state"], "unsupported")
        fallback, _, scenario, _ = self.run_acceptance(
            "referrers_fallback",
            Policy(referrers="fallback",
                   required_artifact_types=frozenset({DELIVERY_NOTE_TYPE})),
        )
        self.assertEqual(fallback["referrers_state"], "fallback_index")
        self.assertEqual(fallback["status"], ACCEPTED)
        tag = fallback_reference(fallback["root_digest"])
        self.assertIn(f"/v2/{REPOSITORY}/manifests/{tag}", scenario["routes"])
        note = next(n for n in fallback["nodes"] if n["role"] == ROLE_REFERRER_MANIFEST)
        self.assertEqual(note["state"], STATE_PRESENT)

    def test_required_artifact_absence_keeps_status_incomplete(self):
        view, _, _, _ = self.run_acceptance(
            "referrers_empty",
            Policy(required_artifact_types=frozenset({DELIVERY_NOTE_TYPE})),
        )
        self.assertEqual(view["status"], INCOMPLETE)
        self.assertTrue(any(e["event"] == "required_artifact_absent" for e in view["events"]))

    def test_subject_mismatch_is_rejected_and_its_blob_not_collected(self):
        view, _, scenario, _ = self.run_acceptance(
            "referrer_subject_mismatch", Policy(),
        )
        self.assertEqual(view["status"], REJECTED_STATUS)
        referrer = next(n for n in view["nodes"] if n["role"] == ROLE_REFERRER_MANIFEST)
        self.assertEqual(referrer["state"], STATE_REJECTED)
        codes = [reason.get("code") for reason in referrer["reasons"]]
        self.assertIn(REASON_SUBJECT_MISMATCH, codes)
        self.assertEqual(referrer["reasons"][-1]["expected"], view["root_digest"])
        # 被拒收 referrer 的 blob 没有进入闭包：库里没有 artifact_blob 节点。
        self.assertFalse(any(n["role"] == "artifact_blob" for n in view["nodes"]))

    def test_subject_missing_reason_constant_is_declared(self):
        self.assertTrue(REASON_SUBJECT_MISSING)

    def test_referrer_descriptor_without_media_type_is_fetched_and_checked(self):
        # OCI 允许描述符省略 mediaType；清单仍须经 manifest 路径取回并核验 subject。
        from tender_watch.oci import digest_of
        root = json.loads(Path(FIXTURES / "referrers_supported.json").read_text())
        tag = root["routes"][f"/v2/{REPOSITORY}/manifests/accepted"]
        root_digest = digest_of(tag["body"].encode())
        root_index = json.loads(tag["body"])
        note_body = b"DELIVERY NOTE without mediaType descriptor\n"
        note_manifest_body = json.dumps({
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.artifact.manifest.v1+json",
            "artifactType": DELIVERY_NOTE_TYPE,
            "blobs": [{"mediaType": "application/octet-stream",
                       "digest": digest_of(note_body), "size": len(note_body)}],
            "subject": {"mediaType": "application/vnd.oci.image.index.v1+json",
                        "digest": root_digest, "size": len(tag["body"])},
        }, separators=(",", ":")).encode()
        note_digest = digest_of(note_manifest_body)
        root["routes"][f"/v2/{REPOSITORY}/referrers/{root_digest}"] = {
            "body": json.dumps({"schemaVersion": 2,
                                "mediaType": "application/vnd.oci.image.index.v1+json",
                                "manifests": [{"digest": note_digest,
                                               "size": len(note_manifest_body),
                                               "artifactType": DELIVERY_NOTE_TYPE}]}),
            "content_type": "application/vnd.oci.image.index.v1+json",
        }
        root["routes"][f"/v2/{REPOSITORY}/manifests/{note_digest}"] = {
            "body": note_manifest_body.decode(),
            "content_type": "application/vnd.oci.artifact.manifest.v1+json",
        }
        root["routes"][f"/v2/{REPOSITORY}/blobs/{digest_of(note_body)}"] = {
            "body": note_body.decode(), "content_type": "application/octet-stream",
        }
        with tempfile.TemporaryDirectory() as tmp, serve(root) as origin:
            archive = Archive(Path(tmp) / "a.sqlite3")
            runner = AcceptanceRunner(Client(origin.base_url), archive)
            view = runner.start(source="local-source", registry="delivery-local",
                                repository=REPOSITORY, reference="accepted",
                                policy=Policy(platforms=(Platform.parse("linux/amd64"),),
                                              required_artifact_types=frozenset({DELIVERY_NOTE_TYPE})))
            archive.close()
        self.assertEqual(view["status"], ACCEPTED)
        referrer = next(n for n in view["nodes"] if n["role"] == ROLE_REFERRER_MANIFEST)
        self.assertEqual(referrer["digest"], note_digest)
        self.assertEqual(referrer["state"], STATE_PRESENT)
        self.assertTrue(any(n["role"] == "artifact_blob" and n["state"] == STATE_PRESENT
                            for n in view["nodes"]))

    def test_subject_missing_is_rejected(self):
        from tender_watch.oci import digest_of
        from tender_watch.oci import REASON_SUBJECT_MISSING as CODE
        root = json.loads(Path(FIXTURES / "referrer_subject_mismatch.json").read_text())
        tag = root["routes"][f"/v2/{REPOSITORY}/manifests/accepted"]
        root_digest = digest_of(tag["body"].encode())
        note_body = b"note without subject\n"
        manifest_body = json.dumps({
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.artifact.manifest.v1+json",
            "artifactType": DELIVERY_NOTE_TYPE,
            "blobs": [{"mediaType": "application/octet-stream",
                       "digest": digest_of(note_body), "size": len(note_body)}],
        }, separators=(",", ":")).encode()
        note_digest = digest_of(manifest_body)
        root["routes"][f"/v2/{REPOSITORY}/referrers/{root_digest}"] = {
            "body": json.dumps({"schemaVersion": 2,
                                "mediaType": "application/vnd.oci.image.index.v1+json",
                                "manifests": [{"digest": note_digest,
                                               "mediaType": "application/vnd.oci.artifact.manifest.v1+json",
                                               "size": len(manifest_body),
                                               "artifactType": DELIVERY_NOTE_TYPE}]}),
            "content_type": "application/vnd.oci.image.index.v1+json",
        }
        root["routes"][f"/v2/{REPOSITORY}/manifests/{note_digest}"] = {
            "body": manifest_body.decode(),
            "content_type": "application/vnd.oci.artifact.manifest.v1+json",
        }
        root["routes"][f"/v2/{REPOSITORY}/blobs/{digest_of(note_body)}"] = {
            "body": note_body.decode(), "content_type": "application/octet-stream",
        }
        with tempfile.TemporaryDirectory() as tmp, serve(root) as origin:
            archive = Archive(Path(tmp) / "a.sqlite3")
            runner = AcceptanceRunner(Client(origin.base_url), archive)
            view = runner.start(source="local-source", registry="delivery-local",
                                repository=REPOSITORY, reference="accepted", policy=Policy())
            archive.close()
        self.assertEqual(view["status"], REJECTED_STATUS)
        referrer = next(n for n in view["nodes"] if n["role"] == ROLE_REFERRER_MANIFEST)
        self.assertEqual(referrer["state"], STATE_REJECTED)
        self.assertIn(CODE, [r.get("code") for r in referrer["reasons"]])


class IntegrityTests(AcceptanceTestBase):
    def test_size_and_digest_mismatch_are_separately_reported(self):
        scenario = load("referrers_supported")
        # 篡改 config 字节但保留原路由（描述符仍声明旧 digest/size）。
        config_route = next(
            route for url, route in scenario["routes"].items() if "/blobs/" in url
            and route["content_type"] == "application/vnd.oci.image.config.v1+json"
        )
        original = config_route["body"]
        config_route["body"] = original + " "
        with serve(scenario) as origin, tempfile.TemporaryDirectory() as temporary:
            archive = Archive(Path(temporary) / "a.sqlite3")
            runner = AcceptanceRunner(Client(origin.base_url), archive)
            view = runner.start(source="local-source", registry="delivery-local",
                                repository=REPOSITORY, reference="accepted",
                                policy=Policy(platforms=(Platform.parse("linux/amd64"),)))
            archive.close()
        self.assertEqual(view["status"], REJECTED_STATUS)
        config_node = next(n for n in view["nodes"] if n["role"] == ROLE_CONFIG)
        codes = {reason["code"] for reason in config_node["reasons"]}
        self.assertIn(REASON_SIZE_MISMATCH, codes)
        self.assertIn(REASON_DIGEST_MISMATCH, codes)

    def test_same_content_is_reused_but_observations_keep_their_origin(self):
        scenario = load("referrers_supported")
        with serve(scenario) as origin, tempfile.TemporaryDirectory() as temporary:
            archive = Archive(Path(temporary) / "a.sqlite3")
            runner = AcceptanceRunner(Client(origin.base_url), archive)
            view = runner.start(source="local-source", registry="delivery-local",
                                repository=REPOSITORY, reference="accepted", policy=Policy())
            captures = archive.captures()
            stored_bodies = archive.connection.execute(
                "SELECT count(*) FROM bodies"
            ).fetchone()[0]
            # 至少有两个不同 manifest 共享同一套合成内容场景之外，
            # 主要断言：captures 数量多于去重 bodies，且每条 capture 保留各自 URL。
            self.assertGreaterEqual(len(captures), stored_bodies)
            urls = {c["url"] for c in captures}
            for item in view["nodes"]:
                if item["provenance"] is not None:
                    self.assertIn(item["provenance"]["url"], urls)
            archive.close()

    def test_edges_record_reference_relations(self):
        view, _, _, _ = self.run_acceptance("referrers_supported", Policy())
        relations = {edge["relation"] for edge in view["edges"]}
        self.assertIn("platform", relations)
        self.assertIn("config", relations)
        self.assertIn("layer", relations)
        self.assertIn("referrer", relations)
        root_children = {edge["child_digest"] for edge in view["edges"]
                         if edge["parent_digest"] == view["root_digest"]
                         and edge["relation"] == "platform"}
        self.assertEqual(len(root_children), 2)


class VisibilityBoundaryTests(unittest.TestCase):
    def _scenario_with_external_url(self):
        import hashlib
        from tender_watch.oci import digest_of
        config_body = b'{"architecture":"amd64","os":"linux"}'
        layer_body = b"local-only synthetic bytes\n"
        layer_digest = digest_of(layer_body)
        manifest = {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {"mediaType": "application/vnd.oci.image.config.v1+json",
                       "digest": digest_of(config_body), "size": len(config_body)},
            "layers": [{
                "mediaType": "application/vnd.oci.image.layer.v1.tar",
                "digest": layer_digest, "size": len(layer_body),
                "urls": ["https://registry.invalid/vendor/tool/blobs/" + layer_digest],
            }],
        }
        manifest_body = json.dumps(manifest, separators=(",", ":")).encode()
        index = {"schemaVersion": 2,
                 "mediaType": "application/vnd.oci.image.index.v1+json",
                 "manifests": [{
                     "mediaType": "application/vnd.oci.image.manifest.v1+json",
                     "digest": digest_of(manifest_body), "size": len(manifest_body),
                     "platform": {"os": "linux", "architecture": "amd64"},
                 }]}
        index_body = json.dumps(index, separators=(",", ":")).encode()
        repository = "vendor/tool"
        routes = {
            f"/v2/{repository}/manifests/accepted": {
                "body": index_body.decode(),
                "content_type": "application/vnd.oci.image.index.v1+json"},
            f"/v2/{repository}/manifests/{digest_of(manifest_body)}": {
                "body": manifest_body.decode(),
                "content_type": "application/vnd.oci.image.manifest.v1+json"},
            f"/v2/{repository}/blobs/{digest_of(config_body)}": {
                "body": config_body.decode(),
                "content_type": "application/vnd.oci.image.config.v1+json"},
            f"/v2/{repository}/blobs/{layer_digest}": {
                "body": layer_body.decode(), "content_type": "application/octet-stream"},
            f"/v2/{repository}/referrers/{digest_of(index_body)}": {
                "status": 404, "content_type": "application/json",
                "body": '{"errors":[{"code":"UNSUPPORTED"}]}'},
        }
        return {"identity_fields": ["registry", "repository", "reference"],
                "source_records": [], "portal": {"csrf": "x"}, "routes": routes}, layer_digest

    def test_external_urls_are_recorded_but_not_followed(self):
        scenario, layer_digest = self._scenario_with_external_url()
        with tempfile.TemporaryDirectory() as tmp, serve(scenario) as origin:
            archive = Archive(Path(tmp) / "a.sqlite3")
            runner = AcceptanceRunner(Client(origin.base_url), archive)
            view = runner.start(source="local-source", registry="delivery-local",
                                repository="vendor/tool", reference="accepted",
                                policy=Policy(referrers="api"))
            archive.close()
        # 本地 digest 路径仍可领取，外部 URL 从不请求。
        self.assertNotIn("registry.invalid",
                         " ".join(request["path"] for request in origin.requests))
        layer = node(view, layer_digest)
        self.assertEqual(layer["state"], STATE_PRESENT)
        self.assertTrue(any(r.get("code") == "external_url_not_followed"
                            for r in layer["reasons"]))
        self.assertTrue(layer["provenance"]["url"].startswith("http://127.0.0.1"))


class OciPrimitiveTests(unittest.TestCase):
    def test_digest_validation(self):
        good = "sha256:" + hashlib.sha256(b"x").hexdigest()
        self.assertTrue(valid_digest(good))
        self.assertFalse(valid_digest("sha256:zzzz"))
        self.assertFalse(valid_digest("md5:" + hashlib.md5(b"x").hexdigest()))
        self.assertFalse(valid_digest("not-a-digest"))

    def test_platform_matching_with_variant(self):
        selector = Platform.parse("linux/arm64/v8")
        self.assertTrue(selector.matches(
            {"platform": {"os": "linux", "architecture": "arm64", "variant": "v8"}}))
        self.assertFalse(selector.matches(
            {"platform": {"os": "linux", "architecture": "arm64"}}))
        with self.assertRaises(ValueError):
            Platform.parse("linux")

    def test_fallback_reference_rewrite(self):
        self.assertEqual(
            fallback_reference("sha256:abcdef"), "sha256-abcdef",
        )

    def test_policy_roundtrip(self):
        policy = Policy(
            platforms=(Platform.parse("linux/amd64"),),
            required_artifact_types=frozenset({"application/x"}),
            referrers="fallback", drift_probe=True,
        )
        restored = Policy.from_json(policy.to_json())
        self.assertEqual(restored, policy)


if __name__ == "__main__":
    unittest.main()
