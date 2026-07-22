from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_default_ci_excludes_retired_portable_suite() -> None:
    workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert 'TTS_MORE_SKIP_LEGACY_PORTABLE: "1"' in workflow


def test_retired_portable_release_workflow_is_absent() -> None:
    assert not (REPO_ROOT / ".github" / "workflows" / "portable-release.yml").exists()
