from __future__ import annotations

import subprocess


def test_noninteractive_subprocess_kwargs_is_empty_off_windows(monkeypatch) -> None:
    from app import subprocess_safety

    monkeypatch.setattr(subprocess_safety.sys, "platform", "linux")

    assert subprocess_safety.noninteractive_subprocess_kwargs() == {}


def test_noninteractive_subprocess_kwargs_preserves_error_mode_and_hides_window(monkeypatch) -> None:
    from app import subprocess_safety

    calls: list[int] = []

    class FakeFunction:
        argtypes = None
        restype = None

        def __init__(self, callback):
            self.callback = callback

        def __call__(self, *args):
            return self.callback(*args)

    class FakeKernel32:
        GetErrorMode = FakeFunction(lambda: 0x0010)
        SetErrorMode = FakeFunction(lambda mode: calls.append(int(mode)) or 0x0010)

    monkeypatch.setattr(subprocess_safety.sys, "platform", "win32")
    monkeypatch.setattr(subprocess_safety.ctypes, "WinDLL", lambda *_args, **_kwargs: FakeKernel32(), raising=False)
    monkeypatch.setattr(subprocess_safety.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)

    assert subprocess_safety.noninteractive_subprocess_kwargs() == {"creationflags": 0x08000000}
    assert calls == [0x0010 | 0x0001 | 0x0002]


def test_noninteractive_subprocess_kwargs_ignores_windows_api_failure(monkeypatch) -> None:
    from app import subprocess_safety

    monkeypatch.setattr(subprocess_safety.sys, "platform", "win32")
    monkeypatch.setattr(
        subprocess_safety.ctypes,
        "WinDLL",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("kernel32 unavailable")),
        raising=False,
    )
    monkeypatch.setattr(subprocess_safety.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)

    assert subprocess_safety.noninteractive_subprocess_kwargs() == {"creationflags": 0x08000000}
