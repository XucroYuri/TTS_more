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
        return self.root / self._safe_project_id(project_id)

    def project_path(self, project_id: str) -> Path:
        return self.project_dir(project_id) / "project.json"

    def manifest_path(self, project_id: str) -> Path:
        return self.project_dir(project_id) / "manifest.json"

    def characters_path(self) -> Path:
        return self.root / "characters.json"

    def save_project(self, project_id: str, project: ScriptProject) -> None:
        self._write_model(self.project_path(project_id), project)

    def load_project(self, project_id: str) -> ScriptProject:
        return self._read_model(self.project_path(project_id), ScriptProject)

    def list_projects(self) -> list[dict[str, object]]:
        if not self.root.exists():
            return []
        projects: list[dict[str, object]] = []
        for path in sorted(self.root.iterdir(), key=lambda item: item.name.lower()):
            project_path = path / "project.json"
            if not path.is_dir() or not project_path.exists():
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
        return projects

    def save_manifest(self, manifest: GenerationManifest) -> None:
        self._write_model(self.manifest_path(manifest.project_id), manifest)

    def load_manifest(self, project_id: str) -> GenerationManifest:
        path = self.manifest_path(project_id)
        if not path.exists():
            return GenerationManifest(project_id=project_id)
        return self._read_model(path, GenerationManifest)

    def save_characters(self, characters: list[Character]) -> None:
        self._write_json(self.characters_path(), [c.model_dump(mode="json") for c in characters])

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
