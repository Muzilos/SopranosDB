from __future__ import annotations

import numpy as np

from sopranos.config import EMBED_MODEL


_model = None


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        # Force CPU: the 1050 Ti (sm_61) is incompatible with the bundled PyTorch CUDA build.
        # MiniLM is small enough that CPU is ~5ms/sentence, no need for GPU.
        _model = SentenceTransformer(EMBED_MODEL, device="cpu")
    return _model


def embed_texts(texts: list[str]) -> np.ndarray:
    model = _get_model()
    vecs = model.encode(texts, batch_size=32, normalize_embeddings=True, show_progress_bar=False)
    return np.asarray(vecs, dtype=np.float32)


def embed_text(text: str) -> np.ndarray:
    return embed_texts([text])[0]
