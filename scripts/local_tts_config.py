"""Read operator-owned integration configuration; never publish local assets."""
import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_config(description: str, configure=None):
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument('--config', type=Path, default=ROOT / 'data/local/comfyui/source-config.json')
    if configure:
        configure(parser)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding='utf-8-sig'))
    if config.get('version') != 1:
        raise ValueError('Expected source-config version 1')
    return config, args


def integration_folder(config):
    return source_path(config.get('integration_dir', ROOT / 'data/local/comfyui'))


def source_path(value):
    """Interpret all operator paths relative to the workstation repository."""
    path = Path(value).expanduser()
    return (path if path.is_absolute() else ROOT / path).resolve()
