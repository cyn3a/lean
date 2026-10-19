from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .apply import apply_plan
from .errors import LakeVAError
from .git_history import GitHistoryResolver
from .planner import Planner, load_plan, save_plan
from .reservoir import ReservoirClient


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lakeva",
        description=(
            "Select version-appropriate direct Lean dependencies and let Lake resolve transitives."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser("plan", help="analyze and record dependency selections")
    plan.add_argument("--old", type=Path, required=True, help="old project checkout")
    plan.add_argument("--new", type=Path, required=True, help="new project checkout")
    plan.add_argument("--target", type=Path, required=True, help="working copy to reconstruct")
    plan.add_argument("-o", "--output", type=Path, required=True, help="plan JSON to write")
    plan.add_argument(
        "--target-toolchain",
        help="exact toolchain string; defaults to NEW/lean-toolchain",
    )
    plan.add_argument(
        "--reservoir-url",
        default="https://reservoir.lean-lang.org/api/v1",
        help="Reservoir API base URL",
    )
    plan.add_argument("--git", default="git", help="Git executable")

    apply = subparsers.add_parser("apply", help="apply a reviewed plan and run Lake")
    apply.add_argument("plan", type=Path, help="plan JSON produced by `lakeva plan`")
    apply.add_argument("--target", type=Path, help="override the target working copy")
    apply.add_argument(
        "--allow-unresolved",
        action="store_true",
        help="apply resolved entries while leaving unresolved entries unchanged",
    )
    apply.add_argument(
        "--force",
        action="store_true",
        help="ignore target file hashes (toolchain equality is still required)",
    )
    apply.add_argument(
        "--skip-lake-update",
        action="store_true",
        help="patch Lake files without resolving transitive dependencies",
    )
    apply.add_argument("--lake", default="lake", help="Lake executable")
    apply.add_argument("--no-backup", action="store_true", help="do not retain a backup copy")

    adapt = subparsers.add_parser(
        "adapt", help="plan and apply in one step: adapt OLD NEW TARGET"
    )
    adapt.add_argument("old", type=Path, help="old project checkout")
    adapt.add_argument("new", type=Path, help="new project checkout")
    adapt.add_argument("target", type=Path, help="working copy to reconstruct")
    adapt.add_argument(
        "-o",
        "--output",
        type=Path,
        help="plan JSON; defaults to TARGET/.lakeva-plan.json",
    )
    adapt.add_argument("--target-toolchain", help="override NEW/lean-toolchain")
    adapt.add_argument(
        "--reservoir-url",
        default="https://reservoir.lean-lang.org/api/v1",
        help="Reservoir API base URL",
    )
    adapt.add_argument("--git", default="git", help="Git executable")
    adapt.add_argument(
        "--allow-unresolved",
        action="store_true",
        help="apply resolved entries while leaving unresolved entries unchanged",
    )
    adapt.add_argument(
        "--skip-lake-update",
        action="store_true",
        help="patch Lake files without resolving transitive dependencies",
    )
    adapt.add_argument("--lake", default="lake", help="Lake executable")
    adapt.add_argument("--no-backup", action="store_true", help="do not retain a backup copy")

    show = subparsers.add_parser("show", help="print a concise plan summary")
    show.add_argument("plan", type=Path)
    show.add_argument("--json", action="store_true", help="print the complete plan JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            planner = Planner(
                ReservoirClient(base_url=args.reservoir_url),
                GitHistoryResolver(git_command=args.git),
            )
            result = planner.build(
                args.old,
                args.new,
                args.target,
                target_toolchain=args.target_toolchain,
            )
            args.output.parent.mkdir(parents=True, exist_ok=True)
            save_plan(result, args.output)
            _print_plan(result, args.output)
            return 2 if result.unresolved else 0
        if args.command == "apply":
            plan = load_plan(args.plan)
            result = apply_plan(
                plan,
                args.target,
                allow_unresolved=args.allow_unresolved,
                force=args.force,
                run_update=not args.skip_lake_update,
                lake_command=args.lake,
                backup=not args.no_backup,
            )
            print(
                f"Applied {result.selected_count} direct dependency selections to {result.target}."
            )
            if result.lake_updated:
                print("Lake resolved the transitive dependency graph and retained the target toolchain.")
            if result.backup:
                print(f"Backup: {result.backup}")
            if result.unresolved_count:
                print(f"Warning: {result.unresolved_count} unresolved dependencies were left unchanged.")
            return 0
        if args.command == "adapt":
            planner = Planner(
                ReservoirClient(base_url=args.reservoir_url),
                GitHistoryResolver(git_command=args.git),
            )
            plan = planner.build(
                args.old,
                args.new,
                args.target,
                target_toolchain=args.target_toolchain,
            )
            output = args.output or (args.target / ".lakeva-plan.json")
            output = output.resolve()
            output.parent.mkdir(parents=True, exist_ok=True)
            save_plan(plan, output)
            _print_plan(plan, output)
            if plan.unresolved and not args.allow_unresolved:
                print(
                    "No files were changed because the plan has unresolved dependencies.",
                    file=sys.stderr,
                )
                return 2
            result = apply_plan(
                plan,
                args.target,
                allow_unresolved=args.allow_unresolved,
                run_update=not args.skip_lake_update,
                lake_command=args.lake,
                backup=not args.no_backup,
            )
            print(
                f"Adapted {result.target} to {plan.target_toolchain} with "
                f"{result.selected_count} direct dependency selections."
            )
            if result.lake_updated:
                print("Lake resolved the transitive dependency graph and retained the target toolchain.")
            if result.backup:
                print(f"Backup: {result.backup}")
            return 0
        if args.command == "show":
            plan = load_plan(args.plan)
            if args.json:
                print(json.dumps(plan.to_dict(), indent=2, ensure_ascii=False))
            else:
                _print_plan(plan, args.plan)
            return 2 if plan.unresolved else 0
    except (LakeVAError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    parser.error("unknown command")
    return 1


def _print_plan(plan, path: Path) -> None:
    print(f"Plan: {path}")
    print(f"Target toolchain: {plan.target_toolchain}")
    for decision in plan.decisions:
        revision = f" @ {decision.revision}" if decision.revision else ""
        print(
            f"  {decision.dependency.display_name}: {decision.status} "
            f"({decision.source}){revision}"
        )
        if decision.reason:
            print(f"    {decision.reason}")
    print(
        f"Resolved: {len(plan.decisions) - len(plan.unresolved)}; "
        f"unresolved: {len(plan.unresolved)}"
    )


if __name__ == "__main__":
    raise SystemExit(main())
