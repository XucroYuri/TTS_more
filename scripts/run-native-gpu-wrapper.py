"""Validate an optional native launcher; --launch is an explicit deployment step."""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--launch", action="store_true", help="start native models using the reviewed wrapper")
    args = parser.parse_args()
    try:
        package = Path(__file__).resolve().parents[1] / "backend" / "app" / "native_gpu"
        spec = importlib.util.spec_from_file_location(
            "tts_more_native_gpu", package / "__init__.py", submodule_search_locations=[str(package)],
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        from tts_more_native_gpu.bootstrap import run_native, validate_config
        config = validate_config(args.config)
        # Validation output deliberately omits paths, args, environment and credentials.
        print(json.dumps({"valid": True, "kind": config["kind"], "launch_requested": args.launch}))
        if args.launch:
            run_native(config)
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception:
        print(json.dumps({"valid": False, "error": "native_wrapper_failed"}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
