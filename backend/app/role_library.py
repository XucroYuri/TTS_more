from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.models import (
    Character,
    EngineName,
    ProjectCharacter,
    ProjectCharacterMode,
    ReferenceAudioGroup,
    ReferenceAudioSample,
    ScriptProject,
    VoiceBinding,
    VoiceProfile,
)
from app.resources import AUDIO_SUFFIXES, GPT_WEIGHT_SUFFIXES, SOVITS_WEIGHT_SUFFIXES


TEXT_SUFFIXES = [".txt", ".lab", ".json"]
COMMON_LOGS_PRESETS: list[dict[str, Any]] = [
    {"name": "珊珊", "logs_name": "许珺雯-山海奇缘-珊珊20260629", "nicknames": [], "match_names": ["珊珊"]},
    {"name": "卡皮巴拉", "logs_name": "张博华-卡皮巴拉", "nicknames": [], "match_names": ["卡皮巴拉"]},
    {"name": "九九", "logs_name": "许珺雯-虚拟游戏-九九", "nicknames": [], "match_names": ["九九"]},
    {"name": "胶布", "logs_name": "胶布TTS新-20260611", "nicknames": [], "match_names": ["胶布"]},
    {"name": "白泽", "logs_name": "白泽TTS新-20260611", "nicknames": [], "match_names": ["白泽"]},
    {"name": "死神", "logs_name": "死神TTS-华-v2ProPlus", "nicknames": [], "match_names": ["死神"]},
    {"name": "断恶", "logs_name": "断恶TTS-华-20251128", "nicknames": [], "match_names": ["断恶"]},
    {"name": "心辰", "logs_name": "心辰TTS-3", "nicknames": [], "match_names": ["心辰"]},
    {"name": "光头", "logs_name": "光头TTS新-20260611", "nicknames": ["小光", "光头胖子"], "match_names": ["光头", "光头TTS新"]},
    {"name": "眼镜", "logs_name": "TTS-大鹏眼镜", "nicknames": ["严镜", "眼镜哥"], "match_names": ["眼镜", "TTS-大鹏眼镜"]},
    {"name": "弱弱", "logs_name": "2张悦荷-弱弱-251126-已训练2r", "nicknames": [], "match_names": ["弱弱"]},
]
PINYIN_FALLBACK = {
    "小": "xiao",
    "品": "pin",
    "美": "mei",
    "王": "wang",
    "强": "qiang",
    "旁": "pang",
    "白": "bai",
    "九": "jiu",
    "妈": "ma",
}


def common_logs_presets() -> list[dict[str, Any]]:
    return [dict(item, nicknames=list(item.get("nicknames", [])), match_names=list(item.get("match_names", []))) for item in COMMON_LOGS_PRESETS]


def scan_role_library_candidates(
    reference_audio_root: Path,
    gpt_weights_roots: list[Path],
    sovits_weights_roots: list[Path],
    limit: int = 80,
) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    _collect_weight_candidates(grouped, "gpt", gpt_weights_roots, GPT_WEIGHT_SUFFIXES)
    _collect_weight_candidates(grouped, "sovits", sovits_weights_roots, SOVITS_WEIGHT_SUFFIXES)
    _collect_reference_candidates(grouped, reference_audio_root)

    candidates = [item for item in grouped.values() if item.get("gpt_weights") or item.get("sovits_weights") or item.get("reference_audio_groups")]
    candidates.sort(key=lambda item: (0 if item.get("recommended_gpt_weights_path") and item.get("recommended_sovits_weights_path") else 1, item["name"]))
    return candidates[:limit]


def scan_logs_index_candidates(
    reference_audio_root: Path,
    gpt_weights_roots: list[Path],
    sovits_weights_roots: list[Path],
    service_id: str | None = None,
    gradio_candidates: list[dict[str, Any]] | None = None,
    limit: int = 80,
) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for candidate in scan_role_library_candidates(reference_audio_root, gpt_weights_roots, sovits_weights_roots, limit=limit * 2):
        _merge_logs_candidate(grouped, _logs_candidate_from_scan(candidate, service_id=service_id, source="filesystem"))
    for candidate in gradio_candidates or []:
        _merge_logs_candidate(grouped, _logs_candidate_from_scan(candidate, service_id=candidate.get("service_id") or service_id, source=candidate.get("source", "gradio")))
    _merge_common_logs_presets(grouped, service_id)
    candidates = list(grouped.values())
    candidates.sort(key=lambda item: (0 if item.get("recommended_gpt_weights_path") and item.get("recommended_sovits_weights_path") else 1, item["logs_name"]))
    return candidates[:limit]


def candidate_to_character(candidate: dict[str, Any]) -> Character:
    name = str(candidate["name"])
    character_id = str(candidate.get("id") or slugify_role_name(name))
    service_id = str(candidate.get("service_id") or "local-gpt-sovits")
    gpt_path = candidate.get("recommended_gpt_weights_path")
    sovits_path = candidate.get("recommended_sovits_weights_path")
    groups = [ReferenceAudioGroup.model_validate(group) for group in candidate.get("reference_audio_groups", [])]
    first_sample = _first_sample(groups)
    prompt_text = first_sample.text if first_sample else ""
    ref_audio_path = first_sample.path if first_sample else None
    gpt_complete = bool(gpt_path and sovits_path and ref_audio_path)

    profiles: list[VoiceProfile] = []
    if gpt_path or sovits_path or ref_audio_path:
        config = {
            "logs_id": candidate.get("logs_id"),
            "logs_name": candidate.get("logs_name"),
            "character_filter": candidate.get("logs_name"),
            "gpt_weight_options": candidate.get("gpt_weights") or [],
            "sovits_weight_options": candidate.get("sovits_weights") or [],
            "gpt_weights_path": gpt_path,
            "sovits_weights_path": sovits_path,
            "ref_audio_path": ref_audio_path,
            "prompt_text": prompt_text,
            "prompt_lang": "zh",
        }
        profiles.append(
            VoiceProfile(
                id=f"{character_id}-gpt",
                name=f"{name} GPT-SoVITS",
                engine=EngineName.GPT_SOVITS,
                service_id=service_id,
                bindings=[
                    VoiceBinding(
                        binding_id=f"{character_id}-gpt-binding",
                        provider_type="gpt-sovits",
                        service_id=service_id,
                        capabilities=["trained_weights_voice", "reference_audio_voice"],
                        config=_compact(config),
                    )
                ],
                config={},
            )
        )
    if groups:
        profiles.append(
            VoiceProfile(
                id=f"{character_id}-index",
                name=f"{name} IndexTTS",
                engine=EngineName.INDEX_TTS,
                service_id="local-indextts",
                bindings=[
                    VoiceBinding(
                        binding_id=f"{character_id}-index-binding",
                        provider_type="indextts",
                        service_id="local-indextts",
                        capabilities=["reference_audio_voice", "emotion_text"],
                        config=_compact({"voice": ref_audio_path, "emotion_mode": "same_as_voice"}),
                    )
                ],
                config={},
            )
        )

    return Character(
        id=character_id,
        name=name,
        aliases=list(dict.fromkeys([name, *(candidate.get("aliases") or [])])),
        nicknames=list(dict.fromkeys(candidate.get("nicknames") or [])),
        match_names=list(dict.fromkeys([*(candidate.get("match_names") or []), *(candidate.get("logs_match_names") or [])])),
        tags=list(dict.fromkeys([*(candidate.get("tags") or []), "logs-first"])),
        library_status="confirmed" if gpt_complete else "partial",
        source_assets={
            "scan": {
                "created_at": datetime.now(timezone.utc).isoformat(),
                "gpt_weight_count": len(candidate.get("gpt_weights", [])),
                "sovits_weight_count": len(candidate.get("sovits_weights", [])),
                "reference_audio_count": sum(len(group.samples) for group in groups),
                "logs_name": candidate.get("logs_name"),
            }
        },
        reference_audio_groups=groups,
        profiles=profiles,
        default_engine=profiles[0].engine if profiles else None,
        default_profile=profiles[0].id if profiles else None,
        fallback_profiles=[profile.id for profile in profiles[1:]],
    )


def match_project_characters(project: ScriptProject, library: list[Character]) -> list[ProjectCharacter]:
    if project.project_characters:
        return project.project_characters
    output: list[ProjectCharacter] = []
    seen: set[str] = set()
    by_name = _library_lookup(library)
    for line in project.lines:
        if line.character_id in seen:
            continue
        seen.add(line.character_id)
        character = by_name.get(_normalize(line.character_id))
        output.append(
            ProjectCharacter(
                project_character_id=line.character_id,
                name=character.name if character else line.character_id,
                library_character_id=character.id if character else None,
                mode=ProjectCharacterMode.REFERENCE,
                match_confidence=1.0 if character else None,
                match_status="matched" if character else "unmatched",
            )
        )
    return output


def resolve_project_characters(project: ScriptProject, library: list[Character]) -> list[Character]:
    output: list[Character] = []
    by_id = {character.id: character for character in library}
    mappings = match_project_characters(project, library)
    for item in mappings:
        source: Character | None = None
        if item.mode == ProjectCharacterMode.SNAPSHOT and item.character_snapshot:
            source = item.character_snapshot
        elif item.library_character_id:
            source = by_id.get(item.library_character_id)
        if source is None:
            output.append(
                Character(
                    id=item.project_character_id,
                    name=item.name,
                    aliases=[item.name],
                    library_status="draft",
                    profiles=[],
                    default_engine=None,
                    default_profile=None,
                )
            )
            continue
        output.append(source.model_copy(deep=True, update={"id": item.project_character_id, "name": item.name or source.name}))
    return output


def freeze_project_character(project: ScriptProject, project_character_id: str, library: list[Character]) -> ProjectCharacter:
    mappings = match_project_characters(project, library)
    by_id = {character.id: character for character in library}
    target: ProjectCharacter | None = None
    for item in mappings:
        if item.project_character_id == project_character_id:
            target = item
            break
    if target is None:
        raise KeyError(project_character_id)
    source = target.character_snapshot if target.mode == ProjectCharacterMode.SNAPSHOT else by_id.get(target.library_character_id or "")
    if source is None:
        raise ValueError(f"project character {project_character_id!r} is not linked to a library character")
    frozen = target.model_copy(deep=True, update={"mode": ProjectCharacterMode.SNAPSHOT, "character_snapshot": source.model_copy(deep=True)})
    project.project_characters = [frozen if item.project_character_id == project_character_id else item for item in mappings]
    return frozen


def referenced_projects(projects: list[tuple[str, ScriptProject]], character_id: str) -> list[str]:
    refs: list[str] = []
    for project_id, project in projects:
        for item in project.project_characters:
            if item.mode == ProjectCharacterMode.REFERENCE and item.library_character_id == character_id:
                refs.append(project_id)
                break
    return refs


def slugify_role_name(name: str) -> str:
    tokens: list[str] = []
    for char in name.strip():
        if char.isascii() and char.isalnum():
            tokens.append(char.lower())
        elif char in PINYIN_FALLBACK:
            tokens.append(PINYIN_FALLBACK[char])
    if not tokens:
        tokens = [re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "role"]
    return "-".join(tokens)


def _collect_weight_candidates(grouped: dict[str, dict[str, Any]], kind: str, roots: list[Path], suffixes: set[str]) -> None:
    field = f"{kind}_weights"
    recommended = f"recommended_{kind}_weights_path"
    for root in roots:
        if not root.exists() or not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in suffixes:
                continue
            name = _extract_role_name(path.stem)
            item = _candidate(grouped, name)
            score = _weight_score(path.stem)
            item.setdefault(field, []).append({"name": path.name, "path": str(path), "score": score})
            current = item.get(recommended)
            if current is None or score > item.get(f"{recommended}_score", (-1, -1)):
                item[recommended] = str(path)
                item[f"{recommended}_score"] = score


def _collect_reference_candidates(grouped: dict[str, dict[str, Any]], root: Path) -> None:
    if not root.exists() or not root.is_dir():
        return
    for child in root.iterdir():
        if not child.is_dir():
            continue
        samples: list[ReferenceAudioSample] = []
        for path in child.rglob("*"):
            if path.is_file() and path.suffix.lower() in AUDIO_SUFFIXES:
                samples.append(_reference_sample(path))
        if not samples:
            continue
        name = _extract_role_name(child.name)
        item = _candidate(grouped, name)
        item.setdefault("reference_audio_groups", []).append(
            ReferenceAudioGroup(
                id=child.name,
                name=child.name,
                paths=[str(child)],
                samples=samples[:8],
            ).model_dump(mode="json")
        )


def _candidate(grouped: dict[str, dict[str, Any]], name: str) -> dict[str, Any]:
    key = _normalize(name)
    if key not in grouped:
        grouped[key] = {"id": slugify_role_name(name), "name": name, "aliases": [name], "gpt_weights": [], "sovits_weights": [], "reference_audio_groups": []}
    return grouped[key]


def _extract_role_name(raw: str) -> str:
    text = re.sub(r"^\d+", "", raw)
    text = re.split(r"[-_]", text, maxsplit=1)[0]
    text = re.sub(r"[（(].*", "", text).strip()
    return text or raw


def _weight_score(stem: str) -> tuple[int, int]:
    epoch = max([int(match) for match in re.findall(r"(?:^|[-_])e(\d+)", stem, flags=re.IGNORECASE)] or [0])
    step = max([int(match) for match in re.findall(r"(?:^|[-_])s(\d+)", stem, flags=re.IGNORECASE)] or [0])
    return (epoch, step)


def _reference_sample(path: Path) -> ReferenceAudioSample:
    text = ""
    text_source = "none"
    for suffix in TEXT_SUFFIXES:
        sidecar = path.with_suffix(suffix)
        if sidecar.exists():
            text = _read_text_sidecar(sidecar)
            text_source = "sidecar" if text else "none"
            break
    return ReferenceAudioSample(path=str(path), text=text, text_source=text_source)


def _read_text_sidecar(path: Path) -> str:
    try:
        if path.suffix.lower() == ".json":
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, str):
                return payload.strip()
            if isinstance(payload, dict):
                return str(payload.get("text") or payload.get("prompt_text") or "").strip()
            return ""
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return ""


def _first_sample(groups: list[ReferenceAudioGroup]) -> ReferenceAudioSample | None:
    for group in groups:
        if group.samples:
            return group.samples[0]
    return None


def _library_lookup(library: list[Character]) -> dict[str, Character]:
    lookup: dict[str, Character] = {}
    for character in library:
        for value in _character_match_values(character):
            lookup[_normalize(value)] = character
    return lookup


def _character_match_values(character: Character) -> list[str]:
    return list(
        dict.fromkeys(
            [
                character.id,
                character.name,
                *character.aliases,
                *character.nicknames,
                *character.match_names,
            ]
        )
    )


def _normalize(value: str) -> str:
    return re.sub(r"\s+", "", value).casefold()


def _compact(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value not in (None, "")}


def _logs_candidate_from_scan(candidate: dict[str, Any], service_id: str | None, source: str) -> dict[str, Any]:
    name = str(candidate.get("logs_name") or candidate.get("name") or "unknown")
    logs_id = str(candidate.get("logs_id") or candidate.get("id") or slugify_role_name(name))
    return {
        **candidate,
        "id": str(candidate.get("id") or logs_id),
        "logs_id": logs_id,
        "logs_name": name,
        "name": str(candidate.get("name") or name),
        "aliases": list(dict.fromkeys([*(candidate.get("aliases") or []), name])),
        "service_id": service_id,
        "source": source,
        "gpt_weights": candidate.get("gpt_weights") or [],
        "sovits_weights": candidate.get("sovits_weights") or [],
        "reference_audio_groups": candidate.get("reference_audio_groups") or [],
    }


def _merge_logs_candidate(grouped: dict[str, dict[str, Any]], candidate: dict[str, Any]) -> None:
    key = f"{candidate.get('service_id') or 'filesystem'}::{candidate['logs_id']}"
    if key not in grouped:
        grouped[key] = candidate
        return
    current = grouped[key]
    current["source"] = "merged" if current.get("source") != candidate.get("source") else current.get("source")
    current["aliases"] = list(dict.fromkeys([*(current.get("aliases") or []), *(candidate.get("aliases") or [])]))
    current["gpt_weights"] = _merge_options(current.get("gpt_weights") or [], candidate.get("gpt_weights") or [])
    current["sovits_weights"] = _merge_options(current.get("sovits_weights") or [], candidate.get("sovits_weights") or [])
    current["reference_audio_groups"] = _merge_options(current.get("reference_audio_groups") or [], candidate.get("reference_audio_groups") or [])
    for field in ["recommended_gpt_weights_path", "recommended_sovits_weights_path", "recommended_ref_audio_path"]:
        if not current.get(field) and candidate.get(field):
            current[field] = candidate[field]
    for field in ["name", "nicknames", "match_names", "logs_match_names"]:
        if field in candidate and candidate.get(field):
            if field == "name" and (not current.get("name") or current.get("name") == current.get("logs_name")):
                current[field] = candidate[field]
            elif isinstance(candidate.get(field), list):
                current[field] = list(dict.fromkeys([*(current.get(field) or []), *candidate[field]]))


def _merge_options(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    output: list[dict[str, Any]] = []
    for item in [*left, *right]:
        marker = str(item.get("path") or item.get("id") or item.get("name") or item)
        if marker in seen:
            continue
        seen.add(marker)
        output.append(item)
    return output


def _merge_common_logs_presets(grouped: dict[str, dict[str, Any]], service_id: str | None) -> None:
    for preset in COMMON_LOGS_PRESETS:
        logs_name = str(preset["logs_name"])
        normalized_logs = _normalize(logs_name)
        matched_key = next(
            (
                key
                for key, candidate in grouped.items()
                if _normalize(str(candidate.get("logs_name") or "")) == normalized_logs
                or _normalize(str(candidate.get("name") or "")) == normalized_logs
            ),
            None,
        )
        candidate = _preset_candidate(preset, service_id)
        if matched_key is None:
            _merge_logs_candidate(grouped, candidate)
            continue
        current = grouped[matched_key]
        current["name"] = candidate["name"]
        current["aliases"] = list(dict.fromkeys([*(current.get("aliases") or []), candidate["name"], logs_name]))
        current["nicknames"] = list(dict.fromkeys([*(current.get("nicknames") or []), *(candidate.get("nicknames") or [])]))
        current["match_names"] = list(dict.fromkeys([*(current.get("match_names") or []), *(candidate.get("match_names") or [])]))
        current["logs_match_names"] = list(dict.fromkeys([*(current.get("logs_match_names") or []), logs_name]))
        current["preset"] = True


def _preset_candidate(preset: dict[str, Any], service_id: str | None) -> dict[str, Any]:
    name = str(preset["name"])
    logs_name = str(preset["logs_name"])
    return {
        "id": slugify_role_name(name),
        "logs_id": slugify_role_name(logs_name),
        "logs_name": logs_name,
        "name": name,
        "aliases": [name, logs_name],
        "nicknames": list(preset.get("nicknames") or []),
        "match_names": list(dict.fromkeys([*(preset.get("match_names") or []), logs_name])),
        "logs_match_names": [logs_name],
        "service_id": service_id or "lan-gpt-sovits-gradio-166",
        "source": "preset",
        "preset": True,
        "tags": ["common-preset"],
        "gpt_weights": [],
        "sovits_weights": [],
        "reference_audio_groups": [],
    }
