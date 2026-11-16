import argparse
import json
import tempfile
from pathlib import Path

from .acceptance import (
    Acceptance,
    FreezeError,
    Journal,
    PlatformSelector,
    Policy,
)
from .archive import Archive
from .client import Client, collect
from .decoding import records
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


def build_policy(args):
    platforms = tuple(
        PlatformSelector(**item) for item in getattr(args, "platform", ())
    )
    return Policy(
        platforms=platforms,
        required_artifact_types=frozenset(getattr(args, "require_artifact", ())),
        allow_fallback_referrers=getattr(args, "fallback_referrers", False),
        require_referrers_visibility=getattr(args, "require_referrers_visibility", False),
    )


def run_acceptance(base_url, archive_path, source, repository, reference, policy, resume_id=None):
    archive = Archive(archive_path)
    try:
        journal = Journal(archive)
        registry_client = Client(base_url)
        from .registry import RegistryClient
        registry = RegistryClient(registry_client, archive, source)
        acceptance = Acceptance(registry, journal, repository, reference, policy)
        if resume_id is None:
            run_id = acceptance.start()
        else:
            run_id = acceptance.resume(resume_id)
        return journal.report(run_id)
    finally:
        archive.close()


def add_policy_arguments(parser):
    parser.add_argument("--platform", action="append", type=parse_platform, default=[],
                        help="平台选择，格式 os/architecture[/variant]，可重复；缺省领取全部平台")
    parser.add_argument("--require-artifact", action="append", default=[],
                        help="要求通过验收的关联制品 artifactType，可重复")
    parser.add_argument("--fallback-referrers", action="store_true",
                        help="referrers 接口不可用时允许使用 sha256-<hex> 兼容标签索引")
    parser.add_argument("--require-referrers-visibility", action="store_true",
                        help="关联制品可见性必须确定，接口不可见又无兼容索引则记为问题")


def parse_platform(text):
    parts = text.split("/")
    if len(parts) not in (2, 3) or not all(parts):
        raise argparse.ArgumentTypeError("平台格式应为 os/architecture[/variant]：" + text)
    payload = {"os": parts[0], "architecture": parts[1]}
    if len(parts) == 3:
        payload["variant"] = parts[2]
    return payload


def main():
    parser = argparse.ArgumentParser(description="来源响应接收与 OCI 交付验收")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("demo")

    collector = subcommands.add_parser("capture")
    collector.add_argument("--base-url", required=True)
    collector.add_argument("--archive", type=Path, required=True)
    collector.add_argument("--source", required=True)
    collector.add_argument("--path", default="/feed")

    accept = subcommands.add_parser("accept", help="按标签或摘要执行一次交付验收")
    accept.add_argument("--base-url", required=True)
    accept.add_argument("--archive", type=Path, required=True)
    accept.add_argument("--source", required=True)
    accept.add_argument("--repository", required=True)
    accept.add_argument("--reference", required=True, help="标签或 sha256 摘要引用")
    add_policy_arguments(accept)

    resume = subcommands.add_parser("resume", help="按冻结视图恢复一次未完成验收，不跟随新标签")
    resume.add_argument("--base-url", required=True)
    resume.add_argument("--archive", type=Path, required=True)
    resume.add_argument("--source", required=True)
    resume.add_argument("--repository", required=True)
    resume.add_argument("--reference", required=True)
    resume.add_argument("--run-id", type=int, required=True)

    report = subcommands.add_parser("accept-report", help="输出一次验收的完整闭包报告")
    report.add_argument("--archive", type=Path, required=True)
    report.add_argument("--run-id", type=int, required=True)

    demo_accept = subcommands.add_parser("demo-acceptance", help="多平台验收、标签移动与兼容关联索引的端到端演示")
    demo_accept.add_argument("--scenario", type=Path)
    add_policy_arguments(demo_accept)

    server = subcommands.add_parser("serve")
    server.add_argument("--scenario", type=Path)
    args = parser.parse_args()

    if args.command == "demo":
        with tempfile.TemporaryDirectory() as temporary, serve() as origin:
            result = capture(origin.base_url, Path(temporary) / "archive.sqlite3",
                             "local-source", "/feed")
            print(json.dumps({"operation": "raw_capture", **result},
                             ensure_ascii=False, indent=2))
        return
    if args.command == "capture":
        print(json.dumps(capture(args.base_url, args.archive, args.source, args.path),
                         ensure_ascii=False, indent=2))
    elif args.command == "accept":
        policy = build_policy(args)
        try:
            result = run_acceptance(args.base_url, args.archive, args.source,
                                    args.repository, args.reference, policy)
        except FreezeError as error:
            print(json.dumps({"operation": "acceptance", "state": "failed",
                              "error": str(error)}, ensure_ascii=False, indent=2))
            raise SystemExit(2)
        print(json.dumps({"operation": "acceptance", **result}, ensure_ascii=False, indent=2))
        raise SystemExit(0 if result["state"] == "complete" else 1)
    elif args.command == "resume":
        # 恢复不接受新策略：策略从台账中的运行记录读取。
        archive = Archive(args.archive)
        try:
            run = Journal(archive).get_run(args.run_id)
        finally:
            archive.close()
        policy = Policy.from_json(run["policy"])
        result = run_acceptance(args.base_url, args.archive, args.source,
                                args.repository, args.reference, policy, resume_id=args.run_id)
        print(json.dumps({"operation": "resume", **result}, ensure_ascii=False, indent=2))
        raise SystemExit(0 if result["state"] == "complete" else 1)
    elif args.command == "accept-report":
        archive = Archive(args.archive)
        try:
            print(json.dumps(Journal(archive).report(args.run_id), ensure_ascii=False, indent=2))
        finally:
            archive.close()
    elif args.command == "demo-acceptance":
        scenario_path = args.scenario
        if scenario_path is None:
            candidates = [
                Path(__file__).resolve().parent / "fixtures" / "acceptance.json",
                Path(__file__).resolve().parents[1] / "fixtures" / "acceptance.json",
            ]
            scenario_path = next((path for path in candidates if path.is_file()), candidates[0])
        from .origin import load_scenario
        with serve(load_scenario(scenario_path)) as origin, tempfile.TemporaryDirectory() as temporary:
            archive_path = Path(temporary) / "acceptance.sqlite3"
            policy = build_policy(args)
            result = run_acceptance(
                origin.base_url, archive_path, "local-source",
                "vendor/tool", "accepted", policy,
            )
            print(json.dumps({"operation": "demo_acceptance", **result},
                             ensure_ascii=False, indent=2))
            raise SystemExit(0 if result["state"] == "complete" else 1)
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
