from pathlib import Path

from app.adapters.base import SynthesisRequest, SynthesisResult
from app.models import EngineName, GenerationManifest, GenerationTask, ScriptLine
from app.queue import GenerationQueue


class RecordingAdapter:
    def __init__(self, engine: EngineName) -> None:
        self.engine = engine
        self.calls: list[str] = []

    def health(self) -> dict:
        return {"engine": self.engine, "ready": True}

    def load(self, profile: str) -> None:
        self.calls.append(f"load:{profile}")

    def synthesize(self, request: SynthesisRequest) -> SynthesisResult:
        self.calls.append(f"synthesize:{request.line.id}")
        request.output_path.parent.mkdir(parents=True, exist_ok=True)
        request.output_path.write_bytes(b"RIFFfake")
        return SynthesisResult(audio_path=request.output_path, metadata={"ok": True})

    def unload(self) -> None:
        self.calls.append("unload")


def test_queue_groups_tasks_by_engine_and_profile(tmp_path: Path) -> None:
    gpt = RecordingAdapter(EngineName.GPT_SOVITS)
    index = RecordingAdapter(EngineName.INDEX_TTS)
    queue = GenerationQueue({EngineName.GPT_SOVITS: gpt, EngineName.INDEX_TTS: index})
    manifest = GenerationManifest(project_id="demo")
    tasks = [
        GenerationTask(line=ScriptLine(id="l1", character_id="a", text="1"), engine=EngineName.GPT_SOVITS, profile="p1"),
        GenerationTask(line=ScriptLine(id="l2", character_id="b", text="2"), engine=EngineName.INDEX_TTS, profile="p2"),
        GenerationTask(line=ScriptLine(id="l3", character_id="a", text="3"), engine=EngineName.GPT_SOVITS, profile="p1"),
    ]

    queue.run(tasks, manifest, output_dir=tmp_path)

    assert gpt.calls == ["load:p1", "synthesize:l1", "synthesize:l3", "unload"]
    assert index.calls == ["load:p2", "synthesize:l2", "unload"]
    assert len(manifest.lines["l1"].versions) == 1
    assert len(manifest.lines["l3"].versions) == 1


def test_queue_records_failed_generation_without_stopping_following_group(tmp_path: Path) -> None:
    class FailingAdapter(RecordingAdapter):
        def synthesize(self, request: SynthesisRequest) -> SynthesisResult:
            self.calls.append(f"synthesize:{request.line.id}")
            raise RuntimeError("boom")

    failing = FailingAdapter(EngineName.GPT_SOVITS)
    ok = RecordingAdapter(EngineName.VIBEVOICE)
    queue = GenerationQueue({EngineName.GPT_SOVITS: failing, EngineName.VIBEVOICE: ok})
    manifest = GenerationManifest(project_id="demo")

    queue.run(
        [
            GenerationTask(line=ScriptLine(id="l1", character_id="a", text="1"), engine=EngineName.GPT_SOVITS, profile="p1"),
            GenerationTask(line=ScriptLine(id="l2", character_id="b", text="2"), engine=EngineName.VIBEVOICE, profile="p2"),
        ],
        manifest,
        output_dir=tmp_path,
    )

    assert manifest.lines["l1"].versions[0].status == "failed"
    assert manifest.lines["l2"].versions[0].status == "completed"

