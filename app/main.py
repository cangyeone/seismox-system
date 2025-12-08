from __future__ import annotations

import asyncio
from typing import List

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlmodel import SQLModel, Session, select

from .database import engine, init_db
from .models import Event, EventWithPicks, HealthResponse, PhasePick, Station, StationRead, Waveform
from .pipeline import ProcessingRequest, enqueue_waveform, process_waveforms, waveform_queue
from .storage import persist_waveform

app = FastAPI(title="SeismoX System", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _on_startup() -> None:
    init_db()
    loop = asyncio.get_event_loop()
    loop.create_task(process_waveforms())


def get_session():
    with Session(engine) as session:
        yield session


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        message="SeismoX pipeline online",
        processing_queue_size=waveform_queue.qsize(),
    )


@app.post("/stations", response_model=StationRead)
def create_station(station: Station, session: Session = Depends(get_session)) -> Station:
    session.add(station)
    session.commit()
    session.refresh(station)
    return station


@app.get("/stations", response_model=List[StationRead])
def list_stations(session: Session = Depends(get_session)) -> List[StationRead]:
    return session.exec(select(Station)).all()


@app.get("/stations/{station_id}", response_model=StationRead)
def get_station(station_id: int, session: Session = Depends(get_session)) -> StationRead:
    station = session.get(Station, station_id)
    if not station:
        raise HTTPException(status_code=404, detail="Station not found")
    return station


class WaveformIngestRequest(SQLModel):
    station_code: str
    payload_base64: str


@app.post("/waveforms/ingest")
async def ingest_waveform(request: WaveformIngestRequest, session: Session = Depends(get_session)) -> dict:
    station = session.exec(select(Station).where(Station.code == request.station_code)).first()
    if not station:
        raise HTTPException(status_code=404, detail="Station not registered")

    file_path, received_at = persist_waveform(request.station_code, request.payload_base64)
    waveform = Waveform(
        station_id=station.id,
        file_path=file_path,
        received_at=received_at,
    )
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
    return {"waveform_id": waveform.id, "queued": True}


@app.get("/events", response_model=List[Event])
def list_events(session: Session = Depends(get_session)) -> List[Event]:
    return session.exec(select(Event).order_by(Event.origin_time.desc())).all()


@app.get("/events/{event_id}", response_model=EventWithPicks)
def get_event(event_id: int, session: Session = Depends(get_session)) -> EventWithPicks:
    event = session.get(Event, event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    picks = session.exec(select(PhasePick).where(PhasePick.event_id == event.id)).all()
    return EventWithPicks(**event.dict(), id=event.id, picks=picks)


@app.get("/picks", response_model=List[PhasePick])
def list_picks(session: Session = Depends(get_session)) -> List[PhasePick]:
    return session.exec(select(PhasePick).order_by(PhasePick.pick_time.desc()).limit(200)).all()


@app.get("/")
def root() -> dict:
    return {"message": "SeismoX realtime catalog API", "docs": "/docs"}
