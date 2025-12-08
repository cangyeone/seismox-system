"""SeedLink live ingest and visualization bridge for the dashboard."""

from __future__ import annotations

import asyncio
import datetime as dt
import io
import logging
import threading
from typing import Any, Dict, Optional

from obspy.clients.seedlink.easyseedlink import create_client
from sqlmodel import Session, select

from .database import engine
from .models import Station, Waveform
from .pipeline import ProcessingRequest, enqueue_waveform
from .storage import persist_waveform_bytes

logger = logging.getLogger(__name__)

_trace_queue: asyncio.Queue = asyncio.Queue()
_stream_task: Optional[asyncio.Task] = None
_worker_task: Optional[asyncio.Task] = None
_stop_event = threading.Event()
_sl_client = None
_latest_frame: Optional[Dict[str, Any]] = None
_status: Dict[str, Any] = {
    "running": False,
    "frames": 0,
    "network": None,
    "station": None,
    "location": None,
    "channel": None,
    "last_frame": None,
    "error": None,
}


async def start_live_stream(
    network: str = "IU", station: str = "ANMO", location: str = "00", channel: str = "BHZ"
) -> bool:
    """Start a SeedLink streaming task and a consumer that feeds the processing pipeline."""

    global _stream_task, _worker_task
    if _status.get("running"):
        return False

    loop = asyncio.get_running_loop()
    _stop_event.clear()
    _status.update(
        {
            "running": True,
            "frames": 0,
            "network": network,
            "station": station,
            "location": location,
            "channel": channel,
            "error": None,
        }
    )

    _stream_task = asyncio.create_task(asyncio.to_thread(_pump_traces, loop, network, station, location, channel))
    _worker_task = asyncio.create_task(_consume_traces())
    return True


async def stop_live_stream() -> bool:
    """Stop streaming and processing tasks."""

    global _stream_task, _worker_task
    if not _status.get("running"):
        return False

    _stop_event.set()
    _status["running"] = False

    if _sl_client:
        try:
            closer = getattr(_sl_client, "close", None) or getattr(_sl_client, "disconnect", None)
            if closer:
                closer()
        except Exception:  # pragma: no cover - best effort shutdown
            logger.exception("Failed to close SeedLink client")

    if _stream_task:
        try:
            await asyncio.wait_for(_stream_task, timeout=3)
        except asyncio.TimeoutError:
            _stream_task.cancel()
        _stream_task = None

    if _worker_task:
        _worker_task.cancel()
        try:
            await _worker_task
        except asyncio.CancelledError:
            pass
        _worker_task = None
    return True


def get_live_status() -> Dict[str, Any]:
    return dict(_status)


def get_latest_frame() -> Optional[Dict[str, Any]]:
    return _latest_frame


def _pump_traces(loop: asyncio.AbstractEventLoop, network: str, station: str, location: str, channel: str) -> None:
    """Run in a background thread, pushing traces into the asyncio queue."""

    global _sl_client

    def _on_trace(trace):
        if _stop_event.is_set():
            return
        asyncio.run_coroutine_threadsafe(_trace_queue.put(trace), loop).result()

    try:
        _sl_client = create_client(server_url="rtserve.iris.washington.edu", on_data=_on_trace)
        _sl_client.select_stream(network, station, location, channel)
        _sl_client.run()
    except Exception as exc:  # pragma: no cover - protective logging
        logger.exception("SeedLink streaming failed: %s", exc)
        _status["error"] = str(exc)
    finally:
        if _sl_client:
            closer = getattr(_sl_client, "close", None) or getattr(_sl_client, "disconnect", None)
            if closer:
                try:
                    closer()
                except Exception:  # pragma: no cover - best effort shutdown
                    logger.exception("Failed to close SeedLink client during teardown")
        _stop_event.set()
        _status["running"] = False


async def _consume_traces() -> None:
    global _latest_frame
    while not _stop_event.is_set():
        try:
            trace = await asyncio.wait_for(_trace_queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            continue

        try:
            await _handle_trace(trace)
            _status["frames"] += 1
            _status["last_frame"] = dt.datetime.utcnow().isoformat()
            # downsample for UI
            samples = trace.data.tolist()
            step = max(1, len(samples) // 800)
            reduced = samples[::step]
            _latest_frame = {
                "network": trace.stats.network,
                "station": trace.stats.station,
                "channel": trace.stats.channel,
                "start_time": str(trace.stats.starttime),
                "sampling_rate": float(trace.stats.sampling_rate),
                "samples": reduced,
            }
        finally:
            _trace_queue.task_done()


async def _handle_trace(trace) -> None:
    """Persist trace to disk, register the station if absent, and enqueue for processing."""

    with Session(engine) as session:
        station = session.exec(select(Station).where(Station.code == trace.stats.station)).first()
        if not station:
            station = Station(
                code=trace.stats.station,
                name=f"{trace.stats.network}-{trace.stats.station}",
                latitude=0.0,
                longitude=0.0,
                elevation_m=0.0,
                status="streaming",
            )
            session.add(station)
            session.commit()
            session.refresh(station)

        buffer = io.BytesIO()
        trace.write(buffer, format="MSEED")
        file_path, received_at = persist_waveform_bytes(trace.stats.station, buffer.getvalue())

        waveform = Waveform(station_id=station.id, file_path=file_path, received_at=received_at)
        session.add(waveform)
        session.commit()
        session.refresh(waveform)

        await enqueue_waveform(
            ProcessingRequest(
                waveform_id=waveform.id,
                station_id=station.id,
                file_path=file_path,
                received_at=received_at,
            )
        )
