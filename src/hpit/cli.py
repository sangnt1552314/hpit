import argparse
import sys

from hpit import __version__
from hpit.core import api, config
from hpit.core.errors import HPITError
from hpit.core.units import human_bytes


def cmd_jobs(args: argparse.Namespace) -> None:
    jobs = api.get_jobs(include_finished=args.all)

    if not jobs:
        print("No active jobs.")
        return

    print(
        f"{'JOB ID':<25} "
        f"{'NAME':<50} "
        f"{'QUEUE':<10} "
        f"{'STATE':<6} "
        f"{'RUNTIME':<10}"
    )

    for job in jobs:
        print(
            f"{job.job_id:<25} "
            f"{job.name:<50} "
            f"{job.queue:<10} "
            f"{job.state:<6} "
            f"{job.runtime:<10}"
        )


def cmd_job(args: argparse.Namespace) -> None:
    details = api.get_job_details(args.job_id)
    job = details.job
    stderr = "merged into stdout" if details.stderr_joined else details.stderr_path

    rows = [
        ("Name", job.name),
        ("Status", job.state_name),
        ("Job ID", job.job_id),
        ("Queue", job.queue),
        ("Node", details.node),
        ("GPU / CPU", f"{job.gpus} / {job.cpus}"),
        ("Memory", f"{details.memory_used} used of {job.memory}"),
        ("Walltime", f"{job.runtime} of {job.requested_walltime}"),
        ("Project", details.project),
        ("Script", details.script),
        ("Working directory", details.workdir),
        ("stdout", details.stdout_path or "--"),
        ("stderr", stderr or "--"),
    ]
    for label, value in rows:
        print(f"{label:<20}{value}")


def cmd_logs(args: argparse.Namespace) -> None:
    stream = "stderr" if args.stderr else "stdout"
    log = api.tail_job_log_by_id(args.job_id, stream, args.lines)

    print(f"==> {log.path} <==", file=sys.stderr)
    if log.note:
        print(log.note, file=sys.stderr)
    for line in log.lines:
        print(line)


def cmd_storage(args: argparse.Namespace) -> None:
    path = args.path or config.SCRATCH
    quotas = api.get_quotas()
    if not quotas:
        usage = api.get_disk_usage(config.SCRATCH)
        print(
            f"Scratch {config.SCRATCH}: {usage.percent:.0f}% used "
            f"({human_bytes(usage.used_bytes)} of {human_bytes(usage.total_bytes)})"
        )
    for quota in quotas:
        files = f", {quota.files:,} files" if quota.files is not None else ""
        print(
            f"{quota.name:<8} {quota.path:<24} {quota.percent:5.1f}% used "
            f"({human_bytes(quota.used_bytes)} of {human_bytes(quota.limit_bytes)}{files})"
        )

    if args.scan:
        print(f"Scanning {path} with du (this can take minutes)...", file=sys.stderr)
        scan = api.scan_directory(path)
    else:
        scan = api.load_cached_scan(path)
        if scan is None:
            print(f"\nNo folder sizes for {path} yet. Run: hpit storage --scan")
            return

    print(f"\n{scan.path}  ({human_bytes(scan.total_bytes)} total)")
    for entry in scan.entries:
        print(f"  {human_bytes(entry.size_bytes):>10}  {entry.name}")


def cmd_projects(args: argparse.Namespace) -> None:
    projects = api.get_projects()
    if not projects:
        print("You are not a member of any project.")
        return

    print(f"{'PROJECT':<16} {'GPU-H LEFT':>12} {'OF TOTAL':>10} {'RESERVED':>9}  {'ENDS':<10}  STATUS")
    for p in projects:
        status = "ended" if p.ended else ("active" if p.active else "inactive")
        print(
            f"{p.name:<16} {p.gpu_hours_left:>12,.1f} {p.gpu_hours_total:>10,.0f} "
            f"{p.gpu_hours_reserved:>9,.1f}  {p.end_date:<10}  {status}"
        )
    print("\nUse a project in a job script with:  #PBS -P <project>")


def cmd_usage(args: argparse.Namespace) -> None:
    from datetime import date

    from hpit.core.accounting import month_bounds, month_range

    start, end = month_range()
    if args.start and len(args.start) == 7 and not args.end:
        # "YYYY-MM" means that whole month, like `hpc project-usage`.
        first, last = month_bounds(date.fromisoformat(args.start + "-01"))
        start, end = first.isoformat(), last.isoformat()
    else:
        start, end = args.start or start, args.end or end
    project = next((p for p in api.get_projects() if p.name == args.project), None)
    if project is None:
        raise HPITError(f"You are not a member of project {args.project}.")

    print(f"{project.name}  {start} → {end}  ({project.gpu_hours_left:,.1f} of {project.gpu_hours_total:,.0f} GPU-h left)")
    print(f"{'USER':<16} {'RUN HERE':>8} {'GPUS':>5} {'RESERVED':>9} {'RUN ALL':>8} {'QUEUED':>7}   {'GPU-H USED':>10} {'JOBS':>5}")
    for m in api.get_project_usage(project.name, start, end, project.users):
        print(
            f"{m.user:<16} {m.running_here:>8} {m.gpus_here:>5} {m.reserved_gpu_hours:>9,.0f} "
            f"{m.running:>8} {m.queued:>7}   {m.gpu_hours:>10,.1f} {m.jobs:>5}"
        )
    print(
        "\nRUN HERE / GPUS / RESERVED: jobs running on this project now (GPUs estimated, 12 CPUs per GPU).\n"
        "RUN ALL / QUEUED: jobs in any project, from the site's qstat snapshot.\n"
        "GPU-H USED: this period; negative = credit returned by jobs that finished early."
    )


def cmd_cluster(args: argparse.Namespace) -> None:
    from hpit.core.cluster import submit_hint

    status = api.get_cluster_status()
    print(
        f"Free GPUs you can use: {status.my_gpus_free}  "
        f"(whole cluster: {status.gpus_free} / {status.gpus_total}, offline nodes excluded)"
    )

    if status.placements:
        print("\nWhere can my job start now?")
        print(f"  {'GPUS':>4}  {'QUEUE':<9} {'WAITING':>7}  {'SUBMIT WITH':<40} NODES WITH ROOM")
        for p in status.placements:
            nodes = ", ".join(f"{n.name} ({n.gpus_free} {n.gpu_model})" for n in p.nodes[:3]) or "none (would wait)"
            print(f"  {p.gpus:>4}  {p.queue.name:<9} {p.waiting:>7}  {submit_hint(p):<40} {nodes}")

    if status.queues_error:
        print(f"\nQueues: {status.queues_error}")
    else:
        print(f"\nQueues (snapshot {status.queues_updated})")
        print(f"  {'QUEUE':<12} {'RUNNING':>8} {'WAITING':>8} {'USERS WAITING':>14}")
        for q in status.queues:
            print(f"  {q.name:<12} {q.running:>8} {q.waiting:>8} {q.users_waiting:>14}")

    free_nodes = [n for n in status.nodes if n.available and n.gpus_free]
    if free_nodes:
        print("\nNodes with free GPUs")
        for n in free_nodes:
            only = f"  only for queue {n.dedicated_queue}" if n.dedicated_queue else ""
            print(
                f"  {n.name:<10} {n.gpus_free}/{n.gpus_total} {n.gpu_model:<5} pool {n.pool:<15}"
                f" {n.cpus_free}/{n.cpus_total} CPUs  mem {n.mem}{only}"
            )


def cmd_clean(args: argparse.Namespace) -> None:
    result = None if args.scan else api.load_cached_cleanup()
    if result is None:
        print("Scanning home and scratch for things to clean (a few minutes)...", file=sys.stderr)
        result = api.scan_cleanup(lambda message: print(f"  {message}", file=sys.stderr))

    items = result.candidates
    selection = args.commands or args.delete
    if not selection:
        print(f"{'#':>3}  {'SAFETY':<7} {'SIZE':>9}  {'CATEGORY':<22} PATH")
        for i, c in enumerate(items, 1):
            safety = "IN USE" if c.in_use_by else c.safety
            print(f"{i:>3}  {safety:<7} {human_bytes(c.size_bytes):>9}  {c.category:<22} {c.display}")
            print(f"{'':>15}{c.reason}")
        print(
            "\nNothing is deleted unless you ask:\n"
            "  hpit clean --commands 1 3 5   print rm commands to copy (or: safe)\n"
            "  hpit clean --delete 1 3 5     delete for good, after typing 'delete' (or: safe)"
        )
        return

    if selection == ["safe"]:
        chosen = [c for c in items if c.safety == "safe" and not c.in_use_by]
    else:
        try:
            chosen = [items[int(n) - 1] for n in selection]
        except (ValueError, IndexError):
            raise HPITError("Give item numbers from `hpit clean`, or `safe`.")

    if args.commands:
        print(api.cleanup_commands(chosen), end="")
        return

    total = sum(c.size_bytes or 0 for c in chosen)
    print(f"About to DELETE FOREVER {len(chosen)} item(s), about {human_bytes(total)}:")
    for c in chosen:
        warn = "   (in use by a running job: will be skipped)" if c.in_use_by else ""
        print(f"  {human_bytes(c.size_bytes):>9}  {c.display}{warn}")
    if not sys.stdin.isatty():
        raise HPITError("Refusing to delete without an interactive confirmation.")
    if input("Type delete to confirm: ").strip() != "delete":
        print("Cancelled; nothing was deleted.")
        return

    results = api.delete_cleanup(chosen, result.roots, lambda m: print(f"  {m}", file=sys.stderr))
    deleted = [r for r in results if r.ok]
    print(f"Deleted {len(deleted)} path(s), about {human_bytes(sum(r.size_bytes for r in deleted))} freed.")
    for r in results:
        if not r.ok:
            print(f"  {r.message}: {r.path}")
    print("History: ~/.cache/hpit/cleanup-history.log")


def cmd_doctor(args: argparse.Namespace) -> None:
    for name, value in api.get_doctor():
        print(f"{name:<24}{value}")


def cmd_login(args: argparse.Namespace) -> None:
    import getpass

    from hpit.core import accounting

    if accounting.is_logged_in() and not args.force:
        print("Already logged in to amgr (token is valid).")
        return

    password = getpass.getpass("NUS password for amgr: ")
    accounting.login(password)
    print("Logged in to amgr.")


def cmd_tui(args: argparse.Namespace) -> None:
    from hpit.tui.app import HPITApp

    HPITApp().run()


def cmd_web(args: argparse.Namespace) -> None:
    print("Web UI coming soon")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hpit",
        description="HPIT - HPC Interactive Terminal",
    )
    parser.add_argument("--version", action="version", version=f"hpit {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="<command>")

    jobs = commands.add_parser("jobs", help="list your jobs")
    jobs.add_argument("-a", "--all", action="store_true", help="include finished jobs")
    jobs.set_defaults(func=cmd_jobs)

    job = commands.add_parser("job", help="show details of one job")
    job.add_argument("job_id")
    job.set_defaults(func=cmd_job)

    logs = commands.add_parser("logs", help="tail a job's stdout or stderr")
    logs.add_argument("job_id")
    logs.add_argument("-e", "--stderr", action="store_true", help="show stderr instead of stdout")
    logs.add_argument("-n", "--lines", type=int, default=config.LOG_LINES)
    logs.set_defaults(func=cmd_logs)

    storage = commands.add_parser("storage", help="scratch usage and folder sizes")
    storage.add_argument("path", nargs="?", help="folder to show (default: scratch)")
    storage.add_argument("--scan", action="store_true", help="measure folder sizes now with du (slow)")
    storage.set_defaults(func=cmd_storage)

    projects = commands.add_parser("projects", help="your projects: GPU-hours left, end dates (needs amgr login)")
    projects.set_defaults(func=cmd_projects)

    usage = commands.add_parser("usage", help="GPU-hours used per project member (needs amgr login)")
    usage.add_argument("project")
    usage.add_argument("start", nargs="?", help="YYYY-MM-DD, or YYYY-MM for a whole month (default: this month)")
    usage.add_argument("end", nargs="?", help="YYYY-MM-DD (default: today)")
    usage.set_defaults(func=cmd_usage)

    cluster = commands.add_parser("cluster", help="free GPUs and queue status")
    cluster.set_defaults(func=cmd_cluster)

    clean = commands.add_parser("clean", help="suggest files/folders to clean up")
    clean.add_argument("--scan", action="store_true", help="scan again instead of using the last scan")
    group = clean.add_mutually_exclusive_group()
    group.add_argument("--commands", nargs="+", metavar="N", help="print rm commands for these items (or 'safe') to copy")
    group.add_argument("--delete", nargs="+", metavar="N", help="delete these items (or 'safe') forever, after confirmation")
    clean.set_defaults(func=cmd_clean)

    doctor = commands.add_parser("doctor", help="check environment and configuration")
    doctor.set_defaults(func=cmd_doctor)

    login = commands.add_parser("login", help="log in to amgr for project credits (asks for password)")
    login.add_argument("--force", action="store_true", help="log in even if the token is still valid")
    login.set_defaults(func=cmd_login)

    tui = commands.add_parser("tui", help="open the terminal UI")
    tui.set_defaults(func=cmd_tui)

    web = commands.add_parser("web", help="open the web UI (not implemented yet)")
    web.set_defaults(func=cmd_web)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        return

    try:
        args.func(args)
    except HPITError as exc:
        if config.DEBUG:
            raise
        print(f"hpit: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
