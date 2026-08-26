from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.advisor import immutable_run
from scripts.advisor.immutable_run import protected_state


class ProtectedStateTests(unittest.TestCase):
    def _repo(self, root: Path) -> None:
        files = {
            "data/raw/input.bin": b"raw-input",
            "data/production/latest.json": b'{"run":"old"}',
            "data/development/advisor_full_reset/runs/run_id=old/report.html": b"old-run",
            "data/live/scores/latest.csv": b"ticker,score\n000001,1\n",
            "data/portfolio/evidence/account.json": b"account-evidence",
            "data/archive/quarantine_20260330/suspect.bin": b"quarantine-evidence",
            "data/backtest/reference.csv": b"month,return\n2020-01,0.1\n",
            "data/diagnostics/qa.json": b'{"status":"old"}',
            "data/processed/latest_scores__asof=old.csv": b"ticker,score\n000001,1\n",
            "data/processed/features__asof=old.csv": b"unprotected-source-feature",
            "data/archive/dart_finstate/source.xml": b"unprotected-source-archive",
            "artifacts/models/model.joblib": b"model-bytes",
            "artifacts/.run_id=historical.staging/evidence.json": b"protected-artifact",
        }
        for relative, content in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

        staging = (
            root
            / "data/development/advisor_full_reset/runs/.run_id=current.staging/report.html"
        )
        staging.parent.mkdir(parents=True, exist_ok=True)
        staging.write_bytes(b"mutable-current-run")

    def test_parallel_and_sequential_states_are_identical_and_staging_is_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._repo(root)
            sequential = protected_state(root, max_workers=1)
            parallel = protected_state(root, max_workers=4)

            self.assertEqual(parallel, sequential)
            self.assertEqual(
                parallel["contract"],
                "PROTECTED_EXISTING_PRODUCTION_MODEL_AND_IMMUTABLE_RUNS_V3",
            )
            self.assertEqual(parallel["scope"]["content_hash"], "SHA256_FULL_FILE_CONTENT")
            self.assertIn("data/development", parallel["scope"]["recursive_roots"])
            self.assertIn("data/processed", parallel["scope"]["recursive_roots"])
            self.assertEqual(parallel["file_count"], 11)

            staging = next(root.rglob(".run_id=current.staging/report.html"))
            staging.write_bytes(b"changed-but-still-staging")
            self.assertEqual(protected_state(root, max_workers=3), sequential)

            # Raw/source archives are deliberately outside the V3 invariant and
            # are not falsely claimed in the manifest.
            (root / "data/raw/input.bin").write_bytes(b"changed-unprotected-raw")
            (root / "data/archive/dart_finstate/source.xml").write_bytes(
                b"changed-unprotected-source"
            )
            self.assertEqual(protected_state(root, max_workers=3), sequential)

            # All processed model inputs/outputs are protected, not only a
            # filename prefix allowlist that could silently miss an artifact.
            (root / "data/processed/features__asof=old.csv").write_bytes(
                b"changed-protected-feature"
            )
            self.assertNotEqual(protected_state(root, max_workers=3), sequential)

    def test_content_change_same_size_and_restored_mtime_changes_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._repo(root)
            protected = root / "data/production/latest.json"
            before_stat = protected.stat()
            before = protected_state(root, max_workers=2)

            replacement = b'{"run":"new"}'
            self.assertEqual(len(replacement), before_stat.st_size)
            protected.write_bytes(replacement)
            os.utime(protected, ns=(before_stat.st_atime_ns, before_stat.st_mtime_ns))
            after = protected_state(root, max_workers=2)

            self.assertEqual(after["file_count"], before["file_count"])
            self.assertNotEqual(after["tree_sha256"], before["tree_sha256"])

    def test_worker_count_bounds_parallel_file_handles(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = root / "data/diagnostics"
            data.mkdir(parents=True)
            for index in range(40):
                (data / f"{index:03d}.txt").write_text(str(index), encoding="utf-8")

            original = immutable_run.sha256_file
            lock = threading.Lock()
            active = 0
            peak = 0

            def tracked(path: Path) -> str:
                nonlocal active, peak
                with lock:
                    active += 1
                    peak = max(peak, active)
                try:
                    time.sleep(0.005)
                    return original(path)
                finally:
                    with lock:
                        active -= 1

            with patch.object(immutable_run, "sha256_file", side_effect=tracked):
                state = protected_state(root, max_workers=4)

            self.assertEqual(state["file_count"], 40)
            self.assertGreater(peak, 1)
            self.assertLessEqual(peak, 4)
            self.assertEqual(immutable_run._resolved_hash_workers(10_000), 32)

    def test_change_during_hash_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "data/production/latest.json"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"before")
            original = immutable_run.sha256_file

            def mutate_then_hash(path: Path) -> str:
                path.write_bytes(b"after-content")
                return original(path)

            with patch.object(immutable_run, "sha256_file", side_effect=mutate_then_hash):
                with self.assertRaisesRegex(RuntimeError, "changed while hashing"):
                    protected_state(root, max_workers=1)


if __name__ == "__main__":
    unittest.main()
