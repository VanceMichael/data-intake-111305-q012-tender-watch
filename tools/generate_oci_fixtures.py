"""生成 OCI 验收演练场景（纯标准库，可离线重复运行）。

所有 digest/size 都由实际字节计算，避免手写哈希漂移。
场景里的 layer/blob 均为不可执行合成字节；本脚本不启动容器、不执行内容。
用法：python3 tools/generate_oci_fixtures.py
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

OUTPUT = Path(__file__).resolve().parents[1] / "fixtures" / "oci"

DELIVERY_NOTE_TYPE = "application/vnd.example.delivery-note.v1+json"
SIGNATURE_TYPE = "application/vnd.example.signature.v1+json"


def digest(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def descriptor(body: bytes, media_type: str, **extra) -> dict:
    item = {"mediaType": media_type, "digest": digest(body), "size": len(body)}
    item.update(extra)
    return item


def route(body: bytes, content_type: str, status: int = 200) -> dict:
    return {"body": body.decode("utf-8"), "content_type": content_type, "status": status}


def manifest_route(body: bytes, media_type: str, status: int = 200) -> dict:
    return route(body, media_type, status)


def build_image(platform: dict, layer_payload: bytes, config_extra: dict | None = None):
    """返回 (index 条目平台, config 字节, layer 字节, image manifest 字节, 描述符集合)。"""
    config_doc = {"architecture": platform["architecture"], "os": platform["os"]}
    if "variant" in platform:
        config_doc["variant"] = platform["variant"]
    if config_extra:
        config_doc.update(config_extra)
    config_body = json.dumps(config_doc, separators=(",", ":")).encode()
    layer_body = layer_payload
    manifest_doc = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": descriptor(config_body, "application/vnd.oci.image.config.v1+json"),
        "layers": [descriptor(layer_body, "application/vnd.oci.image.layer.v1.tar")],
    }
    manifest_body = json.dumps(manifest_doc, separators=(",", ":")).encode()
    platform_descriptor = descriptor(
        manifest_body, "application/vnd.oci.image.manifest.v1+json", platform=platform,
    )
    return config_body, layer_body, manifest_body, platform_descriptor, manifest_doc


def index_body(entries: list[dict]) -> bytes:
    return json.dumps({
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": entries,
    }, separators=(",", ":")).encode()


def referrers_index_body(entries: list[dict]) -> bytes:
    return index_body(entries)


def artifact_manifest(subject: dict, blob_body: bytes, artifact_type: str) -> bytes:
    return json.dumps({
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.artifact.manifest.v1+json",
        "artifactType": artifact_type,
        "blobs": [descriptor(blob_body, "application/octet-stream")],
        "subject": subject,
    }, separators=(",", ":")).encode()


def referrer_descriptor(manifest_body: bytes, artifact_type: str) -> dict:
    item = descriptor(manifest_body, "application/vnd.oci.artifact.manifest.v1+json")
    item["artifactType"] = artifact_type
    return item


def base_routes(repository: str, root_body: bytes, root_media: str,
                parts: list[tuple[bytes, str, str]], *, tag: str = "accepted",
                root_status: int = 200) -> dict:
    """parts: (body, media_type, kind) kind ∈ manifest|blob。"""
    routes = {
        f"/v2/{repository}/manifests/{tag}": manifest_route(root_body, root_media, root_status),
    }
    for body, media_type, kind in parts:
        if kind == "manifest":
            routes[f"/v2/{repository}/manifests/{digest(body)}"] = manifest_route(body, media_type)
        else:
            routes[f"/v2/{repository}/blobs/{digest(body)}"] = route(body, media_type)
    return routes


def write_scenario(name: str, routes: dict, *, portal_csrf: str = "form-2026-b") -> None:
    payload = {
        "identity_fields": ["registry", "repository", "reference"],
        "source_records": [],
        "portal": {"csrf": portal_csrf},
        "routes": dict(sorted(routes.items())),
    }
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / f"{name}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )


def main() -> None:
    repository = "vendor/tool"

    # ---- 公共合成内容 -------------------------------------------------
    amd64_layer = b"synthetic amd64 delivery payload, not executable\n"
    arm64_layer = b"synthetic arm64 delivery payload, not executable\n"
    note_blob = b"DELIVERY NOTE: synthetic contents, acceptance only\n"
    signature_blob = b"SIGNATURE BLOCK: synthetic, not executable\n"

    amd64_config, amd64_layer_body, amd64_manifest, amd64_entry, _ = build_image(
        {"os": "linux", "architecture": "amd64"}, amd64_layer,
    )
    arm64_config, arm64_layer_body, arm64_manifest, arm64_entry, _ = build_image(
        {"os": "linux", "architecture": "arm64", "variant": "v8"}, arm64_layer,
    )
    root_index = index_body([amd64_entry, arm64_entry])
    root_digest_value = digest(root_index)

    # 1) tag_moves：标签先指向 amd64 单清单，随后移到多平台 index。
    # 两个标签视图引用的所有 digest 路由都可用，差异只在标签本身指向谁。
    single_index = index_body([amd64_entry])
    routes = base_routes(
        repository, root_index, "application/vnd.oci.image.index.v1+json",
        [
            (amd64_config, "application/vnd.oci.image.config.v1+json", "blob"),
            (amd64_layer_body, "application/octet-stream", "blob"),
            (amd64_manifest, "application/vnd.oci.image.manifest.v1+json", "manifest"),
            (arm64_config, "application/vnd.oci.image.config.v1+json", "blob"),
            (arm64_layer_body, "application/octet-stream", "blob"),
            (arm64_manifest, "application/vnd.oci.image.manifest.v1+json", "manifest"),
            (single_index, "application/vnd.oci.image.index.v1+json", "manifest"),
        ],
    )
    routes[f"/v2/{repository}/manifests/accepted"] = [
        manifest_route(single_index, "application/vnd.oci.image.index.v1+json"),
        manifest_route(root_index, "application/vnd.oci.image.index.v1+json"),
    ]
    routes[f"/v2/{repository}/referrers/{root_digest_value}"] = {
        "status": 404,
        "content_type": "application/json; charset=utf-8",
        "body": json.dumps({"errors": [{"code": "UNSUPPORTED"}]}),
    }
    write_scenario("tag_moves", routes)

    # 2) closure_gap：index 声明两平台，但 arm64 清单与一个 blob 缺失。
    routes = base_routes(
        repository, root_index, "application/vnd.oci.image.index.v1+json",
        [
            (amd64_config, "application/vnd.oci.image.config.v1+json", "blob"),
            (amd64_layer_body, "application/octet-stream", "blob"),
            (amd64_manifest, "application/vnd.oci.image.manifest.v1+json", "manifest"),
            # arm64 manifest / config / layer 故意不注册 -> 404
        ],
    )
    routes[f"/v2/{repository}/referrers/{root_digest_value}"] = {
        "status": 404,
        "content_type": "application/json; charset=utf-8",
        "body": json.dumps({"errors": [{"code": "UNSUPPORTED"}]}),
    }
    write_scenario("closure_gap", routes)

    # ---- 关联制品（referrers）公共内容 --------------------------------
    root_subject = descriptor(root_index, "application/vnd.oci.image.index.v1+json")
    note_manifest = artifact_manifest(root_subject, note_blob, DELIVERY_NOTE_TYPE)
    note_descriptor = referrer_descriptor(note_manifest, DELIVERY_NOTE_TYPE)
    signature_manifest = artifact_manifest(root_subject, signature_blob, SIGNATURE_TYPE)
    signature_descriptor = referrer_descriptor(signature_manifest, SIGNATURE_TYPE)
    referrers_index = referrers_index_body([note_descriptor, signature_descriptor])

    # 单平台根用于只提供 amd64 闭包的关联制品场景。
    single_root = index_body([amd64_entry])
    single_root_digest = digest(single_root)
    single_subject = descriptor(single_root, "application/vnd.oci.image.index.v1+json")
    single_note_manifest = artifact_manifest(single_subject, note_blob, DELIVERY_NOTE_TYPE)
    single_note_descriptor = referrer_descriptor(single_note_manifest, DELIVERY_NOTE_TYPE)

    # 3) referrers_supported：API 200，subject 正确，交付说明随附。
    routes = base_routes(
        repository, root_index, "application/vnd.oci.image.index.v1+json",
        [
            (amd64_config, "application/vnd.oci.image.config.v1+json", "blob"),
            (amd64_layer_body, "application/octet-stream", "blob"),
            (amd64_manifest, "application/vnd.oci.image.manifest.v1+json", "manifest"),
            (arm64_config, "application/vnd.oci.image.config.v1+json", "blob"),
            (arm64_layer_body, "application/octet-stream", "blob"),
            (arm64_manifest, "application/vnd.oci.image.manifest.v1+json", "manifest"),
            (note_manifest, "application/vnd.oci.artifact.manifest.v1+json", "manifest"),
            (note_blob, "application/octet-stream", "blob"),
            (signature_manifest, "application/vnd.oci.artifact.manifest.v1+json", "manifest"),
            (signature_blob, "application/octet-stream", "blob"),
        ],
    )
    routes[f"/v2/{repository}/referrers/{root_digest_value}"] = route(
        referrers_index, "application/vnd.oci.image.index.v1+json",
    )
    write_scenario("referrers_supported", routes)

    # 4) referrers_empty：API 200 但空列表 —— 确实没有关联制品。
    routes = base_routes(
        repository, single_root, "application/vnd.oci.image.index.v1+json",
        [
            (amd64_config, "application/vnd.oci.image.config.v1+json", "blob"),
            (amd64_layer_body, "application/octet-stream", "blob"),
            (amd64_manifest, "application/vnd.oci.image.manifest.v1+json", "manifest"),
        ],
    )
    routes[f"/v2/{repository}/referrers/{single_root_digest}"] = route(
        referrers_index_body([]), "application/vnd.oci.image.index.v1+json",
    )
    write_scenario("referrers_empty", routes)

    # 5) referrers_fallback：API 404（不支持），兼容 tag 索引提供交付说明。
    routes = base_routes(
        repository, single_root, "application/vnd.oci.image.index.v1+json",
        [
            (amd64_config, "application/vnd.oci.image.config.v1+json", "blob"),
            (amd64_layer_body, "application/octet-stream", "blob"),
            (amd64_manifest, "application/vnd.oci.image.manifest.v1+json", "manifest"),
            (single_note_manifest, "application/vnd.oci.artifact.manifest.v1+json", "manifest"),
            (note_blob, "application/octet-stream", "blob"),
        ],
    )
    routes[f"/v2/{repository}/referrers/{single_root_digest}"] = {
        "status": 404,
        "content_type": "application/json; charset=utf-8",
        "body": json.dumps({"errors": [{"code": "UNSUPPORTED"}]}),
    }
    fallback_tag = single_root_digest.replace(":", "-", 1)
    routes[f"/v2/{repository}/manifests/{fallback_tag}"] = route(
        referrers_index_body([single_note_descriptor]),
        "application/vnd.oci.image.index.v1+json",
    )
    write_scenario("referrers_fallback", routes)

    # 6) referrer_subject_mismatch：交付说明的 subject 指向别的对象，必须拒收。
    foreign_digest = "sha256:" + "11" * 32
    foreign_subject = {"mediaType": "application/vnd.oci.image.index.v1+json",
                       "digest": foreign_digest, "size": 512}
    foreign_note_manifest = artifact_manifest(foreign_subject, note_blob, DELIVERY_NOTE_TYPE)
    foreign_note_descriptor = referrer_descriptor(foreign_note_manifest, DELIVERY_NOTE_TYPE)
    routes = base_routes(
        repository, single_root, "application/vnd.oci.image.index.v1+json",
        [
            (amd64_config, "application/vnd.oci.image.config.v1+json", "blob"),
            (amd64_layer_body, "application/octet-stream", "blob"),
            (amd64_manifest, "application/vnd.oci.image.manifest.v1+json", "manifest"),
            (foreign_note_manifest, "application/vnd.oci.artifact.manifest.v1+json", "manifest"),
            (note_blob, "application/octet-stream", "blob"),
        ],
    )
    routes[f"/v2/{repository}/referrers/{single_root_digest}"] = route(
        referrers_index_body([foreign_note_descriptor]),
        "application/vnd.oci.image.index.v1+json",
    )
    write_scenario("referrer_subject_mismatch", routes)

    print("generated scenarios in", OUTPUT)
    for path in sorted(OUTPUT.glob("*.json")):
        print(" -", path.name)


if __name__ == "__main__":
    main()
