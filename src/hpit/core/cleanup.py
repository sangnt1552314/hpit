"""Find files and folders that could be cleaned up, and explain why.

Nothing is deleted automatically. HPIT lists candidates; you pick some;
then either copy the `rm` commands to run yourself, or ask HPIT to delete
them for good (after a typed confirmation, with fresh safety checks).

One `find` pass per root collects candidates, then `du` sizes them.
Rules are deliberately conservative (inspired by Mole, github.com/tw93/mole):
  - "safe"   = regenerated automatically (caches, bytecode, crash dumps)
  - "review" = only you can judge (models, venvs, checkpoints, old files)
Never suggested: anything inside .git, anything owned by another user.
Anything under a running job's working directory is marked as in use.
"""

import json
from dataclasses import dataclass
import os
import re
import shlex
import time
from collections import defaultdict
from datetime import datetime
from typing import Callable, Dict, Iterable, List, Optional, Set

from hpit.core import config
from hpit.core.command import run_command
from hpit.core.errors import HPITError
from hpit.core.models import CleanupCandidate, CleanupScan

SCAN_TIMEOUT = 900
DU_BATCH = 500

# Thresholds
HF_MIN_BYTES = 1024**2  # smaller HF entries are metadata only
SIZE_CACHE_MAX_AGE = 86400  # reuse Storage-page du sizes up to a day old
PBS_LOG_MIN_AGE_DAYS = 7
LARGE_FILE_GB = 5
LARGE_FILE_MIN_AGE_DAYS = 60

IGNORE_FILE = os.path.expanduser("~/.config/hpit/cleanup-ignore")
_CACHE_FILE = os.path.join(config.CACHE_DIR, "cleanup.json")

# Folders whose whole content is a re-downloadable / regenerable cache.
# (relative to home or scratch; checked if they exist)
KNOWN_CACHES = {
    ".cache/pip": "pip download cache; re-downloaded when needed",
    "pip-cache": "pip download cache; re-downloaded when needed",
    ".cache/uv": "uv download cache; re-downloaded when needed",
    ".conda/pkgs": "conda package cache (same as `conda clean --all`)",
    ".cache/torch_extensions": "compiled PyTorch extensions; rebuilt on next use",
    ".triton/cache": "Triton kernel cache; rebuilt on next use",
    ".nv/ComputeCache": "CUDA JIT cache; rebuilt on next use",
    ".cache/flashinfer": "FlashInfer kernel cache; rebuilt on next use",
    ".cache/huggingface/xet": "Hugging Face download (xet) cache; re-downloaded when needed",
    "cache/xet": "Hugging Face download (xet) cache; re-downloaded when needed",
}

# Folder names found anywhere (pruned, so their insides aren't scanned).
SAFE_DIRS = {
    "__pycache__": "Python bytecode; regenerated automatically",
    ".ipynb_checkpoints": "Jupyter autosave copies",
    ".pytest_cache": "pytest cache; regenerated automatically",
}
REVIEW_DIRS = {
    "node_modules": "npm packages; reinstall with `npm install`",
    "wandb": "local Weights & Biases run files; make sure runs are synced first",
    "lightning_logs": "PyTorch Lightning logs and checkpoints",
}

# Remote-editor servers in home: their insides are never scanned; only
# old versions (and the extension download cache) are suggested.
EDITOR_SERVERS = [".vscode-server", ".cursor-server", ".windsurf-server", ".vscode-server-insiders"]

PBS_LOG_RE = re.compile(r"\.[oe](\d+)$")
CORE_RE = re.compile(r"^core\.\d+$")
CHECKPOINT_RE = re.compile(r"^checkpoint[-_](\d+)$")


class CleanupError(HPITError):
    pass


def default_roots() -> List[str]:
    return [os.path.expanduser("~"), config.SCRATCH]


def scan(roots: Optional[List[str]] = None, active_job_ids: Iterable[str] = (),
         busy_dirs: Iterable[str] = (),
         progress: Optional[Callable[[str], None]] = None) -> CleanupScan:
    """Look for cleanup candidates under `roots` (slow: walks the trees).

    active_job_ids: numeric IDs of your queued/running jobs (their logs are kept).
    busy_dirs: working directories of running jobs (candidates there are "in use").
    progress: called with short status messages while scanning.
    """
    report = progress or (lambda message: None)
    roots = [os.path.normpath(r) for r in (roots or default_roots()) if os.path.isdir(r)]
    if not roots:
        raise CleanupError("None of the folders to scan exist.")

    active = {j.split(".")[0] for j in active_job_ids}
    busy = [os.path.normpath(d) for d in busy_dirs if d and d != "--"]
    ignored = load_ignored()
    my_uid = os.getuid()
    now = time.time()

    candidates: List[CleanupCandidate] = []
    venvs: Set[str] = set()
    models: List[CleanupCandidate] = []
    checkpoints: Dict[str, List[CleanupCandidate]] = defaultdict(list)
    pycache: Dict[str, List[str]] = defaultdict(list)

    running = _running_commands()
    home = os.path.expanduser("~")
    if home in roots:
        candidates.extend(c for c in _editor_leftovers(home, running) if not _is_ignored(c.path, ignored))

    for root in roots:
        for rel, reason in KNOWN_CACHES.items():
            path = os.path.join(root, rel)
            if os.path.isdir(path) and not os.path.islink(path):
                candidates.append(_candidate("Package & build caches", path, "safe", reason))

        report(f"Looking through {root}…")
        for kind, size, mtime, atime, uid, path in _find(root):
            if uid != my_uid or _is_ignored(path, ignored):
                continue
            name = os.path.basename(path)

            if kind == "f" and name == "pyvenv.cfg":
                venvs.add(os.path.dirname(path))
            elif kind == "d" and name == "conda-meta":
                venvs.add(os.path.dirname(path))
            elif kind == "d" and name in SAFE_DIRS:
                if name == "__pycache__":
                    pycache[root].append(path)
                else:
                    candidates.append(_candidate("Temporary files", path, "safe", SAFE_DIRS[name], mtime, atime))
            elif kind == "d" and name == "wandb" and not _has_wandb_runs(path):
                continue  # W&B settings / empty folder, not run files.
            elif kind == "d" and name in REVIEW_DIRS:
                candidates.append(_candidate("ML runs & logs", path, "review", REVIEW_DIRS[name], mtime, atime))
            elif kind == "d" and (name.startswith("models--") or name.startswith("datasets--")) \
                    and os.path.basename(os.path.dirname(path)) != ".locks":
                label = name.split("--", 1)[1].replace("--", "/")
                what = "model" if name.startswith("models--") else "dataset"
                models.append(_candidate(
                    "Hugging Face cache", path, "review",
                    f"cached {what} {label}; re-downloadable from Hugging Face", mtime, atime,
                ))
            elif kind == "d" and CHECKPOINT_RE.match(name):
                checkpoints[os.path.dirname(path)].append(
                    _candidate("ML runs & logs", path, "review", "", mtime, atime)
                )
            elif kind == "f" and CORE_RE.match(name):
                candidates.append(_candidate("Temporary files", path, "safe", "crash dump (core file)", mtime, atime, size))
            elif kind == "f" and PBS_LOG_RE.search(name):
                job = PBS_LOG_RE.search(name).group(1)
                age = (now - mtime) / 86400
                if job not in active and age >= PBS_LOG_MIN_AGE_DAYS:
                    candidates.append(_candidate(
                        "Old job logs", path, "review",
                        f"PBS log of finished job {job}, last written {int(age)} days ago",
                        mtime, atime, size,
                    ))
            elif kind == "f" and size >= LARGE_FILE_GB * 1024**3:
                age = (now - mtime) / 86400
                candidates.append(_candidate(
                    "Large old files", path, "review",
                    f"over {LARGE_FILE_GB} GB, not modified for {int(age)} days", mtime, atime, size,
                ))

    for venv in sorted(venvs):
        if not _is_ignored(venv, ignored):
            st = _stat(venv)
            candidates.append(_candidate(
                "Virtual environments", venv, "review",
                "Python environment; rebuild it from its requirements if needed",
                _env_modified(venv), st.st_atime if st else None,
            ))

    candidates.extend(models)

    # Keep the newest checkpoint in each folder; suggest the older ones.
    for parent, items in checkpoints.items():
        items.sort(key=lambda c: int(CHECKPOINT_RE.match(os.path.basename(c.path)).group(1)))
        newest = os.path.basename(items[-1].path)
        for c in items[:-1]:
            c.reason = f"older checkpoint; newest one kept: {newest}"
            candidates.append(c)

    # Drop candidates inside other candidates (e.g. __pycache__ inside a venv).
    candidates = _outermost(candidates)
    covered = [c.path for c in candidates]

    # __pycache__: one aggregated entry per root, outside other candidates.
    for root, paths in pycache.items():
        paths = [p for p in paths if not _inside_any(p, covered)]
        if paths:
            # The group gets its own (non-filesystem) path, so ignoring it
            # never hides the whole root.
            entry = _candidate(
                "Temporary files", f"{root}/**/__pycache__", "safe",
                f"{len(paths):,} __pycache__ folders (Python bytecode; regenerated automatically)",
                mtime=max((_stat(p).st_mtime for p in paths if _stat(p)), default=None),
            )
            entry.members = paths
            candidates.append(entry)

    _measure(candidates, report)
    candidates = [
        c for c in candidates
        if c.category != "Hugging Face cache" or (c.size_bytes or 0) >= HF_MIN_BYTES
    ]

    for c in candidates:
        c.in_use_by = next((
            d for d in busy
            if any(_inside_any(p, [d]) or _inside_any(d, [p]) for p in (c.members or [c.path]))
        ), "")
        # A large-file rule only makes sense for files that are still big.
        if c.category == "Large old files" and c.size_bytes is not None and c.size_bytes < LARGE_FILE_GB * 1024**3:
            c.size_bytes = None

    candidates = [c for c in candidates if c.category != "Large old files" or c.size_bytes]
    candidates.sort(key=lambda c: (CATEGORY_ORDER.index(c.category), -(c.size_bytes or 0)))

    result = CleanupScan(roots=roots, scanned_at=now, candidates=candidates)
    if roots == [os.path.normpath(r) for r in default_roots()]:
        _save(result)  # Only the standard home+scratch scan is remembered.
    return result


CATEGORY_ORDER = [
    "Package & build caches",
    "Temporary files",
    "Editor leftovers",
    "Hugging Face cache",
    "Virtual environments",
    "ML runs & logs",
    "Old job logs",
    "Large old files",
]


def cleanup_commands(candidates: List[CleanupCandidate]) -> str:
    """Shell commands that remove exactly these candidates, for copy-paste.

    Each item gets a comment line (size, reason, warnings) so the pasted
    block is self-explanatory.
    """
    if not candidates:
        raise CleanupError("Nothing selected.")

    from hpit.core.units import human_bytes

    total = sum(c.size_bytes or 0 for c in candidates)
    lines = [f"# HPIT cleanup: {len(candidates)} item(s), about {human_bytes(total)}. Review before running."]
    for c in candidates:
        size = human_bytes(c.size_bytes) if c.size_bytes is not None else "size unknown"
        lines.append(f"# {size} - {c.reason}")
        if c.in_use_by:
            lines.append(f"# WARNING: inside a running job's working directory ({c.in_use_by})")
        # Grouped entries: exactly the folders HPIT listed, not a fresh search.
        lines.extend(f"rm -rf -- {shlex.quote(p)}" for p in (c.members or [c.path]))
    return "\n".join(lines) + "\n"


@dataclass
class DeleteResult:
    path: str
    ok: bool
    message: str = ""
    size_bytes: int = 0


HISTORY_FILE = os.path.join(config.CACHE_DIR, "cleanup-history.log")


def delete_candidates(
    candidates: List[CleanupCandidate],
    roots: List[str],
    busy_dirs: Iterable[str] = (),
    progress: Optional[Callable[[str], None]] = None,
) -> List[DeleteResult]:
    """Permanently delete these candidates (no undo).

    Every path is checked again right before deleting: it must still exist,
    be owned by you, lie inside one of the scanned `roots`, not be a root or
    location folder itself, and not touch a running job's working directory.
    Anything that fails a check is skipped, never deleted. Each attempt is
    appended to ~/.cache/hpit/cleanup-history.log.
    """
    report = progress or (lambda message: None)
    roots = [os.path.normpath(r) for r in roots]
    busy = [os.path.normpath(d) for d in busy_dirs if d and d != "--"]
    protected = _protected_paths(roots)
    results: List[DeleteResult] = []

    targets = [(c, p) for c in candidates for p in (c.members or [c.path])]
    for i, (c, path) in enumerate(targets, 1):
        report(f"Deleting {i}/{len(targets)}: {path}")
        size = c.size_bytes if not c.members else 0
        problem = _delete_problem(path, roots, protected, busy)
        if problem:
            results.append(DeleteResult(path, False, problem))
            continue

        result = run_command(["rm", "-rf", "--", path], timeout=3600)
        if os.path.lexists(path):
            message = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "could not delete everything"
            results.append(DeleteResult(path, False, message))
        else:
            results.append(DeleteResult(path, True, "deleted", size or 0))

    if any(c.members for c in candidates):
        # Credit grouped entries (e.g. __pycache__) once, if all members went.
        for c in candidates:
            if c.members and all(r.ok for r in results if r.path in c.members):
                ok = [r for r in results if r.path in c.members]
                if ok:
                    ok[0].size_bytes = c.size_bytes or 0

    _log_deletions(results)
    _forget_deleted(results)
    return results


def _protected_paths(roots: List[str]) -> Set[str]:
    """Folders that must never be deleted as a whole."""
    from hpit.core.quota import get_quotas

    protected = {"/", os.path.expanduser("~"), os.path.normpath(config.SCRATCH), *roots}
    try:
        protected |= {os.path.normpath(q.path) for q in get_quotas()}
    except HPITError:
        pass
    # Their parents too (e.g. /scratch, /home/svu).
    protected |= {os.path.dirname(p) for p in list(protected)}
    return protected


def _delete_problem(path: str, roots: List[str], protected: Set[str], busy: List[str]) -> str:
    path_n = os.path.normpath(path)
    if not os.path.isabs(path_n) or path_n != path.rstrip("/"):
        return "skipped: not a clean absolute path"
    if path_n in protected:
        return "skipped: protected folder"
    if not any(_inside_any(path_n, [r]) and path_n != r for r in roots):
        return "skipped: outside the scanned folders"
    try:
        st = os.lstat(path_n)
    except FileNotFoundError:
        return "skipped: already gone"
    except OSError as exc:
        return f"skipped: {exc.strerror}"
    if st.st_uid != os.getuid():
        return "skipped: owned by another user"
    hit = next((d for d in busy if _inside_any(path_n, [d]) or _inside_any(d, [path_n])), None)
    if hit:
        return f"skipped: a running job works in {hit}"
    return ""


def _log_deletions(results: List[DeleteResult]) -> None:
    try:
        os.makedirs(config.CACHE_DIR, exist_ok=True)
        with open(HISTORY_FILE, "a") as f:
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for r in results:
                f.write(f"{stamp}\t{'deleted' if r.ok else 'skipped'}\t{r.size_bytes}\t{r.path}\t{r.message}\n")
    except OSError:
        pass


def _forget_deleted(results: List[DeleteResult]) -> None:
    """Drop deleted items from the saved scan."""
    cached = load_cached()
    if cached is None:
        return
    gone = {r.path for r in results if r.ok}
    cached.candidates = [
        c for c in cached.candidates
        if c.path not in gone and not (c.members and all(m in gone for m in c.members))
    ]
    _save(cached)


def load_cached() -> Optional[CleanupScan]:
    try:
        with open(_CACHE_FILE) as f:
            data = json.load(f)
        scan_result = CleanupScan(
            roots=data["roots"], scanned_at=data["scanned_at"],
            candidates=[CleanupCandidate(**c) for c in data["candidates"]],
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None
    ignored = load_ignored()
    scan_result.candidates = [c for c in scan_result.candidates if not _is_ignored(c.path, ignored)]
    return scan_result


def load_ignored() -> Set[str]:
    try:
        with open(IGNORE_FILE) as f:
            return {line.strip() for line in f if line.strip() and not line.startswith("#")}
    except OSError:
        return set()


def ignore(path: str) -> None:
    os.makedirs(os.path.dirname(IGNORE_FILE), exist_ok=True)
    with open(IGNORE_FILE, "a") as f:
        f.write(path + "\n")


# Helpers

def _find(root: str):
    """Yield (kind, size, mtime, atime, uid, path) for interesting entries."""
    fmt = r"%y\0%s\0%T@\0%A@\0%U\0%p\0"
    pruned = sorted(SAFE_DIRS) + sorted(REVIEW_DIRS) + ["models--*", "datasets--*", "checkpoint-*", "checkpoint_*"]
    expr = [
        "find", root, "-xdev",
        # Skip repos, and installed packages inside environments (huge and
        # never cleaned piecemeal; the environment itself is found via
        # pyvenv.cfg / conda-meta).
        "(", "-name", ".git", "-o", "-name", "site-packages", "-o", "-name", "dist-packages",
        *[x for server in EDITOR_SERVERS for x in ("-o", "-name", server)],
        ")", "-prune", "-o",
        "(", "-type", "d", "(",
    ]
    for i, name in enumerate(pruned):
        expr += (["-o"] if i else []) + ["-name", name]
    expr += [")", "-printf", fmt, "-prune", ")", "-o",
             "(", "-type", "d", "-name", "conda-meta", "-printf", fmt, "-prune", ")", "-o",
             "(", "-type", "f", "(",
             "-name", "pyvenv.cfg", "-o", "-name", "core.[0-9]*",
             "-o", "-regex", r".*\.[oe][0-9]+",
             "-o", "(", "-size", f"+{LARGE_FILE_GB * 1024}M", "-mtime", f"+{LARGE_FILE_MIN_AGE_DAYS}", ")",
             ")", "-printf", fmt, ")"]

    result = run_command(expr, timeout=SCAN_TIMEOUT)
    if result.returncode == 124:
        raise CleanupError(f"Scanning {root} took longer than {SCAN_TIMEOUT // 60} minutes.")

    fields = result.stdout.split("\0")
    for i in range(0, len(fields) - 5, 6):
        kind, size, mtime, atime, uid, path = fields[i:i + 6]
        try:
            yield kind, int(size), float(mtime), float(atime), int(uid), path
        except ValueError:
            continue


def _candidate(category, path, safety, reason, mtime=None, atime=None, size=None) -> CleanupCandidate:
    if mtime is None:
        st = _stat(path)
        mtime, atime = (st.st_mtime, st.st_atime) if st else (None, None)
    return CleanupCandidate(
        category=category, path=path, safety=safety, reason=reason,
        size_bytes=size, modified=mtime, accessed=atime,
    )


def _measure(candidates: List[CleanupCandidate], report: Callable[[str], None]) -> None:
    """Fill in sizes with `du -sk`: small things in batches first, then each
    virtual environment on its own (they are slow: many small files)."""
    sizes = _recent_storage_sizes()
    quick: List[str] = []
    slow: List[str] = []
    for c in candidates:
        if c.size_bytes is None:
            for p in c.members or [c.path]:
                if p not in sizes:
                    (slow if c.category == "Virtual environments" else quick).append(p)

    def du(paths: List[str]) -> None:
        result = run_command(["du", "-sk", "--", *paths], timeout=SCAN_TIMEOUT)
        for line in result.stdout.splitlines():
            size, _, path = line.partition("\t")
            if size.isdigit():
                sizes[path] = int(size) * 1024

    for i in range(0, len(quick), DU_BATCH):
        report(f"Measuring sizes… ({min(i + DU_BATCH, len(quick))}/{len(quick)})")
        du(quick[i:i + DU_BATCH])
    for i, path in enumerate(slow, 1):
        report(f"Measuring virtual environments… {i}/{len(slow)}: {os.path.basename(path)}")
        du([path])

    for c in candidates:
        if c.size_bytes is None:
            parts = [sizes.get(p) for p in (c.members or [c.path])]
            known = [s for s in parts if s is not None]
            c.size_bytes = sum(known) if known else None


def _editor_leftovers(home: str, running: str) -> List[CleanupCandidate]:
    """Old remote-editor server versions; keep the newest and any in use."""
    found = []
    for server in EDITOR_SERVERS:
        base = os.path.join(home, server)
        versions = []
        for sub in ("bin", "cli/servers"):
            folder = os.path.join(base, sub)
            if os.path.isdir(folder):
                versions += [os.path.join(folder, v) for v in os.listdir(folder)]
        versions = [v for v in versions if os.path.isdir(v) and not os.path.islink(v)]
        versions.sort(key=lambda v: _stat(v).st_mtime if _stat(v) else 0)
        name = server.strip(".").replace("-", " ")
        for version in versions[:-1]:  # The newest one stays.
            if version in running:
                continue  # A running editor server uses it.
            found.append(_candidate(
                "Editor leftovers", version, "safe",
                f"old {name} version (not running); the editor re-downloads it if ever needed",
            ))
        vsix = os.path.join(base, "data", "CachedExtensionVSIXs")
        if os.path.isdir(vsix):
            found.append(_candidate("Editor leftovers", vsix, "safe", "downloaded extension installers (cache)"))
    return found


def _running_commands() -> str:
    """Command lines of your running processes (to protect what's in use)."""
    result = run_command(["ps", "-u", config.USER, "-o", "args="])
    return result.stdout if result.returncode == 0 else ""


def _has_wandb_runs(path: str) -> bool:
    try:
        return any(n.startswith(("run-", "offline-run-")) for n in os.listdir(path))
    except OSError:
        return False


def _recent_storage_sizes() -> Dict[str, int]:
    """Folder sizes the Storage page measured recently (saves re-running du)."""
    from hpit.core.storage import _read_cache

    sizes: Dict[str, int] = {}
    for scan_data in _read_cache().values():
        if time.time() - scan_data.get("scanned_at", 0) > SIZE_CACHE_MAX_AGE:
            continue
        for entry in scan_data.get("entries", []):
            if entry.get("is_dir"):
                sizes[entry["path"]] = entry["size_bytes"]
    return sizes


def _outermost(candidates: List[CleanupCandidate]) -> List[CleanupCandidate]:
    ordered = sorted(candidates, key=lambda c: len(c.path))
    kept: List[CleanupCandidate] = []
    kept_paths: List[str] = []
    for c in ordered:
        if not _inside_any(c.path, kept_paths):
            kept.append(c)
            kept_paths.append(c.path)
    return kept


def _inside_any(path: str, parents: List[str]) -> bool:
    return any(path == p or path.startswith(p.rstrip("/") + "/") for p in parents)


def _is_ignored(path: str, ignored: Set[str]) -> bool:
    return bool(ignored) and _inside_any(path, list(ignored))


def _stat(path: str):
    try:
        return os.stat(path)
    except OSError:
        return None


def _env_modified(env: str) -> Optional[float]:
    # pyvenv.cfg / conda-meta change when packages are (re)installed.
    for marker in ("pyvenv.cfg", "conda-meta"):
        st = _stat(os.path.join(env, marker))
        if st:
            return st.st_mtime
    st = _stat(env)
    return st.st_mtime if st else None


def _save(result: CleanupScan) -> None:
    data = {
        "roots": result.roots,
        "scanned_at": result.scanned_at,
        "candidates": [c.__dict__ for c in result.candidates],
    }
    try:
        os.makedirs(config.CACHE_DIR, exist_ok=True)
        with open(_CACHE_FILE, "w") as f:
            json.dump(data, f)
    except OSError:
        pass


def scan_for_user(progress: Optional[Callable[[str], None]] = None) -> CleanupScan:
    """scan() with your active jobs from PBS, so their logs and working
    directories are protected."""
    from hpit.core import pbs

    report = progress or (lambda message: None)
    report("Checking your jobs…")
    try:
        jobs = pbs.get_jobs()
    except HPITError:
        jobs = []
    busy = []
    for job in jobs:
        if job.state == "R":
            try:
                busy.append(pbs.get_job_details(job.job_id).workdir)
            except HPITError:
                pass
    return scan(active_job_ids=[j.job_id for j in jobs], busy_dirs=busy, progress=progress)


def delete_for_user(candidates: List[CleanupCandidate], roots: List[str],
                    progress: Optional[Callable[[str], None]] = None) -> List[DeleteResult]:
    """delete_candidates() with running jobs' working directories from PBS
    checked right now (not from the possibly old scan)."""
    from hpit.core import pbs

    busy = []
    try:
        for job in pbs.get_jobs():
            if job.state == "R":
                busy.append(pbs.get_job_details(job.job_id).workdir)
    except HPITError as exc:
        # Without PBS we can't tell what is in use: refuse to delete.
        raise CleanupError(f"Could not check running jobs ({exc}); nothing was deleted.")
    return delete_candidates(candidates, roots, busy, progress)
