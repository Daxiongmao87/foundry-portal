import logging
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import artifact_cleanup as ac


class MemoryLog(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.tmp = self.root / "tmp"
        self.home.mkdir()
        self.tmp.mkdir()
        self.now = 2_000_000_000.0
        self.clock = 1000.0
        self.snapshot = [100 * ac.GiB, 70 * ac.GiB, 30 * ac.GiB]
        self.logger = logging.getLogger(f"cleanup-test-{id(self)}")
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        self.handler = MemoryLog()
        self.logger.addHandler(self.handler)

    def tearDown(self):
        self.logger.removeHandler(self.handler)
        self.temp.cleanup()

    def engine(self, **overrides):
        values = dict(
            home=self.home,
            tmp_root=self.tmp,
            disk_usage=lambda _: tuple(self.snapshot),
            now=lambda: self.now,
            monotonic=lambda: self.clock,
            uid=os.geteuid(),
            active_check=lambda paths: False,
            mount_points=lambda: set(),
            logger=self.logger,
        )
        values.update(overrides)
        return ac.CleanupEngine(**values)

    def age(self, path, seconds):
        timestamp = self.now - seconds
        for current in [path] + list(path.rglob("*")):
            os.utime(current, (timestamp, timestamp), follow_symlinks=False)

    def cache(self, kind, version, age=0):
        path = self.home / ".cache" / "selenium" / kind / "linux64" / version
        path.mkdir(parents=True)
        (path / "payload").write_bytes(b"cache")
        self.age(path, age)
        return path

    def crash(self, bucket, name, age=0):
        path = self.home / ".config" / "google-chrome-for-testing" / "Crash Reports" / bucket / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"crash")
        self.age(path, age)
        return path

    def chromium_tmp(self, name="org.chromium.Chromium.A1b2C3", age=0):
        path = self.tmp / name
        path.mkdir()
        (path / "payload").write_bytes(b"temp")
        self.age(path, age)
        return path

    def test_pressure_boundaries_and_invalid_snapshots(self):
        g = ac.GiB
        cases = [
            ((100 * g, 79 * g, 21 * g), "normal"),
            ((100 * g, 80 * g, 20 * g), "pressure"),
            ((100 * g, 70 * g, 15 * g), "pressure"),
            ((100 * g, 90 * g, 10 * g), "critical"),
            ((100 * g, 70 * g, 5 * g), "critical"),
        ]
        for snapshot, expected in cases:
            self.assertEqual(ac.classify_pressure(snapshot), expected)
        for snapshot in ((0, 0, 0), (10, 11, 0), (10, -1, 1), (10, 1, 11)):
            self.assertIsNone(ac.classify_pressure(snapshot))

    def test_exact_tmp_allowlist_and_normal_stale_age(self):
        exact = self.chromium_tmp(age=3600)
        scoped = self.chromium_tmp("org.chromium.Chromium.scoped_dir.Z9y8X7", age=3600)
        near = self.chromium_tmp("org.chromium.Chromium.A1b2", age=99_999)
        hidden = self.chromium_tmp(".org.chromium.Chromium.A1b2C3", age=99_999)
        unrelated = self.chromium_tmp("systemd-private-example", age=99_999)
        result = self.engine().run()
        self.assertEqual(result["deleted"], 2)
        self.assertFalse(exact.exists())
        self.assertFalse(scoped.exists())
        self.assertTrue(near.exists())
        self.assertTrue(hidden.exists())
        self.assertTrue(unrelated.exists())

    def test_cache_retention_unions_browser_and_driver_versions(self):
        old = self.cache("chrome", "1.0.0.0", age=40 * 86400)
        chrome_current = self.cache("chrome", "2.0.0.0", age=100)
        shared_driver = self.cache("chromedriver", "2.0.0.0", age=200)
        driver_current = self.cache("chromedriver", "3.0.0.0", age=50)
        self.snapshot[:] = [100 * ac.GiB, 85 * ac.GiB, 15 * ac.GiB]
        result = self.engine().run()
        self.assertEqual(result["mode"], "pressure")
        self.assertFalse(old.exists())
        self.assertTrue(chrome_current.exists())
        self.assertTrue(shared_driver.exists())
        self.assertTrue(driver_current.exists())

    def test_cap_evicts_oldest_minimum_age_without_deleting_recent(self):
        old = self.chromium_tmp(age=200)
        recent = self.chromium_tmp("org.chromium.Chromium.B2c3D4", age=60)
        with patch.dict(ac.POLICY["critical"], {"temp_cap": 0}):
            self.snapshot[:] = [100 * ac.GiB, 95 * ac.GiB, 5 * ac.GiB]
            result = self.engine().run()
        self.assertEqual(result["stats"]["temp"]["cap_deleted"], 1)
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())

    def test_crash_cleanup_preserves_structure_settings_and_unknown_children(self):
        reports = self.home / ".config" / "google-chrome-for-testing" / "Crash Reports"
        old = self.crash("pending", "old.dmp", age=8 * 86400)
        fresh = self.crash("completed", "fresh.dmp", age=60)
        (reports / "settings.dat").write_text("settings")
        outside = reports / "unexpected.bin"
        outside.write_text("keep")
        result = self.engine().run()
        self.assertEqual(result["stats"]["crash"]["stale_deleted"], 1)
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())
        self.assertTrue((reports / "pending").is_dir())
        self.assertTrue((reports / "settings.dat").exists())
        self.assertTrue(outside.exists())

    def test_active_and_future_dated_artifacts_are_preserved(self):
        active = self.chromium_tmp(age=10_000)
        future = self.chromium_tmp("org.chromium.Chromium.B2c3D4")
        self.age(future, -3600)
        engine = self.engine(active_check=lambda paths: active in paths)
        result = engine.run()
        self.assertTrue(active.exists())
        self.assertTrue(future.exists())
        self.assertEqual(result["stats"]["temp"]["active"], 1)

    def test_symlink_near_match_and_hardlinked_candidate_are_safe(self):
        sentinel = self.root / "sentinel"
        sentinel.mkdir()
        (sentinel / "value").write_text("preserve")
        direct_link = self.tmp / "org.chromium.Chromium.A1b2C3"
        direct_link.symlink_to(sentinel, target_is_directory=True)
        hardlinked = self.chromium_tmp("org.chromium.Chromium.B2c3D4", age=99_999)
        os.link(hardlinked / "payload", self.root / "other-link")
        result = self.engine().run()
        self.assertEqual(result["deleted"], 0)
        self.assertTrue(direct_link.is_symlink())
        self.assertEqual((sentinel / "value").read_text(), "preserve")
        self.assertTrue(hardlinked.exists())

    def test_final_inode_revalidation_blocks_swapped_candidate(self):
        candidate = self.chromium_tmp(age=99_999)
        engine = self.engine()
        original = engine._revalidate

        def swap(unit, mounts):
            if candidate in unit.paths:
                backup = candidate.with_name(candidate.name + ".old")
                candidate.rename(backup)
                candidate.mkdir()
                return original(unit, mounts)
            return original(unit, mounts)

        engine._revalidate = swap
        result = engine.run()
        self.assertEqual(result["deleted"], 0)
        self.assertTrue(candidate.exists())
        self.assertEqual(result["stats"]["temp"]["unsafe"], 1)

    def test_cadence_escalation_and_invalid_snapshot_backoff(self):
        engine = self.engine()
        self.assertIn("mode", engine.run())
        self.assertEqual(engine.run(), {"skipped": "not due"})
        self.snapshot[:] = [100 * ac.GiB, 85 * ac.GiB, 15 * ac.GiB]
        self.assertEqual(engine.run()["mode"], "pressure")
        self.snapshot[:] = [0, 0, 0]
        self.clock += 700
        self.assertEqual(engine.run(), {"skipped": "unavailable disk snapshot"})
        self.assertEqual(engine.run(), {"skipped": "snapshot backoff"})

    def test_deletion_budget_sets_backlog_and_next_cycle_bypasses_cadence(self):
        first = self.chromium_tmp(age=99_999)
        second = self.chromium_tmp("org.chromium.Chromium.B2c3D4", age=99_999)
        engine = self.engine(max_units=1)
        result = engine.run()
        self.assertTrue(result["backlog"])
        self.assertEqual(result["deleted"], 1)
        result = engine.run()
        self.assertFalse(result["backlog"])
        self.assertEqual(result["deleted"], 1)
        self.assertFalse(first.exists() or second.exists())

    def test_failure_isolated_and_later_candidate_continues(self):
        first = self.chromium_tmp(age=99_999)
        second = self.chromium_tmp("org.chromium.Chromium.B2c3D4", age=99_999)
        real_unlink = ac._unlink_tree

        def fail_one(path):
            if path == first:
                raise PermissionError("injected")
            real_unlink(path)

        with patch.object(ac, "_unlink_tree", side_effect=fail_one):
            result = self.engine().run()
        self.assertEqual(result["deleted"], 1)
        self.assertEqual(result["stats"]["temp"]["failed"], 1)
        self.assertTrue(first.exists())
        self.assertFalse(second.exists())

    def test_concurrent_and_root_runs_refuse_deletion(self):
        candidate = self.chromium_tmp(age=99_999)
        root_engine = self.engine(uid=0)
        self.assertEqual(root_engine.run(), {"skipped": "unsafe root"})
        engine = self.engine()
        engine._lock.acquire()
        try:
            self.assertEqual(engine.run(), {"skipped": "concurrent"})
        finally:
            engine._lock.release()
        self.assertTrue(candidate.exists())

    def test_proc_scan_detects_accessible_live_cwd(self):
        candidate = self.chromium_tmp(age=99_999)
        proc = self.root / "proc-live"
        (proc / "self").mkdir(parents=True)
        (proc / "self" / "cgroup").write_text("0::/test")
        (proc / "net").mkdir()
        (proc / "net" / "unix").write_text("Num RefCount Protocol Flags Type St Inode Path\n")
        pid = proc / "123"
        (pid / "fd").mkdir(parents=True)
        (pid / "cgroup").write_text("0::/test")
        (pid / "cmdline").write_bytes(b"/bin/sleep\0")
        (pid / "maps").write_text("")
        (pid / "exe").symlink_to("/bin/sleep")
        (pid / "cwd").symlink_to(candidate)
        (pid / "root").symlink_to("/")
        self.assertTrue(ac._proc_references((candidate,), proc_root=proc))

    def test_structured_start_complete_and_failure_logs(self):
        candidate = self.chromium_tmp(age=99_999)
        result = self.engine().run()
        self.assertEqual(result["deleted"], 1)
        joined = "\n".join(self.handler.messages)
        self.assertIn("artifact_cleanup_start", joined)
        self.assertIn("artifact_cleanup_complete", joined)
        self.assertIn("estimated_bytes_removed", joined)
        self.assertFalse(candidate.exists())

    def test_symlinked_fixed_cache_ancestor_cannot_redirect_deletion(self):
        sentinel = self.root / "foundry-sentinel"
        candidate = sentinel / "selenium" / "chrome" / "linux64" / "1.2.3.4"
        candidate.mkdir(parents=True)
        (candidate / "world-data").write_text("preserve")
        self.age(candidate, 99_999_999)
        (self.home / ".cache").symlink_to(sentinel, target_is_directory=True)
        result = self.engine().run()
        self.assertEqual(result["deleted"], 0)
        self.assertEqual((candidate / "world-data").read_text(), "preserve")

    def test_candidate_root_mount_and_mount_table_failure_fail_closed(self):
        candidate = self.chromium_tmp(age=99_999)
        mounted = self.engine(mount_points=lambda: {os.path.normpath(str(candidate))})
        self.assertEqual(mounted.run()["deleted"], 0)
        self.assertTrue(candidate.exists())
        failed = self.engine(mount_points=lambda: (_ for _ in ()).throw(OSError("mountinfo")))
        self.assertEqual(failed.run(), {"skipped": "failure"})
        self.assertTrue(candidate.exists())

    def test_disk_usage_exception_enters_snapshot_backoff(self):
        calls = [0]
        def broken(_):
            calls[0] += 1
            raise OSError("df failed")
        engine = self.engine(disk_usage=broken)
        self.assertEqual(engine.run(), {"skipped": "unavailable disk snapshot"})
        self.assertEqual(engine.run(), {"skipped": "snapshot backoff"})
        self.assertEqual(calls[0], 1)

    def test_independent_cache_retention_uses_each_member_freshness(self):
        old_chrome = self.cache("chrome", "1.0.0.0", age=30 * 86400)
        fresh_driver = self.cache("chromedriver", "1.0.0.0", age=200)
        current_chrome = self.cache("chrome", "2.0.0.0", age=100)
        current_driver = self.cache("chromedriver", "3.0.0.0", age=50)
        engine = self.engine()
        units = engine._discover("pressure", set())["cache"]
        retained = {unit.key for unit in units if unit.retained}
        self.assertEqual(retained, {("linux64", "2.0.0.0"), ("linux64", "3.0.0.0")})
        self.assertTrue(old_chrome.exists() and fresh_driver.exists() and current_chrome.exists() and current_driver.exists())

    def test_indeterminate_active_scan_aborts_before_any_deletion(self):
        first = self.chromium_tmp(age=99_999)
        second = self.chromium_tmp("org.chromium.Chromium.B2c3D4", age=99_999)
        calls = [0]
        def indeterminate(paths):
            calls[0] += 1
            if calls[0] == 2:
                raise ac.ActiveScanError("injected")
            return False
        result = self.engine(active_check=indeterminate).run()
        self.assertEqual(result["skipped"], "indeterminate active scan")
        self.assertTrue(first.exists() and second.exists())

    def test_failed_stale_deletion_does_not_suppress_cap_eviction(self):
        first = self.chromium_tmp(age=1000)
        second = self.chromium_tmp("org.chromium.Chromium.B2c3D4", age=200)
        real_unlink = ac._unlink_tree
        def fail_first(path):
            if path == first:
                raise PermissionError("injected")
            real_unlink(path)
        with patch.dict(ac.POLICY["critical"], {"temp_stale": 500, "temp_min": 100, "temp_cap": 0}):
            self.snapshot[:] = [100 * ac.GiB, 95 * ac.GiB, 5 * ac.GiB]
            with patch.object(ac, "_unlink_tree", side_effect=fail_first):
                result = self.engine().run()
        self.assertTrue(first.exists())
        self.assertFalse(second.exists())
        self.assertEqual(result["stats"]["temp"]["failed"], 1)
        self.assertEqual(result["stats"]["temp"]["cap_deleted"], 1)

    def test_status_guard_excludes_cleanup_from_live_status_work(self):
        import threading
        entered = threading.Event()
        release = threading.Event()
        @ac.status_protected
        def guarded():
            entered.set()
            release.wait(2)
        worker = threading.Thread(target=guarded)
        worker.start()
        self.assertTrue(entered.wait(1))
        try:
            self.assertEqual(ac.cleanup(), {"skipped": "status active"})
        finally:
            release.set()
            worker.join(2)


    def test_unreadable_same_uid_proc_entry_fails_closed(self):
        proc = self.root / "proc"
        (proc / "self").mkdir(parents=True)
        (proc / "self" / "cgroup").write_text("0::/test")
        (proc / "net").mkdir()
        (proc / "net" / "unix").write_text("Num RefCount Protocol Flags Type St Inode Path\n")
        pid = proc / "123"
        pid.mkdir()
        (pid / "cgroup").write_text("0::/test")
        command = pid / "cmdline"
        command.write_bytes(b"unrelated\0")
        (pid / "maps").write_text("")
        (pid / "fd").mkdir()
        (pid / "exe").symlink_to("/bin/sleep")
        (pid / "cwd").symlink_to("/")
        (pid / "root").symlink_to("/")
        real_read = Path.read_bytes
        def unreadable(path):
            if path == command:
                raise PermissionError("injected")
            return real_read(path)
        with patch.object(Path, "read_bytes", unreadable):
            with self.assertRaises(ac.ActiveScanError):
                ac._proc_references((self.tmp,), proc_root=proc)

    def test_processes_outside_portal_cgroup_are_out_of_scope(self):
        candidate = self.chromium_tmp(age=99_999)
        proc = self.root / "proc-other-cgroup"
        (proc / "self").mkdir(parents=True)
        (proc / "self" / "cgroup").write_text("0::/portal")
        (proc / "net").mkdir()
        (proc / "net" / "unix").write_text("Num RefCount Protocol Flags Type St Inode Path\n")
        pid = proc / "123"
        pid.mkdir()
        (pid / "cgroup").write_text("0::/unrelated")
        (pid / "cmdline").write_bytes(os.fsencode(str(candidate)) + b"\0")
        self.assertFalse(ac._proc_references((candidate,), proc_root=proc))

    def test_symlink_swap_after_lstat_cannot_escape_recursive_delete(self):
        candidate = self.chromium_tmp(age=99_999)
        sentinel = self.root / "external-sentinel"
        sentinel.mkdir()
        payload = sentinel / "world-data"
        payload.write_text("preserve")
        backup = candidate.with_name(candidate.name + ".backup")
        real_open = ac.os.open
        swapped = [False]
        def racing_open(path, *args, **kwargs):
            if not swapped[0] and os.fspath(path) == os.fspath(candidate):
                swapped[0] = True
                candidate.rename(backup)
                candidate.symlink_to(sentinel, target_is_directory=True)
            return real_open(path, *args, **kwargs)
        try:
            with patch.object(ac.os, "open", side_effect=racing_open):
                with self.assertRaises(OSError):
                    ac._unlink_tree(candidate)
            self.assertEqual(payload.read_text(), "preserve")
        finally:
            if candidate.is_symlink():
                candidate.unlink()
            if backup.exists():
                ac.shutil.rmtree(backup)


    def test_live_unix_socket_path_protects_candidate_across_cgroups(self):
        candidate = self.chromium_tmp(age=99_999)
        socket_path = candidate / "SingletonSocket"
        proc = self.root / "proc-socket"
        (proc / "self").mkdir(parents=True)
        (proc / "self" / "cgroup").write_text("0::/portal")
        (proc / "net").mkdir()
        (proc / "net" / "unix").write_text(
            "Num RefCount Protocol Flags Type St Inode Path\n"
            f"000: 00000002 00000000 00010000 0001 01 4242 {socket_path}\n"
        )
        self.assertTrue(ac._proc_references((candidate,), proc_root=proc))


    def test_activity_acquired_after_prescan_blocks_final_delete(self):
        candidate = self.chromium_tmp(age=99_999)
        calls = [0]
        def becomes_active(paths):
            calls[0] += 1
            return calls[0] >= 3
        result = self.engine(active_check=becomes_active).run()
        self.assertEqual(result["deleted"], 0)
        self.assertTrue(candidate.exists())
        self.assertGreaterEqual(calls[0], 3)


    def test_cgroup_quiet_wait_handles_idle_and_busy_boundaries(self):
        proc = self.root / "proc-cgroup"
        groups = self.root / "cgroups"
        (proc / "self").mkdir(parents=True)
        (proc / "self" / "cgroup").write_text("0::/system.slice/portal.service")
        service = groups / "system.slice" / "portal.service"
        service.mkdir(parents=True)
        procs = service / "cgroup.procs"
        procs.write_text(str(os.getpid()))
        self.assertTrue(ac._wait_for_cgroup_quiet(proc_root=proc, cgroup_root=groups))
        procs.write_text(f"{os.getpid()} 99999")
        clock = [0.0]
        def advance(_):
            clock[0] += 0.2
        self.assertFalse(
            ac._wait_for_cgroup_quiet(
                timeout=0.4, proc_root=proc, cgroup_root=groups,
                monotonic=lambda: clock[0], sleep=advance,
            )
        )



if __name__ == "__main__":
    unittest.main()
