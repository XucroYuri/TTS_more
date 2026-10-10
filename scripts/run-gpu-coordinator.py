"""Validate or explicitly start the optional local native-priority coordinator."""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--serve", action="store_true", help="bind the reviewed local control port")
    args = parser.parse_args()
    try:
        package = Path(__file__).resolve().parents[1] / "backend" / "app" / "native_gpu"
        spec = importlib.util.spec_from_file_location("tts_more_native_gpu", package / "__init__.py", submodule_search_locations=[str(package)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        from tts_more_native_gpu.launcher import serve_coordinator, validate_coordinator_config
        config = validate_coordinator_config(args.config)
        print(json.dumps({"valid": True, "serve_requested": args.serve,
                          "groups": len(config["groups"]), "participants": len(config["participants"])}))
        if args.serve:
            serve_coordinator(config)
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception:
        # Configurations and native endpoints may contain private operator data.
        print(json.dumps({"valid": False, "error": "gpu_coordinator_failed"}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
