"""Adaptive, bounded cleanup for portal-owned Selenium and Chromium artifacts."""
from __future__ import annotations

import logging
import os
import pwd
import re
import shutil
import stat
import threading
import time
from functools import wraps
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

GiB = 1024**3
MiB = 1024**2
MODES = ("normal", "pressure", "critical")
MAX_DELETE_UNITS = 4096
MAX_DELETE_BYTES = 32 * GiB

POLICY = {
    "normal": dict(
        interval=3600,
        retain=2,
        cache_stale=30 * 86400,
        cache_min=86400,
        cache_cap=2 * GiB,
        crash_stale=7 * 86400,
        crash_min=86400,
        crash_cap=256 * MiB,
        temp_stale=3600,
        temp_min=900,
        temp_cap=2 * GiB,
    ),
    "pressure": dict(
        interval=600,
        retain=1,
        cache_stale=7 * 86400,
        cache_min=6 * 3600,
        cache_cap=GiB,
        crash_stale=86400,
        crash_min=3600,
        crash_cap=128 * MiB,
        temp_stale=900,
        temp_min=300,
        temp_cap=512 * MiB,
    ),
    "critical": dict(
        interval=60,
        retain=1,
        cache_stale=86400,
        cache_min=3600,
        cache_cap=512 * MiB,
        crash_stale=3600,
        crash_min=900,
        crash_cap=32 * MiB,
        temp_stale=300,
        temp_min=120,
        temp_cap=128 * MiB,
    ),
}

_PLATFORM_RE = re.compile(r"[A-Za-z0-9_-]+\Z")
_VERSION_RE = re.compile(r"\d+\.\d+\.\d+\.\d+\Z")
_TEMP_RES = (
    re.compile(r"org\.chromium\.Chromium\.[A-Za-z0-9]{6}\Z"),
    re.compile(r"org\.chromium\.Chromium\.scoped_dir\.[A-Za-z0-9]{6}\Z"),
)


def classify_pressure(snapshot: Sequence[int]) -> str | None:
    """Classify a (total, used, free) root-filesystem snapshot."""
    total, used, free = snapshot
    if total <= 0 or used < 0 or free < 0 or used > total or free > total:
        return None
    if used * 100 >= total * 90 or free <= 5 * GiB:
        return "critical"
    if used * 100 >= total * 80 or free <= 15 * GiB:
        return "pressure"
    return "normal"


@dataclass
class Unit:
    paths: tuple[Path, ...]
    freshness_ns: int
    allocated: int
    kind: str
    key: tuple[str, ...]
    fingerprints: tuple[tuple[int, int, int, int], ...]
    member_freshness: dict[tuple[str, str], int] = field(default_factory=dict)
    retained: bool = False
    unsafe_reason: str | None = None

    @property
    def canonical(self) -> str:
        return str(min(self.paths, key=lambda path: os.fsencode(str(path))))


@dataclass
class ClassStats:
    scanned: int = 0
    eligible: int = 0
    deleted: int = 0
    stale_deleted: int = 0
    cap_deleted: int = 0
    estimated_removed: int = 0
    active: int = 0
    retained: int = 0
    recent: int = 0
    unsafe: int = 0
    failed: int = 0
    deferred: int = 0


class ActiveScanError(RuntimeError):
    """The process scan could not establish that candidates are inactive."""


def _under(path: str, roots: Iterable[Path]) -> bool:
    if not path.startswith("/"):
        return False
    normalized = os.path.normpath(path.removesuffix(" (deleted)"))
    for root in roots:
        base = os.path.normpath(str(root))
        try:
            if os.path.commonpath((base, normalized)) == base:
                return True
        except ValueError:
            continue
    return False


def _proc_references(paths: tuple[Path, ...], proc_root: Path = Path("/proc")) -> bool:
    """Inspect same-UID processes and fail closed for browser-like indeterminate PIDs."""
    uid = os.geteuid()
    try:
        own_cgroup = (proc_root / "self/cgroup").read_bytes()
        entries = list(proc_root.iterdir())
    except OSError as exc:
        raise ActiveScanError(f"cannot enumerate processes: {exc}") from exc
    try:
        unix_text = (proc_root / "net/unix").read_text(errors="replace")
        unix_paths = {}
        for line in unix_text.splitlines()[1:]:
            fields = line.split()
            if len(fields) >= 7:
                unix_paths[fields[6]] = fields[7] if len(fields) >= 8 else ""
    except OSError as exc:
        raise ActiveScanError(f"cannot inspect unix sockets: {exc}") from exc

    held_socket_inodes: set[str] = set()
    for process in entries:
        if not process.name.isdigit():
            continue
        try:
            if process.stat().st_uid != uid:
                continue
            try:
                process_cgroup = (process / "cgroup").read_bytes()
            except FileNotFoundError:
                raise ProcessLookupError
            if process_cgroup != own_cgroup:
                continue
            indeterminate = False
            browser_like = False
            try:
                command = (process / "cmdline").read_bytes().split(b"\0")
                decoded = [os.fsdecode(value) for value in command if value]
                browser_like = any(re.search(r"chrome|chromedriver|crashpad|selenium", value, re.I) for value in decoded)
                if any(_under(value, paths) for value in decoded):
                    return True
            except FileNotFoundError:
                raise ProcessLookupError
            except PermissionError:
                indeterminate = True
            for name in ("exe", "cwd", "root"):
                try:
                    target = os.readlink(process / name)
                    browser_like = browser_like or bool(re.search(r"chrome|chromedriver|crashpad|selenium", target, re.I))
                    if _under(target, paths):
                        return True
                except FileNotFoundError:
                    raise ProcessLookupError
                except PermissionError:
                    indeterminate = True
            try:
                maps = (process / "maps").read_text(errors="replace")
                for line in maps.splitlines():
                    parts = line.split(maxsplit=5)
                    if len(parts) == 6 and _under(parts[5], paths):
                        return True
            except FileNotFoundError:
                raise ProcessLookupError
            except PermissionError:
                indeterminate = True
            try:
                descriptors = list((process / "fd").iterdir())
                for descriptor in descriptors:
                    try:
                        target = os.readlink(descriptor)
                    except FileNotFoundError:
                        continue
                    if _under(target, paths):
                        return True
                    match = re.fullmatch(r"socket:\[(\d+)\]", target)
                    if match:
                        held_socket_inodes.add(match.group(1))
            except FileNotFoundError:
                raise ProcessLookupError
            except PermissionError:
                indeterminate = True
            if indeterminate:
                raise ActiveScanError(f"cannot fully inspect same-UID pid {process.name}")
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError as exc:
            if process.exists():
                raise ActiveScanError(f"cannot inspect same-UID pid {process.name}: {exc}") from exc
        except OSError as exc:
            if process.exists():
                raise ActiveScanError(f"cannot inspect same-UID pid {process.name}: {exc}") from exc

    # A live named UNIX socket is sufficient activity evidence even when its
    # owner is outside the portal cgroup; never unlink its containing unit.
    return any(_under(path, paths) for path in unix_paths.values())

def _mount_points(mountinfo: Path = Path("/proc/self/mountinfo")) -> set[str]:
    points: set[str] = set()
    try:
        for line in mountinfo.read_text(errors="replace").splitlines():
            fields = line.split()
            if len(fields) >= 5:
                value = fields[4].replace("\\040", " ").replace("\\011", "\t").replace("\\012", "\n").replace("\\134", "\\")
                points.add(os.path.normpath(value))
    except OSError as exc:
        raise OSError(f"cannot inspect mount table: {exc}") from exc
    return points


def _real_owned_directories(paths: Iterable[Path], owners: set[int]) -> bool:
    """Validate every fixed discovery ancestor without following symlinks."""
    try:
        for path in paths:
            info = os.lstat(path)
            if info.st_uid not in owners or not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                return False
        return True
    except OSError:
        return False


def _metadata(root: Path, uid: int, mounts: set[str]) -> tuple[int, int, tuple[int, int, int, int]]:
    """Return newest mtime, allocated bytes, and root identity without following links."""
    root_stat = os.lstat(root)
    if root_stat.st_uid != uid or stat.S_ISLNK(root_stat.st_mode) or not (
        stat.S_ISDIR(root_stat.st_mode) or stat.S_ISREG(root_stat.st_mode)
    ):
        raise OSError("unsafe candidate root")
    device = root_stat.st_dev
    newest = 0
    allocated = 0
    stack = [root]
    while stack:
        current = stack.pop()
        current_stat = os.lstat(current)
        if current_stat.st_uid != uid or current_stat.st_dev != device:
            raise OSError("ownership or filesystem boundary")
        normalized = os.path.normpath(str(current))
        if normalized in mounts:
            raise OSError("mount point candidate")
        if stat.S_ISREG(current_stat.st_mode) and current_stat.st_nlink > 1:
            raise OSError("hard-linked file")
        newest = max(newest, current_stat.st_mtime_ns)
        allocated += current_stat.st_blocks * 512
        if stat.S_ISDIR(current_stat.st_mode) and not stat.S_ISLNK(current_stat.st_mode):
            with os.scandir(current) as entries:
                stack.extend(Path(entry.path) for entry in entries)
    fingerprint = (root_stat.st_dev, root_stat.st_ino, stat.S_IFMT(root_stat.st_mode), root_stat.st_uid)
    return newest, allocated, fingerprint


def _unlink_tree(path: Path) -> None:
    """Remove a candidate without following a path swapped to a symlink."""
    current = os.lstat(path)
    if stat.S_ISDIR(current.st_mode) and not stat.S_ISLNK(current.st_mode):
        if not getattr(shutil.rmtree, "avoids_symlink_attacks", False):
            raise OSError("platform lacks symlink-safe recursive removal")
        shutil.rmtree(path)
    else:
        path.unlink()


class CleanupEngine:
    def __init__(
        self,
        home: Path | str | None = None,
        tmp_root: Path | str = "/tmp",
        disk_usage: Callable[[str], object] = shutil.disk_usage,
        now: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        uid: int | None = None,
        active_check: Callable[[tuple[Path, ...]], bool] | None = None,
        mount_points: Callable[[], set[str]] = _mount_points,
        logger: logging.Logger | None = None,
        max_units: int = MAX_DELETE_UNITS,
        max_bytes: int = MAX_DELETE_BYTES,
    ):
        self.uid = os.geteuid() if uid is None else uid
        self.home = Path(home) if home is not None else Path(pwd.getpwuid(self.uid).pw_dir)
        self.tmp_root = Path(tmp_root)
        self.disk_usage = disk_usage
        self.now = now
        self.monotonic = monotonic
        self.active_check = active_check or _proc_references
        self.mount_points = mount_points
        self.log = logger or logging.getLogger(__name__)
        self.max_units = max_units
        self.max_bytes = max_bytes
        self.last_attempt: float | None = None
        self.last_mode: str | None = None
        self.snapshot_retry_after: float = 0
        self.backlog = False
        self.startup = True
        self.status_active = False
        self._lock = threading.Lock()

    def _make_unit(self, paths: tuple[Path, ...], kind: str, key: tuple[str, ...], mounts: set[str]) -> Unit:
        metadata = [_metadata(path, self.uid, mounts) for path in paths]
        member_freshness = {}
        if kind == "cache":
            member_freshness = {
                (path.parts[-3], path.parts[-2]): item[0]
                for path, item in zip(paths, metadata)
            }
        return Unit(
            paths=paths,
            freshness_ns=max(item[0] for item in metadata),
            allocated=sum(item[1] for item in metadata),
            kind=kind,
            key=key,
            fingerprints=tuple(item[2] for item in metadata),
            member_freshness=member_freshness,
        )

    def _discover(self, mode: str, mounts: set[str]) -> dict[str, list[Unit]]:
        found: dict[str, list[Unit]] = {"cache": [], "crash": [], "temp": []}
        cache_members: dict[tuple[str, str], list[Path]] = {}
        cache_root = self.home / ".cache" / "selenium"
        for kind in ("chrome", "chromedriver"):
            kind_root = cache_root / kind
            if not _real_owned_directories(
                (self.home, self.home / ".cache", cache_root, kind_root), {self.uid}
            ):
                continue
            try:
                platforms = list(kind_root.iterdir())
            except OSError:
                continue
            for platform in platforms:
                if not _PLATFORM_RE.fullmatch(platform.name) or not _real_owned_directories(
                    (self.home, self.home / ".cache", cache_root, kind_root, platform), {self.uid}
                ):
                    continue
                try:
                    versions = list(platform.iterdir())
                except OSError:
                    continue
                for version in versions:
                    if _VERSION_RE.fullmatch(version.name) and version.is_dir() and not version.is_symlink():
                        cache_members.setdefault((platform.name, version.name), []).append(version)
        for (platform, version), paths in cache_members.items():
            try:
                found["cache"].append(self._make_unit(tuple(sorted(paths)), "cache", (platform, version), mounts))
            except OSError:
                continue

        # Retain the freshest versions independently for each browser kind/platform;
        # a version group is retained if either member belongs to a retained set.
        retain = POLICY[mode]["retain"]
        for kind in ("chrome", "chromedriver"):
            for platform in {unit.key[0] for unit in found["cache"]}:
                members = [
                    unit
                    for unit in found["cache"]
                    if unit.key[0] == platform and any(path.parts[-3] == kind for path in unit.paths)
                ]
                members.sort(
                    key=lambda unit: (
                        -unit.member_freshness[(kind, platform)],
                        os.fsencode(unit.canonical),
                    )
                )
                for unit in members[:retain]:
                    unit.retained = True

        crash_root = self.home / ".config" / "google-chrome-for-testing" / "Crash Reports"
        for bucket_name in ("attachments", "completed", "new", "pending"):
            bucket = crash_root / bucket_name
            if not _real_owned_directories(
                (
                    self.home,
                    self.home / ".config",
                    self.home / ".config" / "google-chrome-for-testing",
                    crash_root,
                    bucket,
                ),
                {self.uid},
            ):
                continue
            try:
                entries = list(bucket.iterdir())
            except OSError:
                continue
            for entry in entries:
                if entry.is_symlink() or not (entry.is_file() or entry.is_dir()):
                    continue
                try:
                    found["crash"].append(self._make_unit((entry,), "crash", (bucket_name, entry.name), mounts))
                except OSError:
                    continue

        if _real_owned_directories((self.tmp_root,), {0, self.uid}):
            try:
                entries = list(self.tmp_root.iterdir())
            except OSError:
                entries = []
            for entry in entries:
                if (
                    entry.is_dir()
                    and not entry.is_symlink()
                    and any(pattern.fullmatch(entry.name) for pattern in _TEMP_RES)
                ):
                    try:
                        found["temp"].append(self._make_unit((entry,), "temp", (entry.name,), mounts))
                    except OSError:
                        continue
        return found

    def _revalidate(self, unit: Unit, mounts: set[str]) -> bool:
        try:
            current = [_metadata(path, self.uid, mounts) for path in unit.paths]
        except OSError:
            return False
        fingerprints = tuple(item[2] for item in current)
        return fingerprints == unit.fingerprints

    def _delete(self, unit: Unit, mounts: set[str]) -> bool:
        if not self._revalidate(unit, mounts):
            return False
        for path in unit.paths:
            _unlink_tree(path)
        return True

    def run(self, force: bool = False) -> dict[str, object]:
        if self.uid == 0 or self.status_active:
            return {"skipped": "unsafe root"}
        if not self._lock.acquire(blocking=False):
            self.log.info("artifact_cleanup_skip reason=concurrent")
            return {"skipped": "concurrent"}
        try:
            monotonic_now = self.monotonic()
            if monotonic_now < self.snapshot_retry_after and not force:
                return {"skipped": "snapshot backoff"}
            try:
                raw_snapshot = self.disk_usage("/")
            except Exception as exc:
                self.snapshot_retry_after = monotonic_now + 60
                self.log.warning("artifact_cleanup_skip reason=unavailable_disk_snapshot error=%s", exc)
                return {"skipped": "unavailable disk snapshot"}
            snapshot = (
                (raw_snapshot.total, raw_snapshot.used, raw_snapshot.free)
                if hasattr(raw_snapshot, "total")
                else tuple(raw_snapshot)
            )
            mode = classify_pressure(snapshot)
            if mode is None:
                self.snapshot_retry_after = monotonic_now + 60
                self.log.warning("artifact_cleanup_skip reason=unavailable_disk_snapshot")
                return {"skipped": "unavailable disk snapshot"}
            escalation = self.last_mode is not None and MODES.index(mode) > MODES.index(self.last_mode)
            due = (
                force
                or self.startup
                or self.backlog
                or escalation
                or self.last_attempt is None
                or monotonic_now - self.last_attempt >= POLICY[mode]["interval"]
            )
            if not due:
                self.last_mode = mode
                return {"skipped": "not due"}
            reason = "startup" if self.startup else "backlog" if self.backlog else "escalation" if escalation else "interval"
            self.last_attempt = monotonic_now
            self.last_mode = mode
            self.startup = False
            self.backlog = False
            started = self.now()
            total, used, free = snapshot
            self.log.info(
                "artifact_cleanup_start mode=%s total=%d used=%d free=%d used_percent=%.4f reason=%s policy_timestamp=%.3f",
                mode, total, used, free, used * 100 / total, reason, started,
            )
            mounts = self.mount_points()
            units = self._discover(mode, mounts)
            stats = {name: ClassStats(scanned=len(values)) for name, values in units.items()}
            policy = POLICY[mode]

            # Complete two same-UID activity scans before the first mutation. An
            # indeterminate scan therefore fails the entire run closed.
            active: set[tuple[Path, ...]] = set()
            try:
                for _ in range(2):
                    for values in units.values():
                        for unit in values:
                            if self.active_check(unit.paths):
                                active.add(unit.paths)
            except ActiveScanError as exc:
                self.backlog = True
                self.log.warning("artifact_cleanup_skip reason=indeterminate_active_scan error=%s", exc)
                return {"skipped": "indeterminate active scan", "backlog": True}

            attempts = 0
            planned_bytes = 0
            recorded_protection: set[tuple[Path, ...]] = set()

            def protected(class_name: str, unit: Unit, minimum_age: int) -> bool:
                if unit.retained:
                    if unit.paths not in recorded_protection:
                        stats[class_name].retained += 1
                        recorded_protection.add(unit.paths)
                    return True
                if unit.paths in active:
                    if unit.paths not in recorded_protection:
                        stats[class_name].active += 1
                        recorded_protection.add(unit.paths)
                    return True
                age = max(0.0, started - unit.freshness_ns / 1_000_000_000)
                if age < minimum_age:
                    if unit.paths not in recorded_protection:
                        stats[class_name].recent += 1
                        recorded_protection.add(unit.paths)
                    return True
                return False

            def delete_unit(class_name: str, unit: Unit, selection_reason: str) -> bool:
                nonlocal attempts, planned_bytes
                would_exceed = planned_bytes + unit.allocated > self.max_bytes
                if attempts >= self.max_units or would_exceed:
                    if attempts == 0 and unit.allocated > self.max_bytes:
                        pass
                    else:
                        stats[class_name].deferred += 1
                        self.backlog = True
                        return False
                attempts += 1
                planned_bytes += unit.allocated
                try:
                    # Recheck at the deletion boundary. The lifecycle lock keeps
                    # new portal WebDrivers out; this catches late process/socket
                    # references from already-running in-scope tasks.
                    if self.active_check(unit.paths):
                        stats[class_name].active += 1
                        return False
                    if not self._delete(unit, mounts):
                        stats[class_name].unsafe += 1
                        return False
                    stats[class_name].deleted += 1
                    stats[class_name].estimated_removed += unit.allocated
                    if selection_reason == "stale":
                        stats[class_name].stale_deleted += 1
                    else:
                        stats[class_name].cap_deleted += 1
                    return True
                except ActiveScanError as exc:
                    self.backlog = True
                    stats[class_name].active += 1
                    self.log.warning(
                        "artifact_cleanup_skip reason=indeterminate_active_scan path=%s error=%s",
                        unit.canonical, exc,
                    )
                    return False
                except OSError as exc:
                    stats[class_name].failed += 1
                    self.log.warning(
                        "artifact_cleanup_failure class=%s path=%s operation=delete error=%s",
                        class_name, unit.canonical, exc,
                    )
                    return False

            for class_name in ("temp", "crash", "cache"):
                class_units = sorted(
                    units[class_name],
                    key=lambda unit: (unit.freshness_ns, os.fsencode(unit.canonical)),
                )
                remaining = sum(unit.allocated for unit in class_units)
                stale_age = policy[f"{class_name}_stale"]
                minimum_age = policy[f"{class_name}_min"]
                cap = policy[f"{class_name}_cap"]
                attempted_paths: set[tuple[Path, ...]] = set()

                for unit in class_units:
                    age = max(0.0, started - unit.freshness_ns / 1_000_000_000)
                    if age < stale_age or protected(class_name, unit, stale_age):
                        continue
                    stats[class_name].eligible += 1
                    attempted_paths.add(unit.paths)
                    if delete_unit(class_name, unit, "stale"):
                        remaining -= unit.allocated

                if remaining > cap:
                    for unit in class_units:
                        if remaining <= cap:
                            break
                        if unit.paths in attempted_paths or protected(class_name, unit, minimum_age):
                            continue
                        stats[class_name].eligible += 1
                        attempted_paths.add(unit.paths)
                        if delete_unit(class_name, unit, "cap"):
                            remaining -= unit.allocated

            try:
                after_raw = self.disk_usage("/")
                after = (after_raw.total, after_raw.used, after_raw.free) if hasattr(after_raw, "total") else tuple(after_raw)
            except Exception:
                after = snapshot
            removed = sum(value.estimated_removed for value in stats.values())
            failures = sum(value.failed for value in stats.values())
            self.log.info(
                "artifact_cleanup_complete mode=%s deleted=%d estimated_bytes_removed=%d failures=%d backlog=%s before_free=%d after_free=%d duration=%.3f stats=%s",
                mode,
                sum(value.deleted for value in stats.values()),
                removed, failures, self.backlog, free, after[2], self.now() - started,
                {name: vars(value) for name, value in stats.items()},
            )
            for class_name, class_units in units.items():
                residual = sum(unit.allocated for unit in class_units) - stats[class_name].estimated_removed
                if residual > policy[f"{class_name}_cap"]:
                    self.log.warning(
                        "artifact_cleanup_residual class=%s bytes=%d cap=%d active=%d retained=%d recent=%d unsafe=%d failed=%d deferred=%d",
                        class_name, residual, policy[f"{class_name}_cap"],
                        stats[class_name].active, stats[class_name].retained, stats[class_name].recent,
                        stats[class_name].unsafe, stats[class_name].failed, stats[class_name].deferred,
                    )
            return {
                "mode": mode,
                "deleted": sum(value.deleted for value in stats.values()),
                "estimated_bytes_removed": removed,
                "backlog": self.backlog,
                "stats": {name: vars(value) for name, value in stats.items()},
            }
        except Exception as exc:
            self.log.warning("artifact_cleanup_failure class=all operation=run error=%s", exc)
            return {"skipped": "failure"}
        finally:
            self._lock.release()


def _wait_for_cgroup_quiet(
    timeout: float = 10.0,
    proc_root: Path = Path("/proc"),
    cgroup_root: Path = Path("/sys/fs/cgroup"),
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Wait until the portal cgroup contains only this process."""
    try:
        own_line = (proc_root / "self/cgroup").read_text().strip()
        relative = own_line.split("::", 1)[1].lstrip("/")
        procs_file = cgroup_root / relative / "cgroup.procs"
    except (OSError, IndexError):
        return False
    deadline = monotonic() + timeout
    while True:
        try:
            pids = {int(value) for value in procs_file.read_text().split()}
        except (OSError, ValueError):
            return False
        if pids <= {os.getpid()}:
            return True
        if monotonic() >= deadline:
            return False
        sleep(0.2)


_default = CleanupEngine()
_lifecycle_lock = threading.RLock()


def status_protected(function):
    """Serialize every portal WebDriver lifetime against cleanup deletion."""
    @wraps(function)
    def guarded(*args, **kwargs):
        with _lifecycle_lock:
            return function(*args, **kwargs)
    return guarded


def cleanup() -> dict[str, object]:
    if not _lifecycle_lock.acquire(blocking=False):
        logging.getLogger(__name__).info("artifact_cleanup_skip reason=status_active")
        return {"skipped": "status active"}
    try:
        if not _wait_for_cgroup_quiet():
            _default.backlog = True
            logging.getLogger(__name__).info("artifact_cleanup_skip reason=cgroup_busy")
            return {"skipped": "cgroup busy", "backlog": True}
        return _default.run()
    finally:
        _lifecycle_lock.release()
