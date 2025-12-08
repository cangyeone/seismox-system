from __future__ import annotations

import base64
import datetime as dt
from pathlib import Path
from typing import Tuple

from .database import DATA_DIR

WAVEFORM_DIR = DATA_DIR / "waveforms"
WAVEFORM_DIR.mkdir(parents=True, exist_ok=True)


def persist_waveform(station_code: str, payload_base64: str) -> Tuple[str, dt.datetime]:
    timestamp = dt.datetime.utcnow()
    filename = f"{station_code}_{timestamp.strftime('%Y%m%dT%H%M%S%f')}.mseed"
    file_path = WAVEFORM_DIR / filename
    raw = base64.b64decode(payload_base64.encode())
    file_path.write_bytes(raw)
    return str(file_path), timestamp
