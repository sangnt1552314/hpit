"""Fake data for HPIT_MOCK=1, e.g. developing on a laptop without PBS.

Functions mirror the real backend signatures; `hpit.core.api` picks
one or the other, so the UI never checks for mock mode itself.
"""

import os
import random
import time
from typing import Dict, List, Optional, Tuple

from hpit.core import config
from hpit.core.errors import HPITError
from hpit.core.models import (
    ClusterStatus, DiskUsage, FileEntry, Job, JobDetails, LogTail, MemberUsage,
    NodeStat, Placement, Project, QueueInfo, QueueStat, Quota, StorageEntry, StorageScan,
)

GB = 1024**3
_STARTED = time.time()

# (id, name, state, hours already running, requested walltime)
_JOBS = [
    ("637523", "colosseum-lingbot-vla-v2-6b-so101-cube-drawer-10ep", "R", 16.4, "96:00:00"),
    ("638106", "colosseum-pi05-so101-stack-bowls-40k", "R", 15.1, "48:00:00"),
    ("638107", "colosseum-groot-n17-so101-screwdriver-40k", "R", 2.3, "30:00:00"),
    ("638109", "colosseum-g05-so101-stack-bowls-40k", "Q", 0, "48:00:00"),
    ("638110", "colosseum-molmoact2-so101-upright-bottle", "Q", 0, "96:00:00"),
    ("638111", "eval-lingbot-sweep[3]", "H", 0, "05:00:00"),
]
_FINISHED = [
    ("636001", "colosseum-pi05-so101-cube-drawer-10ep", "F", 0, "48:00:00"),
    ("636002", "smoke-test", "F", 0, "00:10:00"),
]
_cancelled = set()
_scans: Dict[str, StorageScan] = {}


def _runtime(hours: float) -> str:
    if hours <= 0:
        return "--"
    seconds = int(hours * 3600 + time.time() - _STARTED)
    return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def _job(row: Tuple) -> Job:
    job_id, name, state, hours, walltime = row
    return Job(
        job_id=f"{job_id}.hopper-m-02", name=name, user=config.USER,
        runtime=_runtime(hours) if state == "R" else ("23:59:59" if state == "F" else "--"),
        state=state, queue="small", gpus=1, cpus=12, memory="225.0 GB",
        requested_walltime=walltime, project="CFP05-CF-002",
    )


def get_jobs(include_finished: bool = False) -> List[Job]:
    rows = (_FINISHED if include_finished else []) + _JOBS
    return [_job(r) for r in rows if r[0] not in _cancelled]


def _find(job_id: str) -> Tuple:
    short = job_id.split(".")[0]
    for row in _JOBS + _FINISHED:
        if row[0] == short and short not in _cancelled:
            return row
    raise HPITError(f"Job {job_id} not found.")


def get_job_details(job_id: str) -> JobDetails:
    row = _find(job_id)
    job = _job(row)
    running = job.state == "R"
    workdir = f"/scratch/{config.USER}/projects/robocolosseum-training"
    return JobDetails(
        job=job,
        node=f"hopper-{20 + int(row[0]) % 9}" if running else "--",
        project="CFP05-CF-002",
        script=f"pbs/{row[1].replace('colosseum-', '')}.pbs",
        workdir=workdir,
        stdout_path=f"{workdir}/{row[1]}.o{row[0]}",
        stderr_path=f"{workdir}/{row[1]}.e{row[0]}",
        stderr_joined=row[0].endswith("3"),
        submitted="Fri Oct  2 17:50:54 2026",
        started="Sat Oct  3 03:03:23 2026" if running else "--",
        memory_used="32.8 GB" if running else "--",
        cpu_time="46:11:42" if running else "--",
        cpu_percent="242%" if running else "--",
        exit_status="0" if job.state == "F" else "--",
        comment="Job run on hopper-26" if running else "Not Running: Insufficient GPUs",
    )


def cancel_job(job_id: str) -> None:
    _find(job_id)
    _cancelled.add(job_id.split(".")[0])


def get_pbs_version() -> str:
    return "2024.1.0 (mock)"


def tail_job_log(details: JobDetails, stream: str, lines: int) -> LogTail:
    if details.job.state in ("Q", "H"):
        raise HPITError("Log file not found yet: the job has not started.")

    if stream == "stderr" and details.stderr_joined:
        log = tail_job_log(details, "stdout", lines)
        log.note = "stderr is merged into stdout for this job (Join_Path=oe)."
        return log

    rng = random.Random(details.job.job_id + stream)
    out = []
    for step in range(0, lines * 50, 50):
        if stream == "stdout":
            loss = 2.5 * (0.999 ** step) + rng.random() * 0.05
            out.append(f"[train] step {step:>6}  loss {loss:.4f}  lr 1.0e-04  {rng.randint(80, 99)} it/s")
        elif step % 500 == 0:
            out.append(f"WARNING: step {step}: grad norm {rng.random() * 10:.2f} clipped")

    path = details.stdout_path if stream == "stdout" else details.stderr_path
    return LogTail(path=path, lines=out[-lines:])


def tail_job_log_by_id(job_id: str, stream: str, lines: int) -> LogTail:
    return tail_job_log(get_job_details(job_id), stream, lines)


# Fake scratch tree: path relative to scratch -> {child: size in GB}.
_TREE: Dict[str, Dict[str, float]] = {
    "": {"cache": 41.3, "outputs": 21.8, "datasets": 11.1, "misc": 2.3},
    "cache": {"huggingface": 35.1, "pip": 6.2},
    "cache/huggingface": {"hub": 33.0, "datasets": 2.1},
    "outputs": {"lingbot": 12.4, "pi05": 6.1, "groot": 3.3},
    "datasets": {"so101": 11.1},
}


def _relative(path: str) -> str:
    relative = os.path.relpath(path, config.SCRATCH)
    return "" if relative == "." else relative


def get_disk_usage(path: str) -> DiskUsage:
    return DiskUsage(path, 1024 * GB, int(726 * GB), int(298 * GB))


def scan_directory(path: str) -> StorageScan:
    time.sleep(1.5)  # Pretend du is working.
    children = _TREE.get(_relative(path), {})
    entries = [
        StorageEntry(path=os.path.join(path, name), name=name, size_bytes=int(gb * GB))
        for name, gb in sorted(children.items(), key=lambda kv: -kv[1])
    ]
    scan = StorageScan(path, sum(e.size_bytes for e in entries), time.time(), entries)
    _scans[path] = scan
    return scan


def load_cached_scan(path: str) -> Optional[StorageScan]:
    return _scans.get(path)


def list_directory(path: str) -> List[FileEntry]:
    now = time.time()
    children = _TREE.get(_relative(path))
    if children is None:
        files = [("README.md", 4_096), ("train.log", 52 * 1024**2), ("model.safetensors", 13 * GB)]
        return [FileEntry(n, os.path.join(path, n), "f", s, now - 3600 * i) for i, (n, s) in enumerate(files)]
    return [
        FileEntry(name, os.path.join(path, name), "d", 4096, now - 86400 * i)
        for i, name in enumerate(sorted(children))
    ]


def find_large_files(root: str, min_mb: int = 1024, limit: int = 100) -> List[FileEntry]:
    time.sleep(1.0)
    now = time.time()
    files = [
        ("cache/huggingface/hub/models--lingbot/model-00001.safetensors", 9.6 * GB),
        ("outputs/lingbot/checkpoint-40000/model.safetensors", 12.4 * GB),
        ("datasets/so101/episodes.tar", 4.2 * GB),
    ]
    return sorted(
        (FileEntry(rel, os.path.join(root, rel), "f", int(size), now - 7200) for rel, size in files),
        key=lambda e: -e.size_bytes,
    )


def get_doctor() -> List[Tuple[str, str]]:
    from hpit.core import system
    rows = system.get_doctor()
    return [(k, "2024.1.0 (mock)" if k == "PBS version" else v) for k, v in rows]



def get_quotas() -> List[Quota]:
    now = time.time()
    return [
        Quota("Home", os.path.expanduser("~"), int(14.7 * GB), 40 * GB, 54593, now - 1800),
        Quota("Scratch", config.SCRATCH, int(726 * GB), 1024 * GB, 281207, now - 600),
        Quota("CFP01-CF-060 scratch", "/scratch/Projects/CFP-01/CFP01-CF-060",
              int(3884 * GB), 4096 * GB, 3391276, now - 600),
        Quota("CFP05-CF-002 scratch", "/scratch/Projects/CFP-05/CFP05-CF-002",
              int(4476 * GB), 10240 * GB, 34724, now - 600),
        Quota("CFP01-CF-060 storage", "/Project_Storage/CFP-01/CFP01-CF-060",
              int(25.6 * 1024 * GB), 30 * 1024 * GB, 8551292, now - 900),
    ]


def get_cluster_status() -> ClusterStatus:
    rng = random.Random(int(time.time() // 300))
    from hpit.core.cluster import nodes_with_room

    nodes = []
    for i in range(3, 47):
        free = rng.choice([0, 0, 0, 0, 0, 1, 2, 8])
        pool = "aisg" if i >= 31 else "nus_hpc_large" if 7 <= i <= 10 else "nus_hpc_medium" if 11 <= i <= 14 else "nus_hpc"
        nodes.append(NodeStat(
            f"hopper-{i:02d}", "offline" if i == 6 else ("free" if free else "job-busy"),
            free, 8, free * 14, 112, "215 GB / 2 TB", 8 - free,
            pool=pool, dedicated_queue="special" if i in (5, 6) else ("AISG_debug" if i >= 31 else ""),
            gpu_model="H100" if i <= 6 else "H200", mem_free_bytes=free * 250 * GB,
        ))
    my_queues = [
        QueueInfo("small", "nus_hpc", 1, 2, "144:00:00", "3", "auto"),
        QueueInfo("medium", "nus_hpc_medium", 3, 7, "96:00:00", "2", "auto"),
        QueueInfo("large", "nus_hpc_large", 8, 16, "48:00:00", "1", "auto"),
        QueueInfo("special", "nus_hpc", 1, 64, "48:00:00", "4", "-q special"),
    ]
    waiting = {"small": 298, "medium": 47, "large": 35, "special": 0}
    placements = [
        Placement(g, q, nodes_with_room(nodes, q, g), waiting[q.name])
        for g in (1, 2, 4, 8) for q in my_queues if q.min_gpus <= g <= q.max_gpus
    ]
    queues = [
        QueueStat("auto", 137, 475, 45), QueueStat("small", 112, 298, 23),
        QueueStat("medium", 4, 47, 18), QueueStat("large", 3, 35, 10),
        QueueStat("interactive", 6, 42, 7), QueueStat("smallx", 7, 12, 4),
        QueueStat("mediumx", 4, 10, 4), QueueStat("largex", 1, 19, 5),
    ]
    usable = sum(n.gpus_free for n in nodes if n.available and n.pool == "nus_hpc_large" or
                 n.available and n.pool in ("nus_hpc", "nus_hpc_medium") and n.dedicated_queue in ("", "special"))
    return ClusterStatus(
        queues, nodes, queues_updated=time.strftime("%Y-%m-%d %H:%M"),
        placements=placements, my_gpus_free=usable,
    )


_logged_in = True


def is_logged_in() -> bool:
    return _logged_in


def login(password: str) -> None:
    global _logged_in
    if not password:
        raise HPITError("No password given.")
    _logged_in = True


def get_projects() -> List[Project]:
    return [
        Project("CFP01-CF-060", "2025-01-01", "2026-09-30", True,
                ["e0920848", "sutanto.patrick", config.USER], 8_000_000, 4_998_277.75, 11_400, "y2034"),
        Project("CFP05-CF-002", "2026-07-30", "2027-07-30", True,
                ["xuanweiliu", "duanj1", config.USER, "keerthivasanm"], 10_000_000, 9_482_701.6, 125_383.3, "y2034"),
    ]


def get_project_usage(name: str, start: str, end: str, members: Optional[List[str]] = None) -> List[MemberUsage]:
    return [
        MemberUsage("shailesh.xml", -159.7, 6, running=1, running_here=1, gpus_here=8, reserved_gpu_hours=384, queued=3),
        MemberUsage(config.USER, 1.0, 10, running=3, running_here=3, gpus_here=3, reserved_gpu_hours=174, queued=2),
        MemberUsage("xuanweiliu", -3.3, 2, running=2, running_here=1, gpus_here=1, reserved_gpu_hours=72, queued=1),
        MemberUsage("keerthivasanm", 9.4, 6), MemberUsage("duanj1", 0.0, 0),
    ]


_mock_cleanup = None


def scan_cleanup(progress=None):
    from hpit.core.models import CleanupCandidate, CleanupScan

    global _mock_cleanup
    for step in ("Looking through home…", "Looking through scratch…", "Measuring virtual environments… 1/2"):
        if progress:
            progress(step)
        time.sleep(0.4)
    s, now, day = config.SCRATCH, time.time(), 86400
    C = CleanupCandidate
    _mock_cleanup = CleanupScan([os.path.expanduser("~"), s], now, [
        C("Package & build caches", f"{s}/cache/xet", "safe", "Hugging Face download (xet) cache; re-downloaded when needed", int(4 * GB), now - 3 * day),
        C("Package & build caches", f"{s}/pip-cache", "safe", "pip download cache; re-downloaded when needed", int(0.4 * GB), now - 20 * day),
        C("Temporary files", f"{s}/**/__pycache__", "safe", "153 __pycache__ folders (Python bytecode; regenerated automatically)", int(0.01 * GB), now, members=[f"{s}/a/__pycache__"]),
        C("Hugging Face cache", f"{s}/cache/hub/models--lerobot--pi05_base", "review", "cached model lerobot/pi05_base; re-downloadable from Hugging Face", int(13.5 * GB), now - 22 * day, now - 22 * day),
        C("Hugging Face cache", f"{s}/cache/hub/models--nvidia--GR00T-N1.7-3B", "review", "cached model nvidia/GR00T-N1.7-3B; re-downloadable from Hugging Face", int(6.5 * GB), now - 22 * day, now - 22 * day),
        C("Virtual environments", f"{s}/virtualenvs/robocolosseum", "review", "Python environment; rebuild it from its requirements if needed", int(5.1 * GB), now - 33 * day),
        C("ML runs & logs", f"{s}/projects/train/outputs/checkpoint-20000", "review", "older checkpoint; newest one kept: checkpoint-40000", int(7.9 * GB), now - 9 * day, in_use_by=f"{s}/projects/train"),
        C("Old job logs", f"{s}/projects/train/smoke.o636002", "review", "PBS log of finished job 636002, last written 12 days ago", 4096, now - 12 * day),
    ])
    return _mock_cleanup


def load_cached_cleanup():
    return _mock_cleanup


def cleanup_commands(candidates):
    from hpit.core.cleanup import cleanup_commands as real

    return real(candidates)


def delete_cleanup(candidates, roots, progress=None):
    """Pretend to delete (mock mode never touches files)."""
    from hpit.core.cleanup import DeleteResult

    results = []
    for c in candidates:
        if progress:
            progress(f"Deleting {c.path}")
        time.sleep(0.2)
        if c.in_use_by:
            results.append(DeleteResult(c.path, False, f"skipped: a running job works in {c.in_use_by}"))
        else:
            results.append(DeleteResult(c.path, True, "deleted (mock)", c.size_bytes or 0))
    if _mock_cleanup:
        gone = {r.path for r in results if r.ok}
        _mock_cleanup.candidates = [c for c in _mock_cleanup.candidates if c.path not in gone]
    return results


def ignore(path):
    if _mock_cleanup:
        _mock_cleanup.candidates = [c for c in _mock_cleanup.candidates if c.path != path]
