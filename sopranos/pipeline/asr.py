from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from faster_whisper import WhisperModel

from sopranos.config import (
    WHISPER_COMPUTE_TYPE, WHISPER_DEVICE, WHISPER_LANGUAGE, WHISPER_MODEL,
)


@dataclass
class Word:
    start: float
    end: float
    text: str


@dataclass
class Segment:
    start: float
    end: float
    text: str
    words: list[Word]


_model: WhisperModel | None = None


def _get_model() -> WhisperModel:
    global _model
    if _model is None:
        _model = WhisperModel(
            WHISPER_MODEL, device=WHISPER_DEVICE, compute_type=WHISPER_COMPUTE_TYPE,
        )
    return _model


def transcribe(wav_path: Path, out_json: Path) -> dict:
    model = _get_model()
    segments_iter, info = model.transcribe(
        str(wav_path),
        language=WHISPER_LANGUAGE,
        word_timestamps=True,
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=500),
    )
    segments: list[Segment] = []
    for seg in segments_iter:
        words = [Word(start=w.start, end=w.end, text=w.word) for w in (seg.words or [])]
        segments.append(Segment(start=seg.start, end=seg.end, text=seg.text, words=words))
    payload = {
        "language": info.language,
        "duration": info.duration,
        "segments": [asdict(s) for s in segments],
    }
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload))
    return payload


def iter_words(transcript: dict) -> Iterable[Word]:
    for seg in transcript["segments"]:
        for w in seg["words"]:
            yield Word(start=w["start"], end=w["end"], text=w["text"])
