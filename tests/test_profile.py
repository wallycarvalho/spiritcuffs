"""Profile staleness logic + evidence/profile persistence."""

from __future__ import annotations

import os
import time
from pathlib import Path

from spc.config import SpcConfig
from spc.githistory import GitEvidence
from spc.profile import (
    artifacts_dir,
    evidence_path,
    load_evidence,
    profile_age_days,
    profile_path,
    save_evidence,
    save_profile,
    staleness_notice,
)


def _evidence() -> GitEvidence:
    return GitEvidence(
        commits_scanned=3,
        hotspots=[("src/a.py", 3)],
        bug_classes=[{"keyword": "fix", "count": 2, "top_files": ["src/a.py"]}],
        test_dirs=["tests/"],
        test_co_change_ratio=0.5,
        language_proportions={"python": 100.0},
        total_files_tracked=1,
    )


def test_save_and_load_evidence_roundtrip(tmp_path: Path) -> None:
    save_evidence(tmp_path, _evidence())
    loaded = load_evidence(tmp_path)
    assert loaded is not None
    assert loaded["commits_scanned"] == 3
    assert loaded["hotspots"] == [["src/a.py", 3]]
    assert evidence_path(tmp_path).is_file()
    assert (artifacts_dir(tmp_path) / "evidence.json").is_file()


def test_load_evidence_missing_or_corrupt(tmp_path: Path) -> None:
    assert load_evidence(tmp_path) is None
    artifacts_dir(tmp_path).mkdir()
    evidence_path(tmp_path).write_text("{not json", encoding="utf-8")
    assert load_evidence(tmp_path) is None


def test_profile_age_and_staleness_notice(tmp_path: Path) -> None:
    assert profile_age_days(tmp_path) is None
    assert staleness_notice(tmp_path, 30) is None  # no profile: no notice in review path

    save_profile(tmp_path, "# profile")
    assert profile_age_days(tmp_path) is not None
    assert profile_age_days(tmp_path) < 1
    assert staleness_notice(tmp_path, 30) is None

    # Backdate the file 40 days.
    old = time.time() - 40 * 86400
    os.utime(profile_path(tmp_path), (old, old))
    notice = staleness_notice(tmp_path, 30)
    assert notice is not None
    assert "40 days old" in notice
    assert "spc learn" in notice


def test_save_profile_creates_dir(tmp_path: Path) -> None:
    save_profile(tmp_path, "narrative")
    assert profile_path(tmp_path).read_text(encoding="utf-8") == "narrative"


def test_staleness_config_boundary(tmp_path: Path) -> None:
    save_profile(tmp_path, "# p")
    old = time.time() - 31 * 86400
    os.utime(profile_path(tmp_path), (old, old))
    config = SpcConfig()  # profile_max_age_days = 30
    notice = staleness_notice(tmp_path, config.learn.profile_max_age_days)
    assert notice is not None
