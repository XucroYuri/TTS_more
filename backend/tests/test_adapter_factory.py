from pathlib import Path

from app.adapter_factory import AdapterSettings, build_adapters
from app.adapters.gpt_sovits import GPTSoVITSHttpAdapter
from app.adapters.indextts import IndexTTSSubprocessAdapter
from app.adapters.mock import MockAdapter
from app.models import EngineName


def test_adapter_factory_uses_mock_adapters_by_default(tmp_path: Path) -> None:
    adapters = build_adapters(AdapterSettings(repo_root=tmp_path, mode="mock"))

    assert isinstance(adapters[EngineName.GPT_SOVITS], MockAdapter)
    assert isinstance(adapters[EngineName.INDEX_TTS], MockAdapter)
    assert EngineName.VIBEVOICE not in adapters


def test_adapter_factory_can_build_real_adapters(tmp_path: Path) -> None:
    adapters = build_adapters(
        AdapterSettings(
            repo_root=tmp_path,
            mode="real",
            gpt_sovits_base_url="http://127.0.0.1:9880",
            python_exe="python",
        )
    )

    assert isinstance(adapters[EngineName.GPT_SOVITS], GPTSoVITSHttpAdapter)
    assert isinstance(adapters[EngineName.INDEX_TTS], IndexTTSSubprocessAdapter)
    assert EngineName.VIBEVOICE not in adapters
