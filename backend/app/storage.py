from __future__ import annotations

import json
import os
import re
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeVar

import yaml
from pydantic import BaseModel

from app.models import Character, GenerationManifest, ScriptProject

T = TypeVar("T", bound=BaseModel)

WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
_ATOMIC_REPLACE_LOCKS = tuple(threading.Lock() for _ in range(64))


def _atomic_replace_lock(path: Path) -> threading.Lock:
    normalized = os.path.normcase(
        os.path.realpath(os.path.abspath(os.fspath(path)))
    )
    return _ATOMIC_REPLACE_LOCKS[hash(normalized) % len(_ATOMIC_REPLACE_LOCKS)]


class ProjectStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def project_dir(self, project_id: str) -> Path:
        safe_id = self._safe_project_id(project_id)
        for root in self.read_project_roots():
            candidate = root / safe_id
            if self._is_project_dir(candidate) and self._project_dir_matches_id(candidate, safe_id):
                return candidate
            marked = self._find_project_dir_by_id(root, safe_id)
            if marked is not None:
                return marked
        return self.writable_project_dir(safe_id)

    def writable_project_dir(self, project_id: str) -> Path:
        safe_id = self._safe_project_id(project_id)
        direct = self.writable_projects_root() / safe_id
        marker = self._read_project_marker(direct)
        if not direct.exists() or marker is None or marker == safe_id:
            return direct
        return self._unique_project_dir_for_title(self.writable_projects_root(), safe_id, safe_id)

    def project_path(self, project_id: str) -> Path:
        return self.project_dir(project_id) / "project.json"

    def manifest_path(self, project_id: str) -> Path:
        return self.project_output_dir(project_id) / "manifest.json"

    def legacy_manifest_path(self, project_id: str) -> Path:
        return self.project_dir(project_id) / "manifest.json"

    def project_script_dir(self, project_id: str) -> Path:
        return self.project_dir(project_id) / "script"

    def project_output_dir(self, project_id: str) -> Path:
        return self.project_dir(project_id) / "output"

    def project_audio_dir(self, project_id: str) -> Path:
        return self.project_output_dir(project_id) / "audio"

    def project_reference_audio_dir(self, project_id: str) -> Path:
        return self.project_output_dir(project_id) / "reference_audio"

    def characters_path(self) -> Path:
        return self._resolve_characters_paths()[0]

    def writable_characters_path(self) -> Path:
        return self._resolve_characters_paths()[1]

    def save_project(self, project_id: str, project: ScriptProject) -> None:
        safe_id = self._safe_project_id(project_id)
        project_dir = self._writable_project_dir_for_project(safe_id, project)
        self._write_text(project_dir / ".project-id", safe_id)
        self._write_model(project_dir / "project.json", project)
        self._write_project_materialized_files(project_dir, project)

    def load_project(self, project_id: str) -> ScriptProject:
        return self._read_model(self.project_path(project_id), ScriptProject)

    def list_projects(self) -> list[dict[str, object]]:
        projects: list[dict[str, object]] = []
        seen: set[str] = set()
        for projects_root in self.read_project_roots():
            if not projects_root.exists():
                continue
            for path in sorted(projects_root.iterdir(), key=lambda item: item.name.lower()):
                project_path = path / "project.json"
                if not path.is_dir() or not project_path.exists():
                    continue
                project = self._read_model(project_path, ScriptProject)
                project_id = self._project_id_for_dir(path)
                if project_id in seen:
                    continue
                projects.append(
                    {
                        "project_id": project_id,
                        "title": project.title,
                        "default_language": project.default_language,
                        "line_count": len(project.lines),
                        "character_count": len(project.project_characters),
                        "script_revision_count": len(project.script_revisions),
                        "parse_revision_count": len(project.parse_revisions),
                        "updated_at": datetime.fromtimestamp(project_path.stat().st_mtime, timezone.utc).isoformat(),
                    }
                )
                seen.add(project_id)
        return projects

    def delete_project(self, project_id: str) -> Path:
        safe_id = self._safe_project_id(project_id)
        project_dir = self.project_dir(safe_id)
        if not self._is_project_dir(project_dir):
            raise FileNotFoundError(project_id)

        trash_root = self.writable_projects_root() / ".trash"
        trash_root.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        target = trash_root / f"{safe_id}-{timestamp}"
        for index in range(1000):
            candidate = target if index == 0 else trash_root / f"{safe_id}-{timestamp}-{index + 1}"
            if not candidate.exists():
                shutil.move(str(project_dir), str(candidate))
                return candidate
        raise FileExistsError("unable to allocate trash directory")

    def read_project_roots(self) -> list[Path]:
        env_path = os.environ.get("TTS_MORE_PROJECTS_PATH")
        if env_path:
            return [Path(env_path)]
        roots = [self.default_projects_root()]
        local_path = self.root / "local" / "projects"
        projects_path = self.root / "projects"
        roots.extend([local_path, projects_path, self.root])
        output: list[Path] = []
        seen: set[str] = set()
        for path in roots:
            key = str(path.resolve(strict=False)).lower()
            if key not in seen:
                output.append(path)
                seen.add(key)
        return output

    def default_projects_root(self) -> Path:
        if self.root.name.lower() == "data":
            return self.root.parent / "Project"
        return self.root / "Project"

    def writable_projects_root(self) -> Path:
        env_path = os.environ.get("TTS_MORE_PROJECTS_PATH")
        if env_path:
            return Path(env_path)
        return self.default_projects_root()

    def save_manifest(self, manifest: GenerationManifest) -> None:
        self._write_text(self.project_dir(manifest.project_id) / ".project-id", self._safe_project_id(manifest.project_id))
        self._write_model(self.manifest_path(manifest.project_id), manifest)

    def load_manifest(self, project_id: str) -> GenerationManifest:
        path = self.manifest_path(project_id)
        if path.exists():
            return self._read_model(path, GenerationManifest)
        legacy_path = self.legacy_manifest_path(project_id)
        if legacy_path.exists():
            return self._read_model(legacy_path, GenerationManifest)
        return GenerationManifest(project_id=project_id)

    def save_characters(self, characters: list[Character]) -> None:
        self._write_json(self.writable_characters_path(), [c.model_dump(mode="json") for c in characters])

    def load_characters(self) -> list[Character]:
        path = self.characters_path()
        if not path.exists():
            return []
        data = self._read_structured(path)
        return [Character.model_validate(item) for item in data]

    def _write_model(self, path: Path, model: BaseModel) -> None:
        self._write_json(path, model.model_dump(mode="json"))

    def _write_json(self, path: Path, payload: object) -> None:
        self._write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))

    def _write_text(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temp_path.write_text(text, encoding="utf-8")
            with _atomic_replace_lock(path):
                temp_path.replace(path)
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _read_model(self, path: Path, model_type: type[T]) -> T:
        return model_type.model_validate(self._read_structured(path))

    def _read_structured(self, path: Path) -> object:
        text = path.read_text(encoding="utf-8")
        if path.suffix.lower() in {".yaml", ".yml"}:
            return yaml.safe_load(text)
        return json.loads(text)

    def _write_project_materialized_files(self, project_dir: Path, project: ScriptProject) -> None:
        script_dir = project_dir / "script"
        output_dir = project_dir / "output"
        script_dir.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)

        active_revision = next(
            (revision for revision in project.script_revisions if revision.revision_id == project.active_script_revision_id),
            project.script_revisions[-1] if project.script_revisions else None,
        )
        if active_revision is not None:
            self._write_text(script_dir / "active.md", active_revision.source_markdown)

        for revision in project.script_revisions:
            revision_name = f"{self._safe_file_stem(revision.revision_id)}.md"
            self._write_text(script_dir / "revisions" / revision_name, revision.source_markdown)

        for revision in project.parse_revisions:
            revision_name = f"{self._safe_file_stem(revision.revision_id)}.json"
            self._write_json(script_dir / "parse-revisions" / revision_name, revision.model_dump(mode="json"))

        self._write_json(output_dir / "lines.json", [line.model_dump(mode="json") for line in project.lines])

    def _writable_project_dir_for_project(self, project_id: str, project: ScriptProject) -> Path:
        root = self.writable_projects_root()
        existing = self._find_project_dir_by_id(root, project_id)
        desired = self._unique_project_dir_for_title(root, project.title, project_id)
        if existing is not None and existing != desired:
            desired.parent.mkdir(parents=True, exist_ok=True)
            if not desired.exists():
                existing.rename(desired)
            return desired
        return desired

    def _unique_project_dir_for_title(self, root: Path, title: str, project_id: str) -> Path:
        base_name = self._safe_project_title(title) or project_id
        for index in range(1000):
            name = base_name if index == 0 else f"{base_name}-{index + 1}"
            candidate = root / name
            if not candidate.exists():
                return candidate
            marker = self._read_project_marker(candidate)
            if marker == project_id:
                return candidate
        raise ValueError("unable to allocate project directory")

    def _find_project_dir_by_id(self, root: Path, project_id: str) -> Path | None:
        direct = root / project_id
        if self._is_project_dir(direct):
            marker = self._read_project_marker(direct)
            if marker is None or marker == project_id:
                return direct
        if not root.exists():
            return None
        for path in sorted(root.iterdir(), key=lambda item: item.name.lower()):
            if not path.is_dir() or path == direct:
                continue
            if self._read_project_marker(path) == project_id:
                return path
        return None

    def _is_project_dir(self, path: Path) -> bool:
        return (
            path.is_dir()
            and (
                (path / "project.json").exists()
                or (path / "output" / "manifest.json").exists()
                or (path / "manifest.json").exists()
            )
        )

    def _project_id_for_dir(self, path: Path) -> str:
        marker = self._read_project_marker(path)
        if marker:
            return marker
        return path.name

    def _project_dir_matches_id(self, path: Path, project_id: str) -> bool:
        marker = self._read_project_marker(path)
        return marker is None or marker == project_id

    def _read_project_marker(self, path: Path) -> str | None:
        marker_path = path / ".project-id"
        if not marker_path.exists():
            return None
        try:
            return self._safe_project_id(marker_path.read_text(encoding="utf-8").strip())
        except ValueError:
            return None

    def _safe_project_id(self, project_id: str) -> str:
        value = project_id.strip()
        if not value:
            raise ValueError("project id is required")
        if value in {".", ".."} or any(separator in value for separator in ("/", "\\")):
            raise ValueError("project id must be a single path segment")
        if ":" in value or Path(value).is_absolute():
            raise ValueError("project id must be relative")
        return value

    def _safe_project_title(self, title: str) -> str:
        value = title.strip()
        value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)
        value = re.sub(r"_+", "_", value).strip(" .")
        if value in {"", ".", ".."}:
            return ""
        if value.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES:
            value = f"{value}_"
        return value[:120]

    def _safe_file_stem(self, value: str) -> str:
        stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value.strip())
        stem = re.sub(r"_+", "_", stem).strip(" .")
        return stem or "revision"

    def _resolve_characters_paths(self) -> tuple[Path, Path]:
        env_path = os.environ.get("TTS_MORE_CHARACTERS_PATH")
        if env_path:
            path = Path(env_path)
            return path, path
        local_path = self.root / "local" / "characters.json"
        legacy_path = self.root / "characters.json"
        template_path = self.root / "templates" / "characters.example.json"
        for candidate in (local_path, legacy_path, template_path):
            if candidate.exists():
                return candidate, local_path
        return local_path, local_path
