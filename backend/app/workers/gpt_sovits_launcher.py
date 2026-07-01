from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path


def main() -> None:
    project_root = Path(__file__).resolve().parents[3]
    repo_dir = project_root / "repo" / "GPT-SoVITS"
    ffmpeg_bin = repo_dir / "ffmpeg-shared" / "bin"
    if ffmpeg_bin.is_dir():
        os.environ["PATH"] = str(ffmpeg_bin) + os.pathsep + os.environ.get("PATH", "")
        add_dll_directory = getattr(os, "add_dll_directory", None)
        if add_dll_directory is not None:
            add_dll_directory(str(ffmpeg_bin))

    os.chdir(repo_dir)
    sys.path.insert(0, str(repo_dir))
    sys.argv = [str(repo_dir / "api_v2.py"), *sys.argv[1:]]
    runpy.run_path(str(repo_dir / "api_v2.py"), run_name="__main__")


if __name__ == "__main__":
    main()
