from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console

from sopranos.config import ARTIFACTS_DIR
from sopranos.db.connection import connect
from sopranos.pipeline import asr, audio, keyframes, probe, scenes, shots, subtitles, vlm, index
from sopranos.pipeline.vlm import VLMRequest
from sopranos.roster import load_roster
from sopranos.utils.episode_paths import EpisodeRef


STAGE_ORDER = ["probe", "audio", "asr", "shots", "scenes", "keyframes", "label", "index"]

console = Console()


@dataclass
class EpisodeArtifacts:
    ref: EpisodeRef
    dir: Path

    @property
    def transcript_json(self) -> Path: return self.dir / "transcript.json"
    @property
    def shots_json(self) -> Path: return self.dir / "shots.json"
    @property
    def scenes_json(self) -> Path: return self.dir / "scenes.json"
    @property
    def keyframes_dir(self) -> Path: return self.dir / "keyframes"
    @property
    def scene_labels_json(self) -> Path: return self.dir / "scene_labels.json"
    @property
    def vlm_cache_dir(self) -> Path: return self.dir / "vlm_cache"
    @property
    def wav_path(self) -> Path: return self.dir / "audio.wav"
    @property
    def probe_json(self) -> Path: return self.dir / "probe.json"

    def sentinel(self, stage: str) -> Path:
        return self.dir / f"_done_{STAGE_ORDER.index(stage):02d}_{stage}"

    def is_done(self, stage: str) -> bool:
        return self.sentinel(stage).exists()

    def mark_done(self, stage: str) -> None:
        self.sentinel(stage).write_text("")

    def invalidate_from(self, stage: str) -> None:
        first_idx = STAGE_ORDER.index(stage)
        for s in STAGE_ORDER[first_idx:]:
            self.sentinel(s).unlink(missing_ok=True)


def artifacts_for(ref: EpisodeRef) -> EpisodeArtifacts:
    d = ARTIFACTS_DIR / ref.code
    d.mkdir(parents=True, exist_ok=True)
    return EpisodeArtifacts(ref=ref, dir=d)


def _run_probe(art: EpisodeArtifacts) -> dict:
    if art.is_done("probe") and art.probe_json.exists():
        return json.loads(art.probe_json.read_text())
    res = probe.ffprobe(str(art.ref.path))
    data = {"duration_s": res.duration_s, "fps": res.fps, "audio_sample_rate": res.audio_sample_rate}
    art.probe_json.write_text(json.dumps(data))
    art.mark_done("probe")
    return data


def _run_audio(art: EpisodeArtifacts) -> Path | None:
    """Extract audio only if we'll actually run ASR (i.e. no usable SRT)."""
    if art.is_done("audio") and art.wav_path.exists():
        return art.wav_path
    if subtitles.find_srt(art.ref.season, art.ref.episode) is not None:
        art.mark_done("audio")  # short-circuit; SRT path won't read wav
        return None
    console.log(f"[{art.ref.code}] extracting audio")
    audio.extract_wav(art.ref.path, art.wav_path)
    art.mark_done("audio")
    return art.wav_path


def _run_asr(art: EpisodeArtifacts) -> dict:
    if art.is_done("asr") and art.transcript_json.exists():
        return json.loads(art.transcript_json.read_text())
    srt_path = subtitles.find_srt(art.ref.season, art.ref.episode)
    if srt_path is not None:
        offset = subtitles.offset_for(art.ref.season, art.ref.episode)
        console.log(f"[{art.ref.code}] parsing SRT ({srt_path.name}, offset {offset:+.2f}s)")
        payload = subtitles.srt_to_transcript_payload(srt_path, offset)
        art.transcript_json.parent.mkdir(parents=True, exist_ok=True)
        art.transcript_json.write_text(json.dumps(payload))
    else:
        if not art.wav_path.exists():
            console.log(f"[{art.ref.code}] extracting audio (SRT not found)")
            audio.extract_wav(art.ref.path, art.wav_path)
        console.log(f"[{art.ref.code}] transcribing (faster-whisper)")
        payload = asr.transcribe(art.wav_path, art.transcript_json)
    art.mark_done("asr")
    return payload


def _run_shots(art: EpisodeArtifacts) -> list[dict]:
    if art.is_done("shots") and art.shots_json.exists():
        return json.loads(art.shots_json.read_text())
    console.log(f"[{art.ref.code}] detecting shots")
    s = shots.detect_shots(art.ref.path, art.shots_json)
    art.mark_done("shots")
    return s


def _run_scenes(art: EpisodeArtifacts, shot_list: list[dict], transcript: dict) -> list[dict]:
    if art.is_done("scenes") and art.scenes_json.exists():
        return json.loads(art.scenes_json.read_text())
    console.log(f"[{art.ref.code}] merging shots into narrative scenes")
    is_e01 = art.ref.episode == 1
    sc = scenes.merge_shots_to_scenes(
        art.ref.path, shot_list, transcript,
        is_first_episode_of_season=is_e01,
        out_json=art.scenes_json,
    )
    art.mark_done("scenes")
    return [{
        "scene_index": s.scene_index, "start_s": s.start_s, "end_s": s.end_s,
        "duration_s": s.duration_s, "shot_indices": s.shot_indices,
    } for s in sc]


def _run_keyframes(art: EpisodeArtifacts, scene_list: list[dict], shot_list: list[dict]) -> dict[int, list[str]]:
    if art.is_done("keyframes"):
        # Reconstruct mapping from disk
        result: dict[int, list[str]] = {}
        for sc in scene_list:
            files = sorted(art.keyframes_dir.glob(f"scene_{sc['scene_index']:03d}_*.jpg"))
            result[sc["scene_index"]] = [str(p) for p in files]
        if all(result.get(sc["scene_index"]) for sc in scene_list):
            return result

    console.log(f"[{art.ref.code}] extracting keyframes for {len(scene_list)} scenes")
    shot_by_idx = {s["shot_index"]: s for s in shot_list}
    result = {}
    for sc in scene_list:
        shot_se = [(shot_by_idx[i]["start_s"], shot_by_idx[i]["end_s"]) for i in sc["shot_indices"] if i in shot_by_idx]
        paths = keyframes.extract_keyframes(
            art.ref.path, sc["start_s"], sc["end_s"], shot_se,
            art.keyframes_dir, sc["scene_index"],
        )
        result[sc["scene_index"]] = [str(p) for p in paths]
    art.mark_done("keyframes")
    return result


def _run_label(
    art: EpisodeArtifacts,
    scene_list: list[dict],
    transcript: dict,
    keyframes_by_idx: dict[int, list[str]],
) -> dict:
    if art.is_done("label") and art.scene_labels_json.exists():
        return json.loads(art.scene_labels_json.read_text())

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "ANTHROPIC_API_KEY not set; cannot run VLM labeling stage.\n"
            "Export your key: export ANTHROPIC_API_KEY=sk-ant-..."
        )

    console.log(f"[{art.ref.code}] labeling {len(scene_list)} scenes with Claude Haiku")
    roster = load_roster()
    requests: list[VLMRequest] = []
    for sc in scene_list:
        kfs = [Path(p) for p in keyframes_by_idx.get(sc["scene_index"], [])]
        if not kfs:
            continue
        tt = scenes.transcript_text_for_scene(transcript, sc["start_s"], sc["end_s"])
        requests.append(VLMRequest(
            episode_code=art.ref.code,
            scene_index=sc["scene_index"],
            start_s=sc["start_s"], end_s=sc["end_s"],
            keyframe_paths=kfs, transcript_text=tt,
        ))

    responses = asyncio.run(vlm.label_scenes(requests, roster, art.vlm_cache_dir))

    payload = {
        "scenes": [{
            "scene_index": r.request.scene_index,
            "label": r.label.model_dump(),
            "raw": r.raw_json,
            "usage": r.usage,
            "cached": r.cached,
            "keyframes": [str(p) for p in r.request.keyframe_paths],
            "transcript_text": r.request.transcript_text,
        } for r in responses],
    }
    art.scene_labels_json.write_text(json.dumps(payload))
    art.mark_done("label")
    return payload


def _run_index(art: EpisodeArtifacts, probe_data: dict, shot_list: list[dict],
               scene_list: list[dict], labels_payload: dict, transcript: dict) -> None:
    console.log(f"[{art.ref.code}] indexing into SQLite")
    labels_by_idx = {s["scene_index"]: vlm_label_from_dict(s["label"]) for s in labels_payload["scenes"]}
    raw_by_idx = {s["scene_index"]: s["raw"] for s in labels_payload["scenes"]}
    kfs_by_idx = {s["scene_index"]: s["keyframes"] for s in labels_payload["scenes"]}
    tr_by_idx = {s["scene_index"]: s["transcript_text"] for s in labels_payload["scenes"]}
    usage_by_idx = {s["scene_index"]: s["usage"] for s in labels_payload["scenes"]}

    with connect() as conn:
        index.sync_characters(conn)
        eid = index.upsert_episode(conn, art.ref, probe_data["duration_s"], probe_data["fps"])
        scene_ids = index.replace_episode_artifacts(
            conn, eid, shot_list, scene_list, labels_by_idx, raw_by_idx, kfs_by_idx, tr_by_idx,
        )
        # Map scene_index -> scene_id by order of scene_list
        for sc, sid in zip(scene_list, scene_ids):
            usage = usage_by_idx.get(sc["scene_index"])
            if usage:
                from sopranos.config import VLM_MODEL
                index.log_usage(conn, "label", VLM_MODEL, usage, scene_id=sid)
    art.mark_done("index")


def vlm_label_from_dict(d: dict):
    from sopranos.db.models import SceneLabel
    return SceneLabel(**d)


def run_episode(ref: EpisodeRef, force_from: str | None = None) -> None:
    art = artifacts_for(ref)
    if force_from:
        art.invalidate_from(force_from)

    probe_data = _run_probe(art)
    _run_audio(art)
    transcript = _run_asr(art)
    shot_list = _run_shots(art)
    scene_list = _run_scenes(art, shot_list, transcript)
    keyframes_by_idx = _run_keyframes(art, scene_list, shot_list)
    labels_payload = _run_label(art, scene_list, transcript, keyframes_by_idx)
    _run_index(art, probe_data, shot_list, scene_list, labels_payload, transcript)

    # Cleanup oversized intermediate
    if art.wav_path.exists():
        try:
            art.wav_path.unlink()
        except OSError:
            pass

    console.log(f"[{art.ref.code}] done. {len(scene_list)} scenes indexed.")
