"""CPU-only native-layout fixtures; never import native code or model weights."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.native_gpu.adapters import NativeAdapter


class Device:
    def __init__(self, value):
        self.value = str(value)

    def __str__(self):
        return self.value


class Tensor:
    def __init__(self, device="cuda:0", *, dtype="float16", fail=False):
        self.device = Device(device)
        self.dtype = dtype
        self.fail = fail

    def to(self, *, device):
        if self.fail:
            raise RuntimeError("fixture migration failed")
        return Tensor(str(device), dtype=self.dtype)


class Module:
    def __init__(self):
        self._parameters = {}
        self._buffers = {}
        self._modules = {}

    def parameter(self, name, device="cuda:0"):
        tensor = Tensor(device)
        self._parameters[name] = tensor
        setattr(self, name, tensor)
        return tensor

    def child(self, name, module):
        self._modules[name] = module
        setattr(self, name, module)
        return module

    def parameters(self):
        for _, module in Module.named_modules(self):
            yield from module._parameters.values()

    def buffers(self):
        for _, module in Module.named_modules(self):
            yield from module._buffers.values()

    def named_modules(self):
        seen = set()

        def registered(module, prefix=""):
            if id(module) in seen:
                return
            seen.add(id(module))
            yield prefix, module
            for key, child in module._modules.items():
                yield from registered(child, f"{prefix}.{key}" if prefix else key)

        yield from registered(self)

    def to(self, *, device):
        # Replacing parameter objects exercises scripted alias rebinding.
        for _, module in Module.named_modules(self):
            for table in (module._parameters, module._buffers):
                for key, tensor in list(table.items()):
                    table[key] = Tensor.to(tensor, device=device)
                    setattr(module, key, table[key])
        return self


class ModuleList(Module):
    def __init__(self, modules):
        super().__init__()
        for i, module in enumerate(modules):
            self.child(str(i), module)

    def __len__(self):
        return len(self._modules)

    def __getitem__(self, key):
        return self._modules[str(key)]


class SV:
    def __init__(self):
        self.embedding_model = model()
        self.is_half = True


class AP_BWE:
    def __init__(self):
        self.model = model()
        self.device = "cuda:0"

    def to(self, device):
        Module.to(self.model, device=device)
        self.device = device
        return self


class Cuda:
    def __init__(self):
        self.synchronized = []
        self.empty_calls = 0

    def synchronize(self, device):
        self.synchronized.append(str(device))

    def empty_cache(self):
        self.empty_calls += 1


@pytest.fixture
def torch_fixture():
    return SimpleNamespace(Tensor=Tensor, device=Device, nn=SimpleNamespace(Module=Module), cuda=Cuda())


def model():
    value = Module()
    value.parameter("weight")
    return value


def linear():
    value = model()
    value.parameter("bias")
    return value


def t2s_model():
    t2s = Module()
    decoder = t2s.child("model", Module())
    for key in ("ar_text_position", "ar_audio_position"):
        decoder.child(key, Module()).pe = Tensor()
    layer = Module()
    attn = layer.child("self_attn", Module())
    attn.parameter("in_proj_weight")
    attn.parameter("in_proj_bias")
    attn.child("out_proj", linear())
    for key in ("norm1", "norm2", "linear1", "linear2"):
        layer.child(key, linear())
    h = decoder.child("h", Module())
    h.child("layers", ModuleList([layer]))
    block = SimpleNamespace(
        qkv_w=attn.in_proj_weight, qkv_b=attn.in_proj_bias,
        out_w=attn.out_proj.weight, out_b=attn.out_proj.bias,
        norm_w1=layer.norm1.weight, norm_b1=layer.norm1.bias,
        norm_w2=layer.norm2.weight, norm_b2=layer.norm2.bias,
        mlp=SimpleNamespace(
            w1=layer.linear1.weight, b1=layer.linear1.bias,
            w2=layer.linear2.weight, b2=layer.linear2.bias,
        ),
        false=Tensor("cpu", dtype="bool"),
    )
    decoder.t2s_transformer = SimpleNamespace(num_blocks=1, blocks=[block])
    return t2s


def audio_module():
    return SimpleNamespace(mel_basis={"cuda_key": Tensor()}, hann_window={"cuda_key": Tensor()})


def webui():
    return SimpleNamespace(
        device="cuda:0", model_version="v2ProPlus",
        bert_model=model(), ssl_model=model(), vq_model=model(), t2s_model=t2s_model(),
        hifigan_model=None, bigvgan_model=None, sv_cn_model=SV(), sr_model=AP_BWE(),
        resample_transform_dict={"old_cuda_key": model()},
        cache={"private reference text": Tensor()},
    )


class TTS:
    def __init__(self):
        self.configs = SimpleNamespace(device=Device("cuda:0"), version="v2ProPlus")
        self.t2s_model, self.vits_model = t2s_model(), model()
        self.bert_model, self.cnhuhbert_model = model(), model()
        self.vocoder = None
        self.sr_model, self.sv_model = AP_BWE(), SV()
        self.text_preprocessor = SimpleNamespace(bert_model=self.bert_model, device=Device("cuda:0"))
        self.prompt_cache = {
            "ref_audio_path": "private/reference.wav", "prompt_semantic": Tensor(),
            "refer_spec": [(Tensor(), Tensor())], "prompt_text": "private transcript",
            "prompt_lang": "zh", "phones": [1, 2], "bert_features": Tensor(),
            "norm_text": "private transcript", "aux_ref_audio_paths": [],
            "raw_audio": Tensor(), "raw_sr": 32000,
        }
        self.set_device_calls = []

    def set_device(self, device, save=True):
        self.set_device_calls.append((str(device), save))
        self.configs.device = device
        for key in ("t2s_model", "vits_model", "bert_model", "cnhuhbert_model"):
            Module.to(getattr(self, key), device=device)
        if self.sr_model is not None:
            self.sr_model.to(device)


class IndexTTS2:
    def __init__(self):
        self.device = "cuda:0"
        self.use_accel = self.use_torch_compile = False
        self.gpt = model()
        self.gpt.accel_engine = None
        inference = self.gpt.child("inference_model", model())
        inference.model_parallel = False
        inference.cached_mel_emb = Tensor()
        for key in ("semantic_model", "semantic_codec", "campplus_model", "bigvgan"):
            setattr(self, key, model())
        self.s2mel = model()
        models = self.s2mel.child("models", Module())
        cfm = models.child("cfm", Module())
        estimator = cfm.child("estimator", Module())
        transformer = estimator.child("transformer", model())
        transformer.use_kv_cache = False
        transformer.freqs_cis = Tensor()
        transformer.causal_mask = Tensor(dtype="bool")
        transformer.mask_cache = None
        self.qwen_emo = SimpleNamespace(model=model())
        self.qwen_emo.model.hf_device_map = {"": "cuda:0"}
        self.semantic_mean, self.semantic_std = Tensor(), Tensor()
        self.emo_matrix, self.spk_matrix = (Tensor(), Tensor()), (Tensor(),)
        for key in ("cache_spk_cond", "cache_s2mel_style", "cache_s2mel_prompt", "cache_emo_cond", "cache_mel"):
            setattr(self, key, Tensor())
        self.cache_spk_audio_prompt = "private/reference.wav"
        self.cache_emo_audio_prompt = "private/emotion.wav"


def adapter(engine, root, torch_fixture, *, shared=None):
    if shared is None:
        if engine == "gpt_webui":
            shared = {"gpt_mel_processing": audio_module()}
        elif engine == "gpt_api_v2":
            shared = {
                "gpt_mel_processing": audio_module(),
                "gpt_tts_module": SimpleNamespace(resample_transform_dict={"cuda_key": model()}),
            }
        else:
            shared = {"index_audio": audio_module()}
    return NativeAdapter(engine, root, torch_module=torch_fixture, shared_modules=shared)


def assert_cpu(result):
    assert result["supported"] is True
    assert result["verified"] is True
    assert result["residency"] == "cpu"
    assert set(result["devices"]) == {"cpu"}


def assert_gpu(result):
    assert result["verified"] is True
    assert result["residency"] == "gpu"


def test_gpt_webui_migrates_registered_models_script_aliases_and_caches(torch_fixture):
    ns = webui()
    a = adapter("gpt_webui", ns, torch_fixture)
    block = ns.t2s_model.model.t2s_transformer.blocks[0]
    old_alias = block.qkv_w
    assert_gpu(a.snapshot())
    assert_cpu(a.offload())
    assert str(ns.sv_cn_model.embedding_model.weight.device) == "cpu"
    assert str(ns.sr_model.model.weight.device) == "cpu"
    assert ns.sr_model.device == "cpu"
    assert block.qkv_w is ns.t2s_model.model.h.layers[0].self_attn.in_proj_weight
    assert block.qkv_w is not old_alias
    assert ns.resample_transform_dict == {}
    assert set(ns.cache) == {"private reference text"}
    assert str(ns.cache["private reference text"].device) == "cpu"
    assert a.shared_modules["gpt_mel_processing"].hann_window == {}
    assert_gpu(a.restore())
    assert ns.device == ns.sr_model.device == "cuda:0"
    assert str(block.false.device) == "cpu"
    assert str(ns.t2s_model.model.ar_audio_position.pe.device) == "cuda:0"
    assert str(ns.cache["private reference text"].device) == "cuda:0"
    assert torch_fixture.cuda.empty_calls == 2


def test_api_covers_omitted_native_fields_and_uses_save_false(torch_fixture):
    root = TTS()
    a = adapter("gpt_api_v2", SimpleNamespace(tts_pipeline=root), torch_fixture)
    assert_cpu(a.offload())
    assert root.set_device_calls == [("cpu", False)]
    assert str(root.text_preprocessor.device) == "cpu"
    assert str(root.sv_model.embedding_model.weight.device) == "cpu"
    assert str(root.prompt_cache["refer_spec"][0][1].device) == "cpu"
    assert root.prompt_cache["prompt_text"] == "private transcript"
    assert_gpu(a.restore())
    assert root.set_device_calls[-1] == ("cuda:0", False)
    assert str(root.prompt_cache["bert_features"].device) == "cuda:0"
    assert str(root.text_preprocessor.device) == "cuda:0"


def test_same_process_ui_and_api_are_independent_despite_shared_mel_cache(torch_fixture):
    ui, api = webui(), TTS()
    shared = audio_module()
    ui_adapter = adapter("gpt_webui", ui, torch_fixture, shared={"gpt_mel_processing": shared})
    api_adapter = adapter("gpt_api_v2", api, torch_fixture, shared={
        "gpt_mel_processing": shared,
        "gpt_tts_module": SimpleNamespace(resample_transform_dict={}),
    })
    assert_cpu(ui_adapter.offload())
    assert str(api.bert_model.weight.device) == "cuda:0"
    assert_gpu(api_adapter.snapshot())
    assert_cpu(api_adapter.offload())
    assert_gpu(api_adapter.restore())
    assert_cpu(ui_adapter.snapshot())


def test_index_covers_matrix_reference_inference_and_transformer_caches(torch_fixture):
    root = IndexTTS2()
    a = adapter("index_v2", SimpleNamespace(tts=root), torch_fixture)
    assert_cpu(a.offload())
    assert str(root.gpt.inference_model.cached_mel_emb.device) == "cpu"
    assert str(root.s2mel.models.cfm.estimator.transformer.causal_mask.device) == "cpu"
    assert str(root.emo_matrix[1].device) == "cpu"
    assert root.qwen_emo.model.hf_device_map == {"": "cpu"}
    assert root.cache_spk_audio_prompt == "private/reference.wav"
    assert_gpu(a.restore())
    assert root.qwen_emo.model.hf_device_map == {"": "cuda:0"}
    assert str(root.semantic_std.device) == "cuda:0"


def test_index_preserves_cpu_only_qwen_placement(torch_fixture):
    root = IndexTTS2()
    Module.to(root.qwen_emo.model, device=Device("cpu"))
    root.qwen_emo.model.hf_device_map = {"": "cpu"}
    a = adapter("index_v2", root, torch_fixture)
    assert_cpu(a.offload())
    assert_gpu(a.restore())
    assert str(root.qwen_emo.model.weight.device) == "cpu"
    assert root.qwen_emo.model.hf_device_map == {"": "cpu"}


@pytest.mark.parametrize("device_map", [
    {"embed": "cuda:0", "head": "cpu"}, {"": "disk"},
    {"": "cuda:1"}, {}, {"": object()},
])
def test_index_rejects_unsupported_qwen_device_map_without_moving_other_models(torch_fixture, device_map):
    root = IndexTTS2()
    root.qwen_emo.model.hf_device_map = device_map
    a = adapter("index_v2", root, torch_fixture)
    result = a.offload()
    assert result["verified"] is False
    assert result["residency"] == "unknown"
    assert str(root.semantic_model.weight.device) == "cuda:0"
    assert torch_fixture.cuda.empty_calls == 0


@pytest.mark.parametrize("hook_at_child", [False, True])
def test_index_rejects_accelerate_hooks_even_with_one_device_map(torch_fixture, hook_at_child):
    root = IndexTTS2()
    target = root.qwen_emo.model.child("dispatched", model()) if hook_at_child else root.qwen_emo.model
    target._hf_hook = object()
    result = adapter("index_v2", root, torch_fixture).offload()
    assert result["error"] == "accelerate_dispatch_hook_unsupported"
    assert str(root.gpt.weight.device) == "cuda:0"


@pytest.mark.parametrize("flag", ["use_accel", "use_torch_compile"])
def test_index_rejects_acceleration_modes(torch_fixture, flag):
    root = IndexTTS2()
    setattr(root, flag, True)
    assert adapter("index_v2", root, torch_fixture).offload()["error"] == "index_acceleration_unsupported"


def test_invalid_late_cache_field_is_preflighted_before_api_set_device(torch_fixture):
    root = TTS()
    root.prompt_cache["refer_spec"] = [object()]
    result = adapter("gpt_api_v2", root, torch_fixture).offload()
    assert result["verified"] is False
    assert root.set_device_calls == []
    assert str(root.bert_model.weight.device) == "cuda:0"


def test_missing_shared_cache_modules_and_unknown_keys_fail_closed(torch_fixture):
    root = webui()
    for shared in ({}, {"gpt_mel_processing": audio_module(), "arbitrary": object()}):
        result = adapter("gpt_webui", root, torch_fixture, shared=shared).offload()
        assert result["error"] == "required_shared_modules_missing_or_unknown"
        assert str(root.bert_model.weight.device) == "cuda:0"


def test_arbitrary_model_to_method_is_never_called(torch_fixture):
    calls = []
    root = webui()
    root.ssl_model = SimpleNamespace(to=lambda *args, **kwargs: calls.append(True))
    result = adapter("gpt_webui", root, torch_fixture).offload()
    assert result["error"] == "unsupported_model_type"
    assert calls == []
    assert str(root.bert_model.weight.device) == "cuda:0"


def test_unknown_cache_keys_and_gpt_versions_are_rejected(torch_fixture):
    root = TTS()
    root.prompt_cache["new_gpu_cache"] = Tensor()
    assert adapter("gpt_api_v2", root, torch_fixture).offload()["error"] == "unsupported_gpt_prompt_cache_layout"
    ns = webui()
    ns.model_version = "v4"
    assert adapter("gpt_webui", ns, torch_fixture).offload()["error"] == "unsupported_gpt_model_version"


def test_partial_transition_failure_keeps_gate_closed_and_explicit_restore_recovers(torch_fixture):
    root = webui()
    a = adapter("gpt_webui", root, torch_fixture)
    root.ssl_model.weight.fail = True
    result = a.offload()
    assert result["verified"] is False
    assert result["residency"] == "unknown"
    assert a.snapshot()["verified"] is False
    root.ssl_model.weight.fail = False
    assert_gpu(a.restore())


def test_verification_detects_unmoved_registered_tensor(torch_fixture):
    root = webui()
    original_to = Module.to

    def silently_skip_one(module, *, device):
        if module is root.ssl_model:
            return module
        return original_to(module, device=device)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Module, "to", silently_skip_one)
        result = adapter("gpt_webui", root, torch_fixture).offload()
    assert result["verified"] is False
    assert result["error"] == "residency_verification_failed"


def test_snapshot_does_not_expose_reference_audio_text_or_native_paths(torch_fixture):
    root = TTS()
    serialized = json.dumps(adapter("gpt_api_v2", root, torch_fixture).snapshot())
    assert "private" not in serialized
    assert "transcript" not in serialized
    assert "reference.wav" not in serialized


def test_known_device_changes_invalidate_the_adapter(torch_fixture):
    root = webui()
    a = adapter("gpt_webui", root, torch_fixture)
    assert_gpu(a.snapshot())
    root.device = "cuda:1"
    assert a.offload()["error"] == "engine_device_changed"


def test_other_gpu_tensor_is_rejected_before_any_model_moves(torch_fixture):
    root = webui()
    root.cache["other GPU"] = Tensor("cuda:1")
    result = adapter("gpt_webui", root, torch_fixture).offload()
    assert result["error"] == "multiple_gpu_devices_unsupported"
    assert str(root.bert_model.weight.device) == "cuda:0"


def test_real_torch_cpu_only_layout_never_calls_cuda(monkeypatch):
    torch = pytest.importorskip("torch")
    monkeypatch.setattr(torch.cuda, "synchronize", lambda *args: pytest.fail("CPU fixture touched CUDA"))
    monkeypatch.setattr(torch.cuda, "empty_cache", lambda: pytest.fail("CPU fixture touched CUDA"))

    t2s = torch.nn.Module()
    decoder = t2s.model = torch.nn.Module()
    for name in ("ar_text_position", "ar_audio_position"):
        position = torch.nn.Module()
        position.pe = torch.zeros(1, 2, device="cpu")
        setattr(decoder, name, position)
    layer = torch.nn.Module()
    attn = layer.self_attn = torch.nn.Module()
    attn.in_proj_weight = torch.nn.Parameter(torch.zeros(2, 2, device="cpu"))
    attn.in_proj_bias = torch.nn.Parameter(torch.zeros(2, device="cpu"))
    attn.out_proj = torch.nn.Linear(2, 2, device="cpu")
    for key in ("linear1", "linear2", "norm1", "norm2"):
        setattr(layer, key, torch.nn.Linear(2, 2, device="cpu"))
    decoder.h = torch.nn.Module()
    decoder.h.layers = torch.nn.ModuleList([layer])
    block = SimpleNamespace(
        qkv_w=attn.in_proj_weight, qkv_b=attn.in_proj_bias,
        out_w=attn.out_proj.weight, out_b=attn.out_proj.bias,
        norm_w1=layer.norm1.weight, norm_b1=layer.norm1.bias,
        norm_w2=layer.norm2.weight, norm_b2=layer.norm2.bias,
        mlp=SimpleNamespace(w1=layer.linear1.weight, b1=layer.linear1.bias,
                            w2=layer.linear2.weight, b2=layer.linear2.bias),
        false=torch.tensor(False, device="cpu"),
    )
    decoder.t2s_transformer = SimpleNamespace(num_blocks=1, blocks=[block])
    ns = SimpleNamespace(
        device="cpu", model_version="v2ProPlus", t2s_model=t2s,
        bert_model=torch.nn.Linear(2, 2, device="cpu"),
        ssl_model=torch.nn.Linear(2, 2, device="cpu"),
        vq_model=torch.nn.Linear(2, 2, device="cpu"),
        hifigan_model=None, bigvgan_model=None, sv_cn_model=None, sr_model=None,
        cache={"cached": torch.ones(1, device="cpu")}, resample_transform_dict={},
    )
    shared = {"gpt_mel_processing": SimpleNamespace(mel_basis={}, hann_window={})}
    a = NativeAdapter("gpt_webui", ns, torch_module=torch, shared_modules=shared)
    assert_cpu(a.offload())
    assert_cpu(a.restore())
    assert block.qkv_w is attn.in_proj_weight
