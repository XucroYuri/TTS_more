from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TypeVar

import yaml
from pydantic import BaseModel

from app.models import Character, GenerationManifest, ScriptProject

T = TypeVar("T", bound=BaseModel)


class ProjectStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def project_dir(self, project_id: str) -> Path:
        safe_id = self._safe_project_id(project_id)
        for root in self.read_project_roots():
            candidate = root / safe_id
            if (candidate / "project.json").exists() or (candidate / "manifest.json").exists():
                return candidate
        return self.writable_project_dir(safe_id)

    def writable_project_dir(self, project_id: str) -> Path:
        return self.writable_projects_root() / self._safe_project_id(project_id)

    def project_path(self, project_id: str) -> Path:
        return self.project_dir(project_id) / "project.json"

    def manifest_path(self, project_id: str) -> Path:
        return self.project_dir(project_id) / "manifest.json"

    def characters_path(self) -> Path:
        return self._resolve_characters_paths()[0]

    def writable_characters_path(self) -> Path:
        return self._resolve_characters_paths()[1]

    def save_project(self, project_id: str, project: ScriptProject) -> None:
        self._write_model(self.writable_project_dir(project_id) / "project.json", project)

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
                if path.name in seen or not path.is_dir() or not project_path.exists():
                    continue
                project = self._read_model(project_path, ScriptProject)
                projects.append(
                    {
                        "project_id": path.name,
                        "title": project.title,
                        "default_language": project.default_language,
                        "line_count": len(project.lines),
                    }
                )
                seen.add(path.name)
        return projects

    def read_project_roots(self) -> list[Path]:
        env_path = os.environ.get("TTS_MORE_PROJECTS_PATH")
        if env_path:
            return [Path(env_path)]
        local_path = self.root / "local" / "projects"
        if local_path.exists():
            return [local_path]
        projects_path = self.root / "projects"
        if projects_path.exists():
            return [projects_path]
        return [self.root]

    def writable_projects_root(self) -> Path:
        env_path = os.environ.get("TTS_MORE_PROJECTS_PATH")
        if env_path:
            return Path(env_path)
        return self.root / "local" / "projects"

    def save_manifest(self, manifest: GenerationManifest) -> None:
        self._write_model(self.manifest_path(manifest.project_id), manifest)

    def load_manifest(self, project_id: str) -> GenerationManifest:
        path = self.manifest_path(project_id)
        if not path.exists():
            return GenerationManifest(project_id=project_id)
        return self._read_model(path, GenerationManifest)

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
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(path)

    def _read_model(self, path: Path, model_type: type[T]) -> T:
        return model_type.model_validate(self._read_structured(path))

    def _read_structured(self, path: Path) -> object:
        text = path.read_text(encoding="utf-8")
        if path.suffix.lower() in {".yaml", ".yml"}:
            return yaml.safe_load(text)
        return json.loads(text)

    def _safe_project_id(self, project_id: str) -> str:
        value = project_id.strip()
        if not value:
            raise ValueError("project id is required")
        if value in {".", ".."} or any(separator in value for separator in ("/", "\\")):
            raise ValueError("project id must be a single path segment")
        if ":" in value or Path(value).is_absolute():
            raise ValueError("project id must be relative")
        return value

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
