import argparse
import json
import tempfile
from pathlib import Path

from .acceptance import AcceptanceRunner
from .archive import Archive
from .client import Client, collect
from .decoding import records
from .oci import Platform, Policy
from .origin import serve


def capture(base_url, archive_path, source, path):
    archive = Archive(archive_path)
    try:
        capture_id, response = collect(Client(base_url), archive, source, path)
        result = {"capture_id": capture_id, "status": response.status, "bytes": len(response.body), "url": response.url}
        if response.status == 200:
            result["source_records"] = records(response.body, response.headers.get("content-type", ""))
        result["archived_observations"] = len(archive.captures())
        return result
    finally:
        archive.close()


def accept(base_url, archive_path, source, registry, repository, reference,
           platforms=(), artifact_types=(), referrers="api", drift_probe=False,
           acceptance_id=None):
    archive = Archive(archive_path)
    try:
        policy = Policy(
            platforms=tuple(Platform.parse(text) for text in platforms),
            required_artifact_types=frozenset(artifact_types),
            referrers=referrers,
            drift_probe=drift_probe,
        )
        runner = AcceptanceRunner(Client(base_url), archive)
        if acceptance_id is None:
            view = runner.start(source=source, registry=registry, repository=repository,
                                reference=reference, policy=policy)
        else:
            view = runner.resume(acceptance_id)
        return view
    finally:
        archive.close()


_EXIT_CODES = {"accepted": 0, "incomplete": 3, "rejected": 4, "running": 5}


def main():
    parser = argparse.ArgumentParser(description="来源响应接收与 OCI 交付物验收")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("demo")
    collector = subcommands.add_parser("capture")
    collector.add_argument("--base-url", required=True)
    collector.add_argument("--archive", type=Path, required=True)
    collector.add_argument("--source", required=True)
    collector.add_argument("--path", default="/feed")

    acceptor = subcommands.add_parser("accept", help="按 OCI 只读接口冻结标签并采集描述符闭包")
    acceptor.add_argument("--base-url", required=True)
    acceptor.add_argument("--archive", type=Path, required=True)
    acceptor.add_argument("--source", required=True)
    acceptor.add_argument("--registry", required=True)
    acceptor.add_argument("--repository", required=True)
    acceptor.add_argument("--reference", required=True)
    acceptor.add_argument("--platform", action="append", default=[],
                          help="os/architecture[/variant]，可重复；缺省表示 index 内全部平台")
    acceptor.add_argument("--require-artifact-type", action="append", default=[],
                          help="必须随附的关联制品 artifactType，可重复")
    acceptor.add_argument("--referrers", choices=("api", "fallback", "skip"), default="api",
                          help="referrers API 缺失时的本地策略")
    acceptor.add_argument("--drift-probe", action="store_true",
                          help="终判后只读复查标签是否移动（不改写冻结视图）")
    acceptor.add_argument("--resume", type=int, default=None,
                          help="恢复指定验收单；恢复后不再请求标签路径")

    server = subcommands.add_parser("serve")
    server.add_argument("--scenario", type=Path)
    args = parser.parse_args()
    if args.command == "demo":
        with tempfile.TemporaryDirectory() as temporary, serve() as origin:
            result = capture(origin.base_url, Path(temporary) / "archive.sqlite3", "local-source", "/feed")
            print(json.dumps({"operation": "raw_capture", **result}, ensure_ascii=False, indent=2))
    elif args.command == "capture":
        print(json.dumps(capture(args.base_url, args.archive, args.source, args.path), ensure_ascii=False, indent=2))
    elif args.command == "accept":
        view = accept(
            args.base_url, args.archive, args.source, args.registry,
            args.repository, args.reference, platforms=args.platform,
            artifact_types=args.require_artifact_type, referrers=args.referrers,
            drift_probe=args.drift_probe, acceptance_id=args.resume,
        )
        print(json.dumps({"operation": "oci_acceptance", **view}, ensure_ascii=False, indent=2))
        raise SystemExit(_EXIT_CODES.get(view["status"], 1))
    else:
        from .origin import load_scenario
        with serve(load_scenario(args.scenario)) as origin:
            print(json.dumps({"base_url": origin.base_url}, ensure_ascii=False), flush=True)
            try:
                import threading
                threading.Event().wait()
            except KeyboardInterrupt:
                pass


if __name__ == "__main__":
    main()
