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


def run_phase_picker(samples: List[float], sampling_rate: float) -> List[Tuple[int, float, float]]:
    """Run the TorchScript picker on a 10s block of samples.

    The picker expects a three-component input; when only one component is
    available we duplicate it across channels. Returns tuples of
    ``(phase_index, sample_index, confidence)``.
    """

    model = _load_model()
    if not model:
        return []

    data = torch.tensor(samples, dtype=torch.float32)
    if data.dim() == 1:
        data = data.unsqueeze(0).repeat(3, 1)
    elif data.dim() == 2 and data.shape[0] != 3:
        # best effort reshape for unexpected layouts
        data = data.transpose(0, 1)[:3].transpose(0, 1)
    if data.dim() == 2:
        data = data.unsqueeze(0)

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
