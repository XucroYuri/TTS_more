from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
import warnings
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PureWindowsPath

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
HELPERS = (
    REPO_ROOT / "scripts" / "portable-python.ps1",
    REPO_ROOT / "integrations" / "windows" / "portable-python.ps1",
)
PY311 = {
    "id": "cpython-3.11.9-embed-amd64",
    "urls": [
        "https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip",
        "https://download.qt.io/development_releases/prebuilt/python/python-3.11.9-embed-amd64.zip",
    ],
    "sha256": "009d6bf7e3b2ddca3d784fa09f90fe54336d5b60f0e0f305c37f400bf83cfd3b",
    "size_bytes": 11249023,
    "archive_entry": "python.exe",
}
PY310 = {
    "id": "cpython-3.10.11-embed-amd64",
    "urls": [
        "https://www.python.org/ftp/python/3.10.11/python-3.10.11-embed-amd64.zip",
        "https://repo.huaweicloud.com/python/3.10.11/python-3.10.11-embed-amd64.zip",
    ],
    "sha256": "608619f8619075629c9c69f361352a0da6ed7e62f83a0e19c63e0ea32eb7629d",
    "size_bytes": 8629277,
    "archive_entry": "python.exe",
}
UV = {
    "id": "uv-0.11.28-windows-x64",
    "urls": [
        "https://files.pythonhosted.org/packages/40/bc/d67b18cddd54c503c7bad2b189a47fd7a1d07ea10b9212624f892b985498/uv-0.11.28-py3-none-win_amd64.whl",
        "https://mirrors.aliyun.com/pypi/packages/40/bc/d67b18cddd54c503c7bad2b189a47fd7a1d07ea10b9212624f892b985498/uv-0.11.28-py3-none-win_amd64.whl",
    ],
    "sha256": "f4fcf2c8d9f1444b900e6b8dbbb828825fb76eca01acd18aeaa5c90240408cda",
    "size_bytes": 27603677,
    "archive_entry": "uv-0.11.28.data/scripts/uv.exe",
}


def _run_ps(helper: Path, body: str, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    command = f". '{helper}'; $ErrorActionPreference = 'Stop'; {body}"
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", command],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if check:
        assert result.returncode == 0, result.stdout + result.stderr
    return result


def _write_zip(path: Path, entries: list[tuple[str, bytes]]) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(path, "w") as archive:
            for name, payload in entries:
                archive.writestr(name, payload)


def _long_unicode_space_parent(tmp_path: Path, target_length: int = 141) -> Path:
    prefix = tmp_path / "含 中文 空格"
    padding = target_length - len(str(prefix)) - 1
    assert padding >= 4, (str(prefix), len(str(prefix)))
    parent = prefix / ("路" * padding)
    parent.mkdir(parents=True)
    assert len(str(parent)) == target_length
    return parent


def test_portable_python_owned_sibling_path_model_fits_real_four_pack_budget() -> None:
    stage = PureWindowsPath(
        r"I:\TTS-More-Full-Packages\.tmw-48168-e6189b21a4147d1b"
        r"\tts-more-controller-48168-67c31e1c7fb7\TTS-More-0.2.0-windows-x64-full-staging"
    )
    runtime_parent = stage / "runtime"
    nonce = "0123456789abcdef0123456789abcdef"
    destination = runtime_parent / "live"
    old_install = runtime_parent / f".{destination.name}.install-{nonce}"
    old_extract = runtime_parent / f".{old_install.name}.extract-{nonce}" / "Lib" / "site-packages"
    uv_destination = stage / "data" / "cache" / "portable" / "tools" / "uv-0.11.28" / "uv.exe"
    old_uv_temporary = PureWindowsPath(f"{uv_destination}.partial-{nonce}")
    old_uv_backup = PureWindowsPath(f"{uv_destination}.backup-{nonce}")
    assert {
        "install": len(str(old_install)),
        "extract": len(str(old_extract)),
        "uv_temporary": len(str(old_uv_temporary)),
        "uv_backup": len(str(old_uv_backup)),
    } == {"install": 187, "extract": 247, "uv_temporary": 217, "uv_backup": 216}

    intended = {
        "install": runtime_parent / f".pi-{nonce}",
        "extract": runtime_parent / f".px-{nonce}" / "Lib" / "site-packages",
        "uv_temporary": uv_destination.parent / f".pu-{nonce}",
        "uv_backup": uv_destination.parent / f".pb-{nonce}",
    }
    assert {name: len(str(path)) for name, path in intended.items()} == {
        "install": 177,
        "extract": 195,
        "uv_temporary": 206,
        "uv_backup": 206,
    }
    assert max(len(str(path)) for path in intended.values()) <= 240

    for helper in HELPERS:
        source = helper.read_text(encoding="utf-8")
        assert "function New-PortableOwnedSiblingPath" in source
        for prefix in ("px", "pi", "pu", "pb"):
            assert f"'{prefix}'" in source
        assert 'GetFileName($Destination) + ".extract-"' not in source
        assert 'GetFileName($Destination) + ".install-"' not in source
        assert '"$Destination.partial-' not in source
        assert '"$Destination.backup-' not in source


@pytest.mark.parametrize("helper", HELPERS, ids=lambda path: str(path.relative_to(REPO_ROOT)))
def test_python_zip_expands_on_long_unicode_space_runtime_parent(helper: Path, tmp_path: Path) -> None:
    archive = tmp_path / "python.zip"
    parent = _long_unicode_space_parent(tmp_path)
    destination = parent / f".live.install-{'a' * 32}"
    sentinel = parent / "existing sentinel.txt"
    sentinel.write_bytes(b"must remain byte-identical")
    _write_zip(
        archive,
        [("python.exe", b"stub"), ("python311.zip", b"stdlib"), ("python311._pth", b"old")],
    )

    result = _run_ps(
        helper,
        f"Expand-PortablePythonArchive -Archive '{archive}' -Destination '{destination}' -ExpectedVersion '3.11.9'",
        check=False,
    )

    assert sentinel.read_bytes() == b"must remain byte-identical"
    assert not list(parent.glob(".*.extract-*"))
    assert not list(parent.glob(".px-*"))
    assert result.returncode == 0, result.stdout + result.stderr
    assert (destination / "python.exe").read_bytes() == b"stub"
    assert (destination / "Lib" / "site-packages").is_dir()


def test_runtime_locks_pin_exact_embeddable_python_and_uv() -> None:
    expected = {
        "packaging/portable/runtime.lock.json": ("3.11.9", PY311),
        "integrations/components/gpt-sovits/runtime.lock.json": ("3.11.9", PY311),
        "integrations/components/indextts/runtime.lock.json": ("3.11.9", PY311),
        "integrations/components/cosyvoice/runtime.lock.json": ("3.10.11", PY310),
    }
    for relative, (version, python_asset) in expected.items():
        lock = json.loads((REPO_ROOT / relative).read_text(encoding="utf-8"))
        assert lock["python_version"] == version
        assert lock["assets"]["python"] == python_asset
        assert lock["assets"]["uv"] == UV
        assert "version" not in lock["assets"]["python"]
        assert "version" not in lock["assets"]["uv"]


@pytest.mark.parametrize("helper", HELPERS, ids=lambda path: str(path.relative_to(REPO_ROOT)))
def test_powershell_downloader_requests_binary_github_release_asset(helper: Path) -> None:
    result = _run_ps(
        helper,
        "$request = New-Object System.Net.Http.HttpRequestMessage([System.Net.Http.HttpMethod]::Get, "
        "'https://api.github.com/repos/GyanD/codexffmpeg/releases/assets/459521355'); "
        "Set-PortableDownloadHeaders -Request $request -Url ([string]$request.RequestUri); "
        "[Console]::WriteLine(([string]$request.Headers.Accept)); "
        "[Console]::WriteLine(([string]$request.Headers.UserAgent))",
    )

    assert "application/octet-stream" in result.stdout
    assert "tts-more-portable-installer" in result.stdout


def test_component_sources_and_generator_use_exact_patch_versions() -> None:
    expected = {"gpt-sovits": "3.11.9", "indextts": "3.11.9", "cosyvoice": "3.10.11"}
    for component, version in expected.items():
        source = json.loads(
            (REPO_ROOT / "integrations" / "components" / component / "component-source.json").read_text(
                encoding="utf-8"
            )
        )
        assert source["python"] == version
    sync_text = (REPO_ROOT / "scripts" / "sync_integrations.py").read_text(encoding="utf-8")
    for version in expected.values():
        assert f'"python": "{version}"' in sync_text


def test_sync_generator_controls_portable_python_helper() -> None:
    sync_text = (REPO_ROOT / "scripts" / "sync_integrations.py").read_text(encoding="utf-8")
    windows_files = sync_text.split('for name in (\n        "Initialize.ps1",', maxsplit=1)[1].split(
        "):\n        _copy_file(source_root / \"integrations\" / \"windows\"", maxsplit=1
    )[0]
    assert '"portable-python.ps1"' in windows_files


def test_portable_python_helper_contract_and_no_drift() -> None:
    controller, integration = (path.read_text(encoding="utf-8") for path in HELPERS)
    assert controller == integration
    assert "function Install-PortablePythonRuntime" in controller
    assert "System.IO.Compression" in controller
    assert "portable_install.py" in controller
    assert "ensure-asset" in controller
    assert "datetime.UTC" not in controller
    assert "sys.path.insert(0,os.path.dirname(script))" in controller
    assert "runpy.run_path" in controller
    assert "& $candidatePython -c $portableInstallBootstrap @arguments 2>&1 | Out-Host" in controller
    assert "$script:DefaultUvArchiveEntry" not in controller
    assert "uv archive_entry is required" in controller
    assert "Lib\\site-packages" in controller
    assert "import site" in controller
    assert "pyvenv.cfg" in controller
    assert ".partial" in controller
    assert 'Range = "bytes=$resumeFrom-"' in controller
    forbidden = ("bootstrap-conda", "conda create", "Get-Command python", "Get-Command uv", "tar.exe", "7z")
    assert not any(token.casefold() in controller.casefold() for token in forbidden)


@pytest.mark.parametrize("helper", HELPERS, ids=lambda path: str(path.relative_to(REPO_ROOT)))
@pytest.mark.parametrize(
    "entry",
    (
        "../escape.txt",
        "/absolute.txt",
        "C:/drive.txt",
        "safe/../../escape.txt",
        "safe:stream/file.txt",
        "safe/file.txt:stream",
        "pyvenv.cfg",
    ),
)
def test_python_zip_rejects_unsafe_entries(helper: Path, entry: str, tmp_path: Path) -> None:
    archive = tmp_path / "python.zip"
    destination = tmp_path / "runtime"
    _write_zip(archive, [("python.exe", b"stub"), ("python311._pth", b"old"), (entry, b"bad")])
    result = _run_ps(
        helper,
        f"Expand-PortablePythonArchive -Archive '{archive}' -Destination '{destination}' -ExpectedVersion '3.11.9'",
        check=False,
    )
    assert result.returncode != 0
    assert not destination.exists()


@pytest.mark.parametrize("helper", HELPERS, ids=lambda path: str(path.relative_to(REPO_ROOT)))
@pytest.mark.parametrize("entry", ("safe:stream/file.txt", "safe/file.txt:stream"))
def test_python_zip_explicitly_rejects_ntfs_ads_segments(helper: Path, entry: str, tmp_path: Path) -> None:
    archive = tmp_path / "python.zip"
    destination = tmp_path / "runtime"
    _write_zip(
        archive,
        [("python.exe", b"stub"), ("python311.zip", b"stdlib"), ("python311._pth", b"old"), (entry, b"bad")],
    )
    result = _run_ps(
        helper,
        f"Expand-PortablePythonArchive -Archive '{archive}' -Destination '{destination}' -ExpectedVersion '3.11.9'",
        check=False,
    )
    assert result.returncode != 0
    assert "path segment contains a colon" in result.stderr
    assert not destination.exists()


@pytest.mark.parametrize("helper", HELPERS, ids=lambda path: str(path.relative_to(REPO_ROOT)))
@pytest.mark.parametrize(
    "entries",
    (
        [("python.exe", b"stub")],
        [("python.exe", b"stub"), ("python311._pth", b"a"), ("other._pth", b"b")],
        [("python.exe", b"stub"), ("python310._pth", b"wrong")],
        [("python.exe", b"one"), ("PYTHON.EXE", b"two"), ("python311._pth", b"old")],
    ),
)
def test_python_zip_rejects_missing_unexpected_or_duplicate_layouts(
    helper: Path, entries: list[tuple[str, bytes]], tmp_path: Path
) -> None:
    archive = tmp_path / "python.zip"
    destination = tmp_path / "runtime"
    _write_zip(archive, entries)
    result = _run_ps(
        helper,
        f"Expand-PortablePythonArchive -Archive '{archive}' -Destination '{destination}' -ExpectedVersion '3.11.9'",
        check=False,
    )
    assert result.returncode != 0
    assert not destination.exists()


@pytest.mark.parametrize("helper", HELPERS, ids=lambda path: str(path.relative_to(REPO_ROOT)))
def test_python_zip_writes_exact_single_pth(helper: Path, tmp_path: Path) -> None:
    archive = tmp_path / "python.zip"
    destination = tmp_path / "runtime"
    _write_zip(
        archive,
        [("python.exe", b"stub"), ("python311.zip", b"stdlib"), ("python311._pth", b"python311.zip\r\n.\r\n")],
    )
    _run_ps(
        helper,
        f"Expand-PortablePythonArchive -Archive '{archive}' -Destination '{destination}' -ExpectedVersion '3.11.9'",
    )
    pth_files = list(destination.glob("*._pth"))
    assert [path.name for path in pth_files] == ["python311._pth"]
    effective = [line.strip() for line in pth_files[0].read_text(encoding="ascii").splitlines() if line.strip()]
    assert effective == ["python311.zip", ".", "Lib\\site-packages", "import site"]
    assert (destination / "Lib" / "site-packages").is_dir()


@pytest.mark.parametrize("helper", HELPERS, ids=lambda path: str(path.relative_to(REPO_ROOT)))
def test_python_zip_rejects_reparse_point_entries(helper: Path, tmp_path: Path) -> None:
    archive = tmp_path / "python.zip"
    destination = tmp_path / "runtime"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("python.exe", b"stub")
        bundle.writestr("python311.zip", b"stdlib")
        bundle.writestr("python311._pth", b"old")
        reparse = zipfile.ZipInfo("reparse-target")
        reparse.create_system = 0
        reparse.external_attr = 0x400
        bundle.writestr(reparse, b"target")
    result = _run_ps(
        helper,
        f"Expand-PortablePythonArchive -Archive '{archive}' -Destination '{destination}' -ExpectedVersion '3.11.9'",
        check=False,
    )
    assert result.returncode != 0
    assert "reparse-point entry" in result.stderr
    assert not destination.exists()


@pytest.mark.parametrize("helper", HELPERS, ids=lambda path: str(path.relative_to(REPO_ROOT)))
@pytest.mark.parametrize("count", (0, 2))
def test_uv_wheel_requires_exactly_one_declared_entry(helper: Path, count: int, tmp_path: Path) -> None:
    wheel = tmp_path / "uv.whl"
    destination = tmp_path / "tools" / "uv.exe"
    entries = [(UV["archive_entry"], b"uv") for _ in range(count)]
    if not entries:
        entries = [("wrong/scripts/uv.exe", b"uv")]
    _write_zip(wheel, entries)
    result = _run_ps(
        helper,
        f"Export-PortableUvExecutable -Wheel '{wheel}' -ArchiveEntry '{UV['archive_entry']}' -Destination '{destination}'",
        check=False,
    )
    assert result.returncode != 0
    assert not destination.exists()


@pytest.mark.parametrize("helper", HELPERS, ids=lambda path: str(path.relative_to(REPO_ROOT)))
def test_uv_export_atomically_replaces_existing_destination_from_declared_entry(
    helper: Path, tmp_path: Path
) -> None:
    wheel = tmp_path / "uv.whl"
    destination = tmp_path / "tools" / "uv.exe"
    expected = b"verified-uv-from-wheel"
    _write_zip(wheel, [(UV["archive_entry"], expected)])
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"untrusted-existing-uv")

    _run_ps(
        helper,
        f"Export-PortableUvExecutable -Wheel '{wheel}' -ArchiveEntry '{UV['archive_entry']}' "
        f"-Destination '{destination}'",
    )

    assert destination.read_bytes() == expected
    assert not list(destination.parent.glob("uv.exe.partial-*"))


@pytest.mark.parametrize("helper", HELPERS, ids=lambda path: str(path.relative_to(REPO_ROOT)))
def test_python_downloader_resumes_at_eight_bytes_and_falls_back(helper: Path, tmp_path: Path) -> None:
    payload = b"portable-python-asset"
    ranges: list[str | None] = []
    paths: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            paths.append(self.path)
            ranges.append(self.headers.get("Range"))
            if self.path == "/first":
                self.send_response(503)
                self.end_headers()
                return
            start = 8 if self.headers.get("Range") == "bytes=8-" else 0
            self.send_response(206 if start else 200)
            self.send_header("Content-Length", str(len(payload) - start))
            if start:
                self.send_header("Content-Range", f"bytes {start}-{len(payload) - 1}/{len(payload)}")
            self.end_headers()
            self.wfile.write(payload[start:])

        def log_message(self, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        destination = tmp_path / "asset.zip"
        partial = Path(f"{destination}.partial")
        partial.write_bytes(payload[:8])
        lock = tmp_path / "asset.json"
        lock.write_text(
            json.dumps(
                {
                    "id": "fixture",
                    "urls": [
                        f"http://127.0.0.1:{server.server_port}/first",
                        f"http://127.0.0.1:{server.server_port}/second",
                    ],
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "size_bytes": len(payload),
                }
            ),
            encoding="utf-8",
        )
        _run_ps(
            helper,
            f"$asset = Get-Content -Raw '{lock}' | ConvertFrom-Json; "
            f"Get-PortableLockedAsset -Asset $asset -Destination '{destination}'",
        )
        assert destination.read_bytes() == payload
        assert paths[:2] == ["/first", "/second"]
        assert ranges[:2] == ["bytes=8-", "bytes=8-"]
        assert not partial.exists()
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.mark.parametrize("helper", HELPERS, ids=lambda path: str(path.relative_to(REPO_ROOT)))
def test_downloader_rolls_failed_mirror_back_to_partial_baseline(helper: Path, tmp_path: Path) -> None:
    payload = b"portable-python-asset"
    corrupt = b"X" * (len(payload) - 8)
    ranges: list[tuple[str, str | None]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            request_range = self.headers.get("Range")
            ranges.append((self.path, request_range))
            if self.path == "/first":
                body = corrupt
                self.send_response(206)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Range", f"bytes 8-{len(payload) - 1}/{len(payload)}")
            else:
                body = payload[8:]
                self.send_response(206)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Range", f"bytes 8-{len(payload) - 1}/{len(payload)}")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        destination = tmp_path / "asset.zip"
        partial = Path(f"{destination}.partial")
        partial.write_bytes(payload[:8])
        lock = tmp_path / "asset.json"
        lock.write_text(
            json.dumps(
                {
                    "id": "fixture",
                    "urls": [
                        f"http://127.0.0.1:{server.server_port}/first",
                        f"http://127.0.0.1:{server.server_port}/second",
                    ],
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "size_bytes": len(payload),
                }
            ),
            encoding="utf-8",
        )
        _run_ps(
            helper,
            f"$asset = Get-Content -Raw '{lock}' | ConvertFrom-Json; "
            f"Get-PortableLockedAsset -Asset $asset -Destination '{destination}'",
        )
        assert destination.read_bytes() == payload
        assert ranges == [("/first", "bytes=8-"), ("/second", "bytes=8-")]
        assert not partial.exists()
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.mark.parametrize("helper", HELPERS, ids=lambda path: str(path.relative_to(REPO_ROOT)))
def test_downloader_publishes_valid_equal_length_partial_without_network(helper: Path, tmp_path: Path) -> None:
    payload = b"complete-verified-partial"
    destination = tmp_path / "asset.zip"
    partial = Path(f"{destination}.partial")
    partial.write_bytes(payload)
    lock = tmp_path / "asset.json"
    lock.write_text(
        json.dumps(
            {
                "id": "fixture",
                "urls": ["http://127.0.0.1:1/network-must-not-run"],
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size_bytes": len(payload),
            }
        ),
        encoding="utf-8",
    )

    _run_ps(
        helper,
        f"$asset = Get-Content -Raw '{lock}' | ConvertFrom-Json; "
        f"Get-PortableLockedAsset -Asset $asset -Destination '{destination}'",
    )

    assert destination.read_bytes() == payload
    assert not partial.exists()


@pytest.mark.parametrize("helper", HELPERS, ids=lambda path: str(path.relative_to(REPO_ROOT)))
def test_downloader_restarts_corrupt_equal_length_partial_from_zero(helper: Path, tmp_path: Path) -> None:
    payload = b"complete-verified-partial"
    ranges: list[str | None] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            ranges.append(self.headers.get("Range"))
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        destination = tmp_path / "asset.zip"
        partial = Path(f"{destination}.partial")
        partial.write_bytes(b"X" * len(payload))
        lock = tmp_path / "asset.json"
        lock.write_text(
            json.dumps(
                {
                    "id": "fixture",
                    "urls": [f"http://127.0.0.1:{server.server_port}/asset"],
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "size_bytes": len(payload),
                }
            ),
            encoding="utf-8",
        )

        _run_ps(
            helper,
            f"$asset = Get-Content -Raw '{lock}' | ConvertFrom-Json; "
            f"Get-PortableLockedAsset -Asset $asset -Destination '{destination}'",
        )

        assert destination.read_bytes() == payload
        assert ranges == [None]
        assert not partial.exists()
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.mark.parametrize("helper", HELPERS, ids=lambda path: str(path.relative_to(REPO_ROOT)))
@pytest.mark.parametrize(
    ("content_range", "body"),
    (
        ("bytes 0-12/21", b"bad-response"),
        ("bytes 8-20/22", b"bad-response"),
    ),
)
def test_downloader_rejects_invalid_resumed_content_range_without_mutating_baseline(
    helper: Path, content_range: str, body: bytes, tmp_path: Path
) -> None:
    payload = b"portable-python-asset"

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(206)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Range", content_range)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        destination = tmp_path / "asset.zip"
        partial = Path(f"{destination}.partial")
        baseline = payload[:8]
        partial.write_bytes(baseline)
        lock = tmp_path / "asset.json"
        lock.write_text(
            json.dumps(
                {
                    "id": "fixture",
                    "urls": [f"http://127.0.0.1:{server.server_port}/invalid-range"],
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "size_bytes": len(payload),
                }
            ),
            encoding="utf-8",
        )
        result = _run_ps(
            helper,
            f"$asset = Get-Content -Raw '{lock}' | ConvertFrom-Json; "
            f"Get-PortableLockedAsset -Asset $asset -Destination '{destination}'",
            check=False,
        )
        assert result.returncode != 0
        assert "Content-Range" in result.stderr
        assert partial.read_bytes() == baseline
        assert not destination.exists()
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.mark.parametrize("helper", HELPERS, ids=lambda path: str(path.relative_to(REPO_ROOT)))
def test_helper_parses_in_windows_powershell_51(helper: Path) -> None:
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            f"$e=$null; [void][Management.Automation.Language.Parser]::ParseFile('{helper}',[ref]$null,[ref]$e); if($e.Count){{ $e | % Message; exit 1 }}",
        ],
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
