"""Explicit in-process device adapters for the supported native TTS layouts.

This module does not import a native application, attach to another process,
or expose Python objects over HTTP. The external launcher supplies objects it
has already loaded and holds its lifecycle lock before calling these methods.
Only the fields below and standard torch Module/Tensor operations are used.
An unfamiliar layout is a closed admission gate, not an invitation to inspect
arbitrary objects. Process-wide CUDA allocator verification belongs to the
guard after *all* adapters in that process have finished offloading.
"""

from __future__ import annotations

import gc
import importlib
from dataclasses import dataclass, field
from typing import Any


class AdapterUnsupported(ValueError):
    """A layout cannot be safely migrated by the explicit supported profile."""


_GPT_VERSIONS = {"v1", "v2", "v2Pro", "v2ProPlus"}
_SHARED_KEYS = {
    "gpt_webui": {"gpt_mel_processing"},
    "gpt_api_v2": {"gpt_mel_processing", "gpt_tts_module"},
    "index_v2": {"index_audio"},
}
_PROMPT_KEYS = {
    "ref_audio_path", "prompt_semantic", "refer_spec", "prompt_text",
    "prompt_lang", "phones", "bert_features", "norm_text",
    "aux_ref_audio_paths", "raw_audio", "raw_sr",
}
_BLOCK_ALIASES = {
    "qkv_w": ("self_attn", "in_proj_weight"),
    "qkv_b": ("self_attn", "in_proj_bias"),
    "out_w": ("self_attn", "out_proj", "weight"),
    "out_b": ("self_attn", "out_proj", "bias"),
    "norm_w1": ("norm1", "weight"), "norm_b1": ("norm1", "bias"),
    "norm_w2": ("norm2", "weight"), "norm_b2": ("norm2", "bias"),
}
_MLP_ALIASES = {
    "w1": ("linear1", "weight"), "b1": ("linear1", "bias"),
    "w2": ("linear2", "weight"), "b2": ("linear2", "bias"),
}


def _read(owner: Any, key: str) -> Any:
    if type(owner) is dict:
        if key not in owner:
            raise AdapterUnsupported(f"missing_field:{key}")
        return owner[key]
    try:
        return getattr(owner, key)
    except AttributeError as exc:
        raise AdapterUnsupported(f"missing_field:{key}") from exc


def _write(owner: Any, key: str, value: Any) -> None:
    if type(owner) is dict:
        owner[key] = value
    else:
        setattr(owner, key, value)


def _optional(owner: Any, key: str, default: Any = None) -> Any:
    try:
        return _read(owner, key)
    except AdapterUnsupported:
        return default


def _path(owner: Any, names: tuple[str, ...]) -> Any:
    for key in names:
        owner = _read(owner, key)
    return owner


@dataclass
class _Slot:
    owner: Any
    key: str
    name: str
    kind: str
    restore_device: str

    def value(self) -> Any:
        return _read(self.owner, self.key)


@dataclass
class _Alias:
    owner: Any
    key: str
    layer: Any
    source: tuple[str, ...]
    name: str


@dataclass
class _Inventory:
    models: list[_Slot] = field(default_factory=list)
    tensors: list[_Slot] = field(default_factory=list)
    clear_caches: list[_Slot] = field(default_factory=list)
    aliases: list[_Alias] = field(default_factory=list)
    devices: list[tuple[Any, str, str]] = field(default_factory=list)
    device_maps: list[Any] = field(default_factory=list)


class NativeAdapter:
    """Migrate a known native engine while its owning guard holds the lock.

    ``shared_modules`` contains already-loaded modules with these fixed keys:
    ``gpt_mel_processing`` (module.mel_processing), ``gpt_tts_module``
    (TTS_infer_pack.TTS), or ``index_audio`` (indextts.s2mel.modules.audio).
    No module is discovered or imported using a user-provided name.

    GPT WebUI and API models are independent. Create separate adapters even
    when they share a process and the same mel-processing module. An adapter's
    ``verified`` result proves its explicit fields' residency; it does not
    prove that another adapter or unrelated CUDA owner has freed its memory.
    """

    def __init__(
        self,
        engine: str,
        namespace: Any,
        *,
        torch_module: Any | None = None,
        shared_modules: dict[str, Any] | None = None,
    ) -> None:
        self.engine = engine
        self.namespace = namespace
        self.torch = torch_module
        self.shared_modules = shared_modules if shared_modules is not None else {}
        self._original_device: str | None = None
        self._qwen_device: str | None = None
        self._last_error: str | None = None
        self._failed = False
        if self.torch is None:
            try:
                self.torch = importlib.import_module("torch")
            except ImportError:
                self._last_error = "torch_unavailable"

    def _tensor(self, value: Any) -> bool:
        return isinstance(value, self.torch.Tensor)

    def _module(self, value: Any) -> bool:
        return isinstance(value, self.torch.nn.Module)

    def _device(self, value: Any) -> str:
        if not isinstance(value, (str, self.torch.device)):
            raise AdapterUnsupported("unsupported_device_type")
        device = str(value)
        if device == "cpu":
            return device
        if device == "cuda":
            return "cuda:0"
        if device.startswith("cuda:") and device[5:].isdigit():
            return device
        raise AdapterUnsupported("unsupported_device")

    def _assert_tensor_tree(self, value: Any, depth: int = 0) -> None:
        if value is None or self._tensor(value):
            return
        if depth < 3 and type(value) in (tuple, list) and len(value) <= 2048:
            for item in value:
                self._assert_tensor_tree(item, depth + 1)
            return
        raise AdapterUnsupported("unsupported_tensor_cache_layout")

    def _tensor_leaves(self, value: Any):
        if self._tensor(value):
            yield value
        elif type(value) in (tuple, list):
            for item in value:
                yield from self._tensor_leaves(item)

    def _module_tensors(self, model: Any):
        # The standard Module API traverses registered torch children, not
        # arbitrary Python attributes or unknown application objects.
        yield from self.torch.nn.Module.parameters(model)
        yield from self.torch.nn.Module.buffers(model)

    def _check_model(self, model: Any) -> None:
        if not self._module(model):
            raise AdapterUnsupported("unsupported_model_type")
        for _, child in self.torch.nn.Module.named_modules(model):
            if _optional(child, "_hf_hook") is not None:
                raise AdapterUnsupported("accelerate_dispatch_hook_unsupported")
            if _optional(child, "_orig_mod") is not None:
                raise AdapterUnsupported("compiled_model_unsupported")
            if (
                _optional(child, "is_loaded_in_4bit", False)
                or _optional(child, "is_loaded_in_8bit", False)
                or _optional(child, "hf_quantizer") is not None
            ):
                raise AdapterUnsupported("quantized_model_unsupported")

    def _add_model(
        self, inv: _Inventory, owner: Any, key: str, name: str,
        *, optional: bool = False, restore_device: str | None = None,
    ) -> None:
        model = _read(owner, key)
        if model is None and optional:
            return
        self._check_model(model)
        inv.models.append(_Slot(owner, key, name, "model", restore_device or self._original_device or "cpu"))

    def _add_tensor(self, inv: _Inventory, owner: Any, key: str, name: str) -> None:
        self._assert_tensor_tree(_read(owner, key))
        inv.tensors.append(_Slot(owner, key, name, "tensor", self._original_device or "cpu"))

    def _add_clear_cache(
        self, inv: _Inventory, owner: Any, key: str, name: str, *, modules: bool = False,
    ) -> None:
        cache = _read(owner, key)
        if type(cache) is not dict:
            raise AdapterUnsupported("unsupported_shared_cache_layout")
        for cache_key, value in cache.items():
            if not isinstance(cache_key, (str, int)):
                raise AdapterUnsupported("unsupported_shared_cache_key")
            if modules:
                self._check_model(value)
            elif not self._tensor(value):
                raise AdapterUnsupported("unsupported_shared_cache_value")
        inv.clear_caches.append(_Slot(owner, key, name, "module_cache" if modules else "tensor_cache", "cpu"))

    def _add_semantic_cache(self, inv: _Inventory, owner: Any) -> None:
        cache = _read(owner, "cache")
        if type(cache) is not dict or any(
            not isinstance(key, (str, int)) or not self._tensor(value)
            for key, value in cache.items()
        ):
            raise AdapterUnsupported("unsupported_gpt_semantic_cache_layout")
        # The WebUI's freeze switch reuses this semantic cache. Preserve the
        # values on CPU instead of silently invalidating the user's freeze.
        inv.tensors.append(_Slot(owner, "cache", "semantic_cache", "tensor_map", self._original_device or "cpu"))

    def _add_audio_caches(self, inv: _Inventory, key: str) -> None:
        module = self.shared_modules[key]
        self._add_clear_cache(inv, module, "mel_basis", f"{key}.mel_basis")
        self._add_clear_cache(inv, module, "hann_window", f"{key}.hann_window")

    def _add_ap_bwe(self, inv: _Inventory, owner: Any, key: str) -> None:
        wrapper = _read(owner, key)
        if wrapper is None:
            return
        if type(wrapper).__name__ != "AP_BWE":
            raise AdapterUnsupported("unsupported_super_resolution_wrapper")
        self._add_model(inv, wrapper, "model", f"{key}.model")
        self._device(_read(wrapper, "device"))
        inv.devices.append((wrapper, "device", self._original_device or "cpu"))

    def _add_sv(self, inv: _Inventory, owner: Any, key: str) -> None:
        wrapper = _read(owner, key)
        if wrapper is None:
            return
        if type(wrapper).__name__ != "SV":
            raise AdapterUnsupported("unsupported_speaker_embedding_wrapper")
        self._add_model(inv, wrapper, "embedding_model", f"{key}.embedding_model")

    def _add_t2s_nonregistered(self, inv: _Inventory, t2s: Any) -> None:
        decoder = _read(t2s, "model")
        for key in ("ar_text_position", "ar_audio_position"):
            self._add_tensor(inv, _read(decoder, key), "pe", f"t2s.{key}.pe")
        fast = _read(decoder, "t2s_transformer")
        blocks = _read(fast, "blocks")
        layers = _read(_read(decoder, "h"), "layers")
        count = _read(fast, "num_blocks")
        if type(count) is not int or count < 1 or count > 256 or len(blocks) != count or len(layers) != count:
            raise AdapterUnsupported("unsupported_gpt_fast_transformer_layout")
        for i in range(count):
            block, layer = blocks[i], layers[i]
            for target, source in _BLOCK_ALIASES.items():
                if not self._tensor(_read(block, target)) or not self._tensor(_path(layer, source)):
                    raise AdapterUnsupported("unsupported_gpt_weight_alias")
                inv.aliases.append(_Alias(block, target, layer, source, f"t2s.blocks.{i}.{target}"))
            mlp = _read(block, "mlp")
            for target, source in _MLP_ALIASES.items():
                if not self._tensor(_read(mlp, target)) or not self._tensor(_path(layer, source)):
                    raise AdapterUnsupported("unsupported_gpt_weight_alias")
                inv.aliases.append(_Alias(mlp, target, layer, source, f"t2s.blocks.{i}.mlp.{target}"))
            # This scripted constant is originally a CPU bool tensor. It must
            # stay on CPU rather than being restored as a model weight.
            constant = _read(block, "false")
            if not self._tensor(constant) or self._device(constant.device) != "cpu":
                raise AdapterUnsupported("unsupported_gpt_script_constant")

    def _gpt_webui(self, inv: _Inventory) -> None:
        ns = self.namespace
        if _read(ns, "model_version") not in _GPT_VERSIONS:
            raise AdapterUnsupported("unsupported_gpt_model_version")
        for key in ("bert_model", "ssl_model", "vq_model", "t2s_model"):
            self._add_model(inv, ns, key, key)
        for key in ("hifigan_model", "bigvgan_model"):
            if _read(ns, key) is not None:
                raise AdapterUnsupported("gpt_vocoder_version_unsupported")
        self._add_sv(inv, ns, "sv_cn_model")
        self._add_ap_bwe(inv, ns, "sr_model")
        self._add_t2s_nonregistered(inv, _read(ns, "t2s_model"))
        self._add_clear_cache(inv, ns, "resample_transform_dict", "resample_transform_dict", modules=True)
        self._add_semantic_cache(inv, ns)
        self._add_audio_caches(inv, "gpt_mel_processing")
        inv.devices.append((ns, "device", self._original_device or "cpu"))

    def _gpt_api(self, inv: _Inventory) -> None:
        pipeline = self._root()
        configs = _read(pipeline, "configs")
        if _read(configs, "version") not in _GPT_VERSIONS:
            raise AdapterUnsupported("unsupported_gpt_model_version")
        if type(pipeline).__name__ != "TTS" or not callable(_read(pipeline, "set_device")):
            raise AdapterUnsupported("unsupported_gpt_pipeline_type")
        for key in ("t2s_model", "vits_model", "bert_model", "cnhuhbert_model"):
            self._add_model(inv, pipeline, key, key)
        if _read(pipeline, "vocoder") is not None:
            raise AdapterUnsupported("gpt_vocoder_version_unsupported")
        self._add_ap_bwe(inv, pipeline, "sr_model")
        self._add_sv(inv, pipeline, "sv_model")
        self._add_t2s_nonregistered(inv, _read(pipeline, "t2s_model"))
        preprocessor = _read(pipeline, "text_preprocessor")
        if _read(preprocessor, "bert_model") is not _read(pipeline, "bert_model"):
            raise AdapterUnsupported("gpt_preprocessor_model_mismatch")
        self._device(_read(preprocessor, "device"))
        inv.devices.append((preprocessor, "device", self._original_device or "cpu"))
        inv.devices.append((configs, "device", self._original_device or "cpu"))
        cache = _read(pipeline, "prompt_cache")
        if type(cache) is not dict or set(cache) - _PROMPT_KEYS:
            raise AdapterUnsupported("unsupported_gpt_prompt_cache_layout")
        for key in ("prompt_semantic", "refer_spec", "bert_features"):
            self._add_tensor(inv, cache, key, f"prompt_cache.{key}")
        if "raw_audio" in cache:
            self._add_tensor(inv, cache, "raw_audio", "prompt_cache.raw_audio")
        self._add_audio_caches(inv, "gpt_mel_processing")
        self._add_clear_cache(inv, self.shared_modules["gpt_tts_module"], "resample_transform_dict", "tts.resample_transform_dict", modules=True)

    def _qwen_model(self, inv: _Inventory, root: Any) -> None:
        qwen = _read(root, "qwen_emo")
        model = _read(qwen, "model")
        self._check_model(model)
        tensors = list(self._module_tensors(model))
        if not tensors:
            raise AdapterUnsupported("qwen_model_has_no_resident_tensors")
        devices = {self._device(t.device) for t in tensors}
        if len(devices) != 1:
            raise AdapterUnsupported("qwen_multi_device_unsupported")
        current = next(iter(devices))
        if current != "cpu" and current != self._original_device:
            raise AdapterUnsupported("qwen_other_gpu_unsupported")
        device_map = _optional(model, "hf_device_map")
        if device_map is not None:
            if type(device_map) is not dict or not device_map:
                raise AdapterUnsupported("qwen_device_map_unsupported")
            mapped = set()
            for key, value in device_map.items():
                if type(key) is not str or type(value) not in (int, str, self.torch.device):
                    raise AdapterUnsupported("qwen_device_map_unsupported")
                mapped.add(self._device(f"cuda:{value}" if type(value) is int else value))
            if len(mapped) != 1:
                raise AdapterUnsupported("qwen_sharded_device_map_unsupported")
            if mapped != {current}:
                raise AdapterUnsupported("qwen_device_map_residency_mismatch")
            # One-device auto placement without Accelerate hooks is the
            # supported subset. CPU/disk dispatch and sharded hooks are not.
            inv.device_maps.append(model)
        if self._qwen_device is None:
            self._qwen_device = current
        self._add_model(inv, qwen, "model", "qwen_emo.model", restore_device=self._qwen_device)

    def _index(self, inv: _Inventory) -> None:
        root = self._root()
        if type(root).__name__ != "IndexTTS2":
            raise AdapterUnsupported("unsupported_index_pipeline_type")
        if _read(root, "use_accel") or _read(root, "use_torch_compile"):
            raise AdapterUnsupported("index_acceleration_unsupported")
        gpt = _read(root, "gpt")
        if _optional(gpt, "accel_engine") is not None or _optional(gpt, "ds_engine") is not None:
            raise AdapterUnsupported("index_acceleration_unsupported")
        for key in ("gpt", "semantic_model", "semantic_codec", "s2mel", "campplus_model", "bigvgan"):
            self._add_model(inv, root, key, key)
        for key in (
            "semantic_mean", "semantic_std", "emo_matrix", "spk_matrix",
            "cache_spk_cond", "cache_s2mel_style", "cache_s2mel_prompt",
            "cache_emo_cond", "cache_mel",
        ):
            self._add_tensor(inv, root, key, key)
        inference_model = _read(gpt, "inference_model")
        self._check_model(inference_model)
        if _read(inference_model, "model_parallel"):
            raise AdapterUnsupported("index_model_parallel_unsupported")
        self._add_tensor(inv, inference_model, "cached_mel_emb", "gpt.inference_model.cached_mel_emb")
        transformer = _path(_read(root, "s2mel"), ("models", "cfm", "estimator", "transformer"))
        self._check_model(transformer)
        if _read(transformer, "use_kv_cache") is not False:
            raise AdapterUnsupported("index_s2mel_kv_cache_layout_unsupported")
        for key in ("freqs_cis", "causal_mask", "mask_cache"):
            self._add_tensor(inv, transformer, key, f"s2mel.transformer.{key}")
        self._qwen_model(inv, root)
        self._add_audio_caches(inv, "index_audio")
        inv.devices.append((root, "device", self._original_device or "cpu"))

    def _root(self) -> Any:
        if self.engine == "gpt_api_v2":
            return _optional(self.namespace, "tts_pipeline", self.namespace)
        if self.engine == "index_v2":
            return _optional(self.namespace, "tts", self.namespace)
        return self.namespace

    def _inventory(self) -> _Inventory:
        if self.torch is None:
            raise AdapterUnsupported("torch_unavailable")
        if self.engine not in _SHARED_KEYS:
            raise AdapterUnsupported("unsupported_engine")
        if type(self.shared_modules) is not dict or set(self.shared_modules) != _SHARED_KEYS[self.engine]:
            raise AdapterUnsupported("required_shared_modules_missing_or_unknown")
        if self.engine == "gpt_api_v2":
            declared = _read(_read(self._root(), "configs"), "device")
        else:
            declared = _read(self._root(), "device")
        device = self._device(declared)
        if self._original_device is None:
            self._original_device = device
        if device not in {"cpu", self._original_device}:
            raise AdapterUnsupported("engine_device_changed")
        inv = _Inventory()
        {"gpt_webui": self._gpt_webui, "gpt_api_v2": self._gpt_api, "index_v2": self._index}[self.engine](inv)
        allowed = {"cpu", self._original_device}
        for slot in inv.models:
            if any(self._device(t.device) not in allowed for t in self._module_tensors(slot.value())):
                raise AdapterUnsupported("multiple_gpu_devices_unsupported")
        for slot in inv.tensors:
            leaves = slot.value().values() if slot.kind == "tensor_map" else self._tensor_leaves(slot.value())
            if any(self._device(t.device) not in allowed for t in leaves):
                raise AdapterUnsupported("multiple_gpu_devices_unsupported")
        for alias in inv.aliases:
            if self._device(_read(alias.owner, alias.key).device) not in allowed:
                raise AdapterUnsupported("multiple_gpu_devices_unsupported")
        for slot in inv.clear_caches:
            for value in slot.value().values():
                leaves = self._module_tensors(value) if slot.kind == "module_cache" else (value,)
                if any(self._device(t.device) not in allowed for t in leaves):
                    raise AdapterUnsupported("multiple_gpu_devices_unsupported")
        return inv

    def _inspect(self, inv: _Inventory) -> dict:
        devices: dict[str, int] = {}
        seen: set[int] = set()
        all_cpu, all_original = True, True
        names: list[str] = []

        def inspect_tensor(tensor: Any, expected: str) -> None:
            nonlocal all_cpu, all_original
            device = self._device(tensor.device)
            all_cpu = all_cpu and device == "cpu"
            all_original = all_original and device == expected
            if id(tensor) not in seen:
                seen.add(id(tensor))
                devices[device] = devices.get(device, 0) + 1

        for slot in inv.models:
            names.append(slot.name)
            for tensor in self._module_tensors(slot.value()):
                inspect_tensor(tensor, slot.restore_device)
        for slot in inv.tensors:
            names.append(slot.name)
            leaves = slot.value().values() if slot.kind == "tensor_map" else self._tensor_leaves(slot.value())
            for tensor in leaves:
                inspect_tensor(tensor, slot.restore_device)
        for alias in inv.aliases:
            inspect_tensor(_read(alias.owner, alias.key), self._original_device or "cpu")
        for slot in inv.clear_caches:
            # Device-keyed shared caches are discarded on both transitions.
            # A nonempty cache is allowed only while running on the original
            # device. A stale GPU-key/CPU-tensor entry never verifies ready.
            for value in slot.value().values():
                leaves = self._module_tensors(value) if slot.kind == "module_cache" else (value,)
                for tensor in leaves:
                    inspect_tensor(tensor, self._original_device or "cpu")
        metadata_cpu = all(self._device(_read(owner, key)) == "cpu" for owner, key, _ in inv.devices)
        metadata_original = all(self._device(_read(owner, key)) == original for owner, key, original in inv.devices)
        residency = "cpu" if all_cpu and metadata_cpu else "gpu" if all_original and metadata_original and any(k.startswith("cuda:") for k in devices) else "unknown"
        return {
            "engine": self.engine, "supported": True,
            "residency": residency, "verified": residency != "unknown" and not self._failed,
            "devices": devices, "model_fields": names,
            "original_device": self._original_device,
            "allocator_verification": "required_after_all_process_adapters",
            "error": self._last_error,
        }

    def snapshot(self) -> dict:
        """Return redacted field/device evidence without changing residency."""
        try:
            return self._inspect(self._inventory())
        except AdapterUnsupported as exc:
            return self._unsupported(str(exc))
        except Exception:
            return self._unsupported("adapter_inspection_failed")

    def _unsupported(self, code: str) -> dict:
        return {
            "engine": self.engine, "supported": False, "verified": False,
            "residency": "unknown", "error": code,
        }

    def _move_tree(self, value: Any, target: str) -> Any:
        if self._tensor(value):
            return self.torch.Tensor.to(value, device=self.torch.device(target))
        if type(value) is tuple:
            return tuple(self._move_tree(item, target) for item in value)
        if type(value) is list:
            return [self._move_tree(item, target) for item in value]
        return None  # Preflight accepts only None or known tensor containers.

    def _transition(self, offload: bool) -> dict:
        try:
            # Validate every supported field before making the first change.
            inv = self._inventory()
            target = "cpu" if offload else self._original_device or "cpu"
            if self.engine == "gpt_api_v2":
                # Explicit existing native method; never persist machine
                # defaults or invoke a method named by a remote request.
                self._root().set_device(self.torch.device(target), save=False)
            for slot in inv.models:
                destination = "cpu" if offload else slot.restore_device
                self.torch.nn.Module.to(slot.value(), device=self.torch.device(destination))
            for slot in inv.tensors:
                destination = "cpu" if offload else slot.restore_device
                if slot.kind == "tensor_map":
                    value = {
                        key: self._move_tree(tensor, destination)
                        for key, tensor in slot.value().items()
                    }
                else:
                    value = self._move_tree(slot.value(), destination)
                _write(slot.owner, slot.key, value)
            for alias in inv.aliases:
                # Scripted GPT fast blocks retain direct weight references.
                # Rebind to registered weights after Module.to; merely moving
                # those aliases independently breaks future weight identity.
                _write(alias.owner, alias.key, _path(alias.layer, alias.source))
            for slot in inv.clear_caches:
                slot.value().clear()
            for owner, key, original in inv.devices:
                destination = "cpu" if offload else original
                value = _read(owner, key)
                _write(owner, key, destination if isinstance(value, str) else self.torch.device(destination))
            for model in inv.device_maps:
                destination = "cpu" if offload else self._qwen_device or "cpu"
                model.hf_device_map = {key: destination for key in model.hf_device_map}
            # Drop inventory references before standard allocator cleanup.
            del inv
            gc.collect()
            if (self._original_device or "").startswith("cuda:"):
                self.torch.cuda.synchronize(self.torch.device(self._original_device))
                self.torch.cuda.empty_cache()
            self._failed = False
            self._last_error = None
            result = self.snapshot()
            expected = "cpu" if offload else "gpu" if target.startswith("cuda:") else "cpu"
            if not result["verified"] or result["residency"] != expected:
                self._failed = True
                self._last_error = "residency_verification_failed"
                return {**result, "verified": False, "error": self._last_error}
            return result
        except AdapterUnsupported as exc:
            return self._unsupported(str(exc))
        except Exception:
            # A partially migrated engine remains behind the guard's closed
            # gate. Explicit restore() may recover it; no inference proceeds.
            self._failed = True
            self._last_error = "device_transition_failed"
            return {
                "engine": self.engine, "supported": True, "verified": False,
                "residency": "unknown", "error": self._last_error,
            }

    def offload(self) -> dict:
        return self._transition(True)

    def restore(self) -> dict:
        return self._transition(False)
