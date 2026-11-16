"""生成 OCI 验收演示场景 fixtures/acceptance.json。

全部内容为本地合成：JSON 清单与不可执行字节，不启动容器、不执行负载。
标签移动用来源的响应数组模拟：同一标签路径第一次返回旧 index，
第二次返回新 index；验收器冻结第一次观察到的摘要，之后只按摘要寻址。

运行：python3 -I tools/build_acceptance_fixture.py
"""
import hashlib
import json
from pathlib import Path

REPOSITORY = "vendor/tool"

# 两个平台各自的 config 与不可执行 layer（字节可复用，来源引用各自登记）。
BLOBS = {
    "cfg-amd64": (
        b'{"architecture":"amd64","os":"linux","synthetic":true}',
        "application/vnd.oci.image.config.v1+json",
    ),
    "cfg-arm64": (
        b'{"architecture":"arm64","os":"linux","synthetic":true}',
        "application/vnd.oci.image.config.v1+json",
    ),
    "layer-base": (
        b"synthetic delivery payload, not executable\n",
        "application/vnd.oci.image.layer.v1.tar",
    ),
    "layer-arm64": (
        b"arm64 synthetic delivery payload, not executable\n",
        "application/vnd.oci.image.layer.v1.tar",
    ),
    # 交付说明类关联制品自己的 config/layer。
    "note-config": (
        b'{"note":"synthetic delivery note config"}',
        "application/vnd.oci.image.config.v1+json",
    ),
    "note-layer": (
        b"Delivery note: synthetic, unsigned, not executable.\n",
        "application/vnd.oci.artifact.note.v1+json",
    ),
}


def digest(body):
    return "sha256:" + hashlib.sha256(body).hexdigest()


def blob_descriptor(body, media_type):
    return {"mediaType": media_type, "size": len(body), "digest": digest(body)}


def manifest_body(media_type, **payload):
    document = {"schemaVersion": 2, "mediaType": media_type, **payload}
    return json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode()


def main():
    routes = {}

    def add_blob(key):
        body, media_type = BLOBS[key]
        routes[f"/v2/{REPOSITORY}/blobs/{digest(body)}"] = {
            "body": body.decode("utf-8"),
            "content_type": "application/octet-stream"
            if media_type.startswith("application/vnd.oci.image.layer") else media_type,
            "declared_media_type": media_type,
        }
        return blob_descriptor(body, media_type)

    cfg_amd64 = add_blob("cfg-amd64")
    cfg_arm64 = add_blob("cfg-arm64")
    layer_base = add_blob("layer-base")
    layer_arm64 = add_blob("layer-arm64")
    note_config = add_blob("note-config")
    note_layer = add_blob("note-layer")

    # 平台 image manifest。
    amd64_body = manifest_body(
        "application/vnd.oci.image.manifest.v1+json",
        config=cfg_amd64, layers=[layer_base],
    )
    arm64_body = manifest_body(
        "application/vnd.oci.image.manifest.v1+json",
        config=cfg_arm64, layers=[layer_base, layer_arm64],
    )
    amd64_descriptor = blob_descriptor(amd64_body, "application/vnd.oci.image.manifest.v1+json")
    arm64_descriptor = blob_descriptor(arm64_body, "application/vnd.oci.image.manifest.v1+json")
    amd64_descriptor["platform"] = {"os": "linux", "architecture": "amd64"}
    arm64_descriptor["platform"] = {"os": "linux", "architecture": "arm64"}

    old_index_body = manifest_body(
        "application/vnd.oci.image.index.v1+json",
        manifests=[amd64_descriptor, arm64_descriptor],
    )

    # 标签移动后的新 index：amd64 平台清单换成另一份内容（config 仍可复用）。
    moved_amd64_body = manifest_body(
        "application/vnd.oci.image.manifest.v1+json",
        config=cfg_amd64, layers=[layer_arm64],
        annotations={"revision": "moved"},
    )
    moved_amd64 = blob_descriptor(moved_amd64_body, "application/vnd.oci.image.manifest.v1+json")
    moved_amd64["platform"] = {"os": "linux", "architecture": "amd64"}
    new_index_body = manifest_body(
        "application/vnd.oci.image.index.v1+json",
        manifests=[moved_amd64, arm64_descriptor],
    )

    # 关联制品（交付说明）：subject 指向“旧 index 摘要”，即被验收标签的冻结摘要。
    old_index_digest = digest(old_index_body)
    note_body = manifest_body(
        "application/vnd.oci.image.manifest.v1+json",
        config=note_config, layers=[note_layer],
        subject={"mediaType": "application/vnd.oci.image.index.v1+json",
                 "size": len(old_index_body), "digest": old_index_digest},
        artifactType="application/vnd.example.delivery-note.v1+json",
    )
    note_descriptor = blob_descriptor(note_body, "application/vnd.oci.image.manifest.v1+json")
    note_descriptor["artifactType"] = "application/vnd.example.delivery-note.v1+json"

    referrers_index_body = manifest_body(
        "application/vnd.oci.image.index.v1+json", manifests=[note_descriptor]
    )
    fallback_tag = "sha256-" + old_index_digest.split(":", 1)[1]

    # 清单路由（含摘要寻址，恢复与继续都走这些路径）。
    manifest_routes = [
        (amd64_descriptor["digest"], amd64_body, "application/vnd.oci.image.manifest.v1+json"),
        (arm64_descriptor["digest"], arm64_body, "application/vnd.oci.image.manifest.v1+json"),
        (moved_amd64["digest"], moved_amd64_body, "application/vnd.oci.image.manifest.v1+json"),
        (note_descriptor["digest"], note_body, "application/vnd.oci.image.manifest.v1+json"),
        (old_index_digest, old_index_body, "application/vnd.oci.image.index.v1+json"),
        (digest(new_index_body), new_index_body, "application/vnd.oci.image.index.v1+json"),
    ]
    for ref, body, media_type in manifest_routes:
        routes[f"/v2/{REPOSITORY}/manifests/{ref}"] = {
            "body": body.decode("utf-8"), "content_type": media_type,
        }

    # 标签路径：第一次旧 index，之后保持新 index（模拟抓取期间标签移动）。
    routes[f"/v2/{REPOSITORY}/manifests/accepted"] = [
        {"body": old_index_body.decode("utf-8"),
         "content_type": "application/vnd.oci.image.index.v1+json"},
        {"body": new_index_body.decode("utf-8"),
         "content_type": "application/vnd.oci.image.index.v1+json"},
    ]

    # referrers：本场景来源不支持该接口（404 不等于空列表），
    # 兼容标签 sha256-<hex> 提供同一组关联制品。
    routes[f"/v2/{REPOSITORY}/referrers/{old_index_digest}"] = {
        "status": 404,
        "json": {"errors": [{"code": "UNSUPPORTED"}]},
    }
    routes[f"/v2/{REPOSITORY}/manifests/{fallback_tag}"] = {
        "body": referrers_index_body.decode("utf-8"),
        "content_type": "application/vnd.oci.image.index.v1+json",
    }

    scenario = {
        "identity_fields": ["registry", "repository", "reference"],
        "portal": {"csrf": "form-2026-b"},
        "source_records": [
            {"registry": "delivery-local", "repository": REPOSITORY, "reference": "accepted",
             "descriptor": blob_descriptor(old_index_body,
                                           "application/vnd.oci.image.index.v1+json")},
        ],
        "routes": routes,
    }
    target = Path(__file__).resolve().parents[1] / "fixtures" / "acceptance.json"
    target.write_text(json.dumps(scenario, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("wrote", target)
    print("frozen tag digest:", old_index_digest)
    print("moved tag digest:", digest(new_index_body))
    print("fallback tag:", fallback_tag)


if __name__ == "__main__":
    main()
