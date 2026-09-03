from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import soundfile


def test_save_audio_compat_registers_and_publishes_real_wav(
    monkeypatch,
    tmp_path: Path,
) -> None:
    def get_save_image_path(filename_prefix: str, output_dir: str):
        return output_dir, filename_prefix, 1, "", filename_prefix

    monkeypatch.setitem(
        sys.modules,
        "folder_paths",
        SimpleNamespace(
            get_output_directory=lambda: str(tmp_path),
            get_save_image_path=get_save_image_path,
        ),
    )

    module = importlib.import_module(
        "integrations.comfyui_nodes.tts_more_compat"
    )
    node_type = module.NODE_CLASS_MAPPINGS["SaveAudio"]
    result = node_type().save_audio(
        {
            "waveform": np.asarray([[[0.0, 0.25, -0.25, 0.0]]]),
            "sample_rate": 16000,
        },
        filename_prefix="tts_more_test",
    )

    assert node_type.INPUT_TYPES()["required"] == {
        "audio": ("AUDIO",),
        "filename_prefix": ("STRING", {"default": "ComfyUI"}),
    }
    assert node_type.RETURN_TYPES == ()
    assert node_type.OUTPUT_NODE is True
    assert result == {
        "ui": {
            "audio": [
                {
                    "filename": "tts_more_test_00001_.wav",
                    "subfolder": "",
                    "type": "output",
                }
            ]
        }
    }

    samples, sample_rate = soundfile.read(
        tmp_path / "tts_more_test_00001_.wav",
        always_2d=True,
    )
    assert sample_rate == 16000
    assert samples.shape == (4, 1)
    assert float(np.max(np.abs(samples))) > 0.2
