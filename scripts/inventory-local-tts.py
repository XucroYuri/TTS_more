"""Inventory local model pairs without changing upstream projects."""
import hashlib
import json
import re
from pathlib import Path
from local_tts_config import integration_folder, load_config, source_path

config, _ = load_config(__doc__)
PROJECTS = config.get('gpt_sovits_projects', [])

def training_name(path):
    return re.sub(r"[-_]e\d+(?:_s\d+)?$", "", path.stem)

resources = {}
inventory = []
for project in PROJECTS:
  prefix, source = project['id'], source_path(project['source_root'])
  for version in ("v1", "v2", "v2Pro", "v2ProPlus", "v3", "v4"):
    suffix = "" if version == "v1" else "_" + version
    gpts = list((source / ("GPT_weights" + suffix)).glob("*.ckpt"))
    sovits = list((source / ("SoVITS_weights" + suffix)).glob("*.pth"))
    names = sorted({training_name(p) for p in [*gpts, *sovits]})
    for name in names:
        gs = [p for p in gpts if training_name(p) == name]
        ss = [p for p in sovits if training_name(p) == name]
        entry = {"project": prefix, "version": version, "name": name, "gpt_count": len(gs), "sovits_count": len(ss)}
        if gs and ss:
            epoch = lambda p: int(re.search(r"[-_]e(\d+)", p.stem).group(1)) if re.search(r"[-_]e(\d+)", p.stem) else 0
            gpt, sov = max(gs, key=epoch), max(ss, key=epoch)
            identity = name if version == "v2ProPlus" else version + ":" + name
            rid = prefix + "-" + hashlib.sha256(identity.encode()).hexdigest()[:12]
            pre = source / "GPT_SoVITS/pretrained_models"
            resource = {"engine": "gpt_sovits", "version": version, "source_root": str(source), "runtime_root": str(source), "python_executable": str(source_path(project['python_executable'])), "gpt_weight": str(gpt), "sovits_weight": str(sov), "bert_path": str(pre / "chinese-roberta-wwm-ext-large"), "cnhubert_path": str(pre / "chinese-hubert-base")}
            if version in {"v2Pro", "v2ProPlus"}:
                resource["sv_path"] = str(pre / "sv/pretrained_eres2netv2w24s4ep4.ckpt")
            entry.update(resource_id=rid, paths_exist=all(Path(v).exists() for k, v in resource.items() if k not in {"engine", "version"}))
            if entry["paths_exist"]:
                resources[rid] = resource
        mapped_logs = project.get('logs_mapping', {}).get(name)
        logs = source_path(mapped_logs) if mapped_logs else source / "logs" / name
        entry["logs_path"] = str(logs)
        entry["logs_exists"] = logs.is_dir()
        annotation = logs / '2-name2text.txt'
        entry["annotation_files"] = [str(annotation)] if annotation.is_file() else []
        inventory.append(entry)
for project in config.get('indextts_projects', []):
    index = source_path(project['source_root'])
    resource = {"engine": "index_tts", "source_root": str(index), "model_dir": str(source_path(project.get('model_dir', index / 'checkpoints'))), "python_executable": str(source_path(project['python_executable']))}
    if not all(Path(value).exists() for key, value in resource.items() if key != 'engine'):
        raise FileNotFoundError(f"IndexTTS resource paths are incomplete: {project['id']}")
    resources[project['id']] = resource
out = integration_folder(config)
out.mkdir(parents=True, exist_ok=True)
(out / "resources.yaml").write_text(json.dumps({"version": 1, "resources": resources}, ensure_ascii=False, indent=2), encoding="utf-8")
(out / "model-inventory.json").write_text(json.dumps(inventory, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps({"registered": len(resources), "training_names": len(inventory), "unpaired": sum(not x["gpt_count"] or not x["sovits_count"] for x in inventory)}, ensure_ascii=False))
