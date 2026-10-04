from dataclasses import dataclass, field
from datetime import date
from typing import List, Optional


STATE_NAMES = {
    "R": "RUNNING",
    "Q": "QUEUED",
    "H": "HELD",
    "E": "EXITING",
    "F": "FINISHED",
    "S": "SUSPENDED",
    "B": "ARRAY",
    "W": "WAITING",
    "X": "DONE",
}


@dataclass
class Job:
    job_id: str
    name: str
    user: str
    runtime: str
    state: str
    queue: str
    gpus: int = 0
    cpus: int = 0
    memory: str = "--"
    requested_walltime: str = "--"
    project: str = ""

    @property
    def short_id(self) -> str:
        return self.job_id.split(".")[0]

    @property
    def state_name(self) -> str:
        return STATE_NAMES.get(self.state, self.state)


@dataclass
class JobDetails:
    job: Job
    node: str = "--"
    project: str = "--"
    script: str = "--"
    workdir: str = "--"
    stdout_path: str = ""
    stderr_path: str = ""
    # Join_Path=oe means stderr is written into the stdout file.
    stderr_joined: bool = False
    submitted: str = "--"
    started: str = "--"
    memory_used: str = "--"
    cpu_time: str = "--"
    cpu_percent: str = "--"
    exit_status: str = "--"
    comment: str = ""


@dataclass
class Quota:
    """A usage-vs-limit report, e.g. home or scratch quota."""
    name: str
    path: str
    used_bytes: int
    limit_bytes: int
    files: Optional[int] = None
    reported_at: Optional[float] = None

    @property
    def known(self) -> bool:
        """False when no quota report was found for this location."""
        return self.limit_bytes > 0

    @property
    def percent(self) -> float:
        return 100.0 * self.used_bytes / self.limit_bytes if self.limit_bytes else 0.0


# 1 GPU-hour = 100 service units (SU), as `hpc project` computes it.
SU_PER_GPU_HOUR = 100.0


@dataclass
class Project:
    name: str
    start_date: str
    end_date: str
    active: bool
    users: List[str]
    credits_su: float = 0.0
    net_su: float = 0.0
    # Held for running jobs; returned when they finish early.
    reserved_su: float = 0.0
    period: str = ""

    @property
    def gpu_hours_total(self) -> float:
        return self.credits_su / SU_PER_GPU_HOUR

    @property
    def gpu_hours_left(self) -> float:
        return self.net_su / SU_PER_GPU_HOUR

    @property
    def gpu_hours_reserved(self) -> float:
        return self.reserved_su / SU_PER_GPU_HOUR

    @property
    def ended(self) -> bool:
        # amgr can still list a project as active after its end date.
        try:
            return date.fromisoformat(self.end_date) < date.today()
        except ValueError:
            return False

    @property
    def used_fraction(self) -> float:
        return 1 - self.net_su / self.credits_su if self.credits_su else 0.0


@dataclass
class MemberUsage:
    user: str
    gpu_hours: float
    jobs: int
    # Live, from the site's qstat snapshot (all users' jobs):
    running: int = 0          # running jobs, any project
    running_here: int = 0     # of those, charged to this project
    gpus_here: int = 0        # estimated GPUs in use on this project
    reserved_gpu_hours: float = 0.0  # credit held by those running jobs
    queued: int = 0           # waiting jobs, any project (not charged yet)


@dataclass
class SnapshotJob:
    """One row of the site's periodic cluster-wide qstat snapshot."""
    job_id: str
    user: str
    queue: str
    state: str
    cpus: int
    memory: str


@dataclass
class QueueStat:
    name: str
    running: int
    waiting: int
    users_waiting: int


@dataclass
class NodeStat:
    name: str
    state: str
    gpus_free: int
    gpus_total: int
    cpus_free: int
    cpus_total: int
    mem: str
    jobs: int
    pool: str = ""             # node_pool; each queue uses one pool
    dedicated_queue: str = ""  # set when only one queue may use the node
    gpu_model: str = ""
    mem_free_bytes: int = 0

    @property
    def available(self) -> bool:
        return not any(s in self.state for s in ("offline", "down", "unknown"))


@dataclass
class QueueInfo:
    name: str
    pool: str
    min_gpus: int
    max_gpus: int
    max_walltime: str
    max_run: str
    via: str  # how you reach it: "auto" (routed) or "-q <name>" (on its user list)


@dataclass
class Placement:
    """Where a job of `gpus` GPUs could start right now."""
    gpus: int
    queue: QueueInfo
    nodes: List[NodeStat]  # nodes with room for the job now, best first
    waiting: int = 0       # jobs already waiting in that queue


@dataclass
class ClusterStatus:
    queues: List[QueueStat]
    nodes: List[NodeStat]
    queues_updated: str = ""
    queues_error: Optional[str] = None
    placements: List[Placement] = field(default_factory=list)
    # Free GPUs on nodes your queues can actually use.
    my_gpus_free: int = 0

    @property
    def gpus_free(self) -> int:
        return sum(n.gpus_free for n in self.nodes if n.available)

    @property
    def gpus_total(self) -> int:
        return sum(n.gpus_total for n in self.nodes if n.available)


@dataclass
class DiskUsage:
    path: str
    total_bytes: int
    used_bytes: int
    available_bytes: int

    @property
    def percent(self) -> float:
        if self.total_bytes <= 0:
            return 0.0
        return 100.0 * self.used_bytes / self.total_bytes


@dataclass
class StorageEntry:
    path: str
    name: str
    size_bytes: int
    is_dir: bool = True


@dataclass
class StorageScan:
    path: str
    total_bytes: int
    scanned_at: float
    entries: List[StorageEntry] = field(default_factory=list)
    # du could not read everything (e.g. permission denied).
    incomplete: bool = False


@dataclass
class FileEntry:
    name: str
    path: str
    kind: str  # "d" directory, "f" file, "l" symlink, other find %y letters
    size_bytes: int
    modified: float

    @property
    def is_dir(self) -> bool:
        return self.kind == "d"


@dataclass
class LogTail:
    path: str
    lines: List[str]
    note: Optional[str] = None


@dataclass
class CleanupCandidate:
    """Something that could be removed. HPIT only suggests; you decide."""
    category: str
    path: str
    safety: str  # "safe" (regenerated automatically) or "review" (your call)
    reason: str
    size_bytes: Optional[int] = None
    modified: Optional[float] = None
    accessed: Optional[float] = None  # approximate on NFS
    in_use_by: str = ""  # running job working directory it is under
    members: List[str] = field(default_factory=list)  # for grouped entries
    label: str = ""  # display name for grouped entries

    @property
    def display(self) -> str:
        return self.label or self.path


@dataclass
class CleanupScan:
    roots: List[str]
    scanned_at: float
    candidates: List[CleanupCandidate] = field(default_factory=list)
