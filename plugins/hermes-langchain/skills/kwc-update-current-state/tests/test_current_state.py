import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))
from current_state import candidate_sections, inspect, validate_document  # noqa: E402


REQUIRED_BODY = """# 현재 구현 상태

## 스냅샷
- 확인 기준 Git SHA: `{sha}`

{marker}## 전체 진행 요약
내용

## 백엔드 구조
내용

## 확인된 HTTP 진입점
내용

## 주요 구현 규칙
내용

## 데이터베이스
내용

## 테스트·검증
내용

## 알려진 차이와 다음 작업
내용
"""

MARKER = """## 진행 중 구현 작업
- 추적 기준: owner/repo#1
- 계보: 없음
- delivery branch: p1/issue1
- 최초 기준 SHA: 1111111111111111111111111111111111111111
- 최초 implementation snapshot SHA: 1111111111111111111111111111111111111111
- requirement 위치: docs/requirement/p1/user.md#회원가입
- 작업 요약: 회원가입 구현을 진행한다.

"""


class CurrentStateContractTest(unittest.TestCase):
    def test_skill_uses_helper_as_black_box(self):
        skill = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("kwc-orchestrator-contract/references/script-usage.md", skill)
        self.assertIn("helper 구현 소스는 읽지 않는다", skill)

    def git(self, repo: Path, *args: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return completed.stdout.strip()

    def write_document(self, repo: Path, sha: str, marker: str = "") -> Path:
        document = repo / "docs" / "current-state.md"
        document.parent.mkdir(parents=True, exist_ok=True)
        document.write_text(REQUIRED_BODY.format(sha=sha, marker=marker), encoding="utf-8")
        return document

    def init_repo(self, root: Path) -> tuple[Path, str]:
        repo = root / "repo"
        repo.mkdir()
        self.git(repo, "init", "-q")
        self.git(repo, "config", "user.email", "test@example.com")
        self.git(repo, "config", "user.name", "Test")
        source = repo / "src" / "main" / "java" / "App.java"
        source.parent.mkdir(parents=True)
        source.write_text("class App {}\n", encoding="utf-8")
        self.git(repo, "add", ".")
        self.git(repo, "commit", "-qm", "initial")
        return repo, self.git(repo, "rev-parse", "HEAD")

    def commit_document(self, repo: Path, sha: str, marker: str = "") -> Path:
        document = self.write_document(repo, sha, marker)
        self.git(repo, "add", "docs/current-state.md")
        self.git(repo, "commit", "-qm", "docs")
        return document

    def test_validates_required_schema_and_optional_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            document = Path(directory) / "current-state.md"
            document.write_text(REQUIRED_BODY.format(sha="1" * 40, marker=MARKER), encoding="utf-8")
            self.assertEqual([], validate_document(document))

    def test_rejects_frontend_and_obsolete_automation_sections(self):
        with tempfile.TemporaryDirectory() as directory:
            document = Path(directory) / "current-state.md"
            body = REQUIRED_BODY.format(sha="1" * 40, marker="")
            body += "\n## 프런트엔드\n내용\n\n## 테스트·자동화\n내용\n"
            document.write_text(body, encoding="utf-8")
            errors = validate_document(document)
            self.assertIn("OUT_OF_SCOPE_SECTION:프런트엔드", errors)
            self.assertIn("OUT_OF_SCOPE_SECTION:테스트·자동화", errors)

    def test_rejects_invalid_or_unexpected_marker_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            document = Path(directory) / "current-state.md"
            marker = MARKER.replace("- 최초 기준 SHA: " + "1" * 40, "- 최초 기준 SHA: short")
            marker += "- task ID: task-1\n"
            document.write_text(REQUIRED_BODY.format(sha="1" * 40, marker=marker), encoding="utf-8")
            errors = validate_document(document)
            self.assertIn("INVALID_MARKER_SHA:최초 기준 SHA", errors)
            self.assertIn("UNEXPECTED_MARKER_FIELD:task ID", errors)

    def test_missing_snapshot_section_returns_errors_instead_of_crashing(self):
        with tempfile.TemporaryDirectory() as directory:
            document = Path(directory) / "current-state.md"
            body = REQUIRED_BODY.format(sha="1" * 40, marker=MARKER).replace("## 스냅샷\n", "")
            document.write_text(body, encoding="utf-8")
            errors = validate_document(document)
            self.assertIn("MISSING_SECTION:스냅샷", errors)
            self.assertIn("INVALID_MARKER_POSITION", errors)

    def test_only_src_changes_make_snapshot_stale(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, snapshot = self.init_repo(Path(directory))
            document = self.commit_document(repo, snapshot)
            readme = repo / "README.md"
            readme.write_text("docs only\n", encoding="utf-8")
            self.git(repo, "add", "README.md")
            self.git(repo, "commit", "-qm", "docs outside src")
            self.assertEqual("fresh", inspect(repo, document)["freshness"])

            source = repo / "src" / "main" / "java" / "App.java"
            source.write_text("class App { int value; }\n", encoding="utf-8")
            self.git(repo, "add", "src/main/java/App.java")
            self.git(repo, "commit", "-qm", "backend")
            result = inspect(repo, document)
            self.assertEqual("stale", result["freshness"])
            self.assertEqual(["src/main/java/App.java"], result["covered_changed_paths"])

    def test_dirty_repo_is_insufficient_even_for_outside_src_change(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, snapshot = self.init_repo(Path(directory))
            document = self.commit_document(repo, snapshot)
            (repo / "notes.txt").write_text("dirty\n", encoding="utf-8")
            result = inspect(repo, document)
            self.assertEqual("insufficient", result["freshness"])
            self.assertIn("WORKTREE_DIRTY", result["errors"])

    def test_candidate_sections_cover_main_test_and_migration(self):
        sections = candidate_sections(
            [
                "src/main/java/example/Controller.java",
                "src/main/resources/db/migration/V2__x.sql",
                "src/test/java/example/ControllerTest.java",
            ]
        )
        self.assertEqual(
            [
                "전체 진행 요약",
                "백엔드 구조",
                "확인된 HTTP 진입점",
                "주요 구현 규칙",
                "데이터베이스",
                "테스트·검증",
                "알려진 차이와 다음 작업",
            ],
            sections,
        )

    def test_cli_emits_machine_readable_json(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, snapshot = self.init_repo(Path(directory))
            document = self.commit_document(repo, snapshot)
            completed = subprocess.run(
                [
                    sys.executable,
                    str(SKILL_DIR / "scripts" / "current_state.py"),
                    "inspect",
                    "--repo",
                    str(repo),
                    "--document",
                    str(document),
                ],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            payload = json.loads(completed.stdout)
            self.assertEqual("current-state-inspection-v1", payload["schema"])
            self.assertEqual("fresh", payload["freshness"])


if __name__ == "__main__":
    unittest.main()
