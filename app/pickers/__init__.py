"""Utilities to load and run the bundled ObsPy-based RNN phase picker."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Tuple

import torch

logger = logging.getLogger(__name__)

_MODEL_PATH = Path(__file__).with_name("rnn.origdiff.pnsn.jit")
_model = None  # type: ignore[var-annotated]


def _load_model():
    global _model
    if _model is not None:
        return _model
    if not _MODEL_PATH.exists():
        logger.warning("Picker model not found at %s; using simulation fallback", _MODEL_PATH)
        _model = False
        return _model
    _model = torch.jit.load(str(_MODEL_PATH), map_location="cpu")
    _model.eval()
    logger.info("Loaded picker model from %s", _MODEL_PATH)
    return _model


def run_phase_picker(
    samples: List[List[float]], sampling_rate: float
) -> List[Tuple[int, float, float]]:
    """Run the TorchScript picker on a 10s block of **three-component** samples.

    The bundled TorchScript model expects a shape of ``(batch, channels, N)`` with
    exactly three components. The caller should pre-align channels and supply
    them as ``[[ch1...], [ch2...], [ch3...]]``. Returns tuples of
    ``(phase_index, sample_index, confidence)``.
    """

    model = _load_model()
    if not model:
        return []

    data = torch.tensor(samples, dtype=torch.float32)
    # Accept either (3, N) or (N, 3) and normalize to (1, 3, N)
    if data.dim() == 2 and data.shape[0] != 3 and data.shape[1] == 3:
        data = data.transpose(0, 1)
    if data.dim() == 2:
        data = data.unsqueeze(0)
    elif data.dim() == 3 and data.shape[1] != 3 and data.shape[2] == 3:
        data = data.transpose(1, 2)

    with torch.no_grad():
        result = model(data)

    picks: List[Tuple[int, float, float]] = []
    if isinstance(result, torch.Tensor):
        result = result.detach().cpu().tolist()
    for item in result:
        if not item:
            continue
        try:
            phase_idx, sample_idx, confidence = item[:3]
            picks.append((int(phase_idx), float(sample_idx), float(confidence)))
        except Exception:  # pragma: no cover - tolerate malformed rows
            logger.exception("Failed to parse picker output row: %s", item)
    return picks
