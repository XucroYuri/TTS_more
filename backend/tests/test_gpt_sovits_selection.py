from app.gpt_sovits_selection import pair_dynamic_weights
from app.voice_matching_models import (
    CatalogSnapshot,
    VoiceResourceRecord,
    WeightArtifactRecord,
)


def _weight(
    artifact_id: str,
    relative_path: str,
    kind: str,
    training_task: str,
    *,
    root_id: str = "portable",
) -> WeightArtifactRecord:
    return WeightArtifactRecord(
        artifact_id=artifact_id,
        root_id=root_id,
        relative_path=relative_path,
        kind=kind,
        character_id="九九",
        training_task=training_task,
        fingerprint=f"fingerprint-{artifact_id}",
    )


def _dynamic_resource() -> VoiceResourceRecord:
    return VoiceResourceRecord(
        resource_id="gpt-sovits-local",
        character_id="九九",
        supports_dynamic_weights=True,
        compatible_root_ids=["portable"],
    )


def test_dynamic_pair_uses_highest_progress_within_exact_training_task() -> None:
    snapshot = CatalogSnapshot(
        version="catalog-v1",
        weight_artifacts=[
            _weight("gpt-old", "GPT_weights_v2ProPlus/task-a-e40.ckpt", "gpt", "task-a"),
            _weight("gpt-new", "GPT_weights_v2ProPlus/task-a-e50.ckpt", "gpt", "task-a"),
            _weight("sovits-old", "SoVITS_weights_v2ProPlus/task-a_e20_s300.pth", "sovits", "task-a"),
            _weight("sovits-new", "SoVITS_weights_v2ProPlus/task-a_e24_s360.pth", "sovits", "task-a"),
        ],
    )

    pairs = pair_dynamic_weights(snapshot, _dynamic_resource())

    assert len(pairs) == 1
    assert pairs[0].training_task == "task-a"
    assert pairs[0].gpt_weight_artifact_id == "gpt-new"
    assert pairs[0].sovits_weight_artifact_id == "sovits-new"
    assert pairs[0].root_id == "portable"


def test_dynamic_pair_never_joins_similar_tasks_or_different_roots() -> None:
    snapshot = CatalogSnapshot(
        version="catalog-v1",
        weight_artifacts=[
            _weight("gpt-a", "GPT_weights/task-a-e50.ckpt", "gpt", "task-a"),
            _weight("sovits-prefix", "SoVITS_weights/task-a-extra_e24_s360.pth", "sovits", "task-a-extra"),
            _weight("sovits-other-root", "SoVITS_weights/task-a_e24_s360.pth", "sovits", "task-a", root_id="other"),
        ],
    )

    assert pair_dynamic_weights(snapshot, _dynamic_resource()) == []
