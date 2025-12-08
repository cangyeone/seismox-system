# SeismoX System

A runnable prototype for a regional real-time seismic catalog. The stack is streaming-first (Kafka/Flink-ready) but ships with a lightweight FastAPI service so you can exercise ingestion, detection, association, and catalog persistence in a single process.

## What is implemented
- Station management API (create/list/view) with geospatial metadata and status fields.
- IRIS station auto-discovery/import to seed the catalog without manual typing.
- Waveform ingestion endpoint that accepts base64 mseed payloads, persists them to local storage, and queues them for processing.
- Background real-time pipeline that simulates Pg/Sg/Pn/Sn picking, performs simple association, assigns a location/magnitude, and records the picks and events.
- Event and pick browsing APIs for lightweight web visualization and downstream integration.
- Health endpoint exposing the live processing queue depth.

## Quickstart
1. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```
2. **Run the API**
   ```bash
   uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
   ```
3. **Open the dashboard** at `http://localhost:8000/` for station 管理、USGS/IRIS 数据演示、波形可视化和目录浏览。API docs remain at `/docs`.

### Minimal workflow example
1. **Register a station**
   ```bash
   curl -X POST http://localhost:8000/stations \
     -H "Content-Type: application/json" \
     -d '{
       "code": "ABC1",
       "name": "Demo Station",
       "latitude": 34.25,
       "longitude": 108.95,
       "elevation_m": 1200,
       "status": "healthy"
     }'
   ```
2. **Ingest a waveform** (example uses an empty payload placeholder)
   ```bash
   echo -n "" | base64 | \
   xargs -I{} curl -X POST http://localhost:8000/waveforms/ingest \
     -H "Content-Type: application/json" \
     -d '{"station_code": "ABC1", "payload_base64": "{}"}'
   ```
   The background worker will queue picks, associate an event, and mark the waveform as processed.
3. **List events and picks**
   ```bash
   curl http://localhost:8000/events
   curl http://localhost:8000/picks
   ```

4. **(Optional) Import IRIS stations & start USGS demo feed** from the dashboard (or via API)
   ```bash
   curl -X POST "http://localhost:8000/stations/import/iris?network=IU&limit=8"
   curl -X POST http://localhost:8000/usgs/start
   curl http://localhost:8000/usgs/status
   ```
   The IRIS helper grabs FDSN station metadata (GeoCSV) and writes it into the local DB; the USGS feed pulls the official GeoJSON stream every 60s and materializes events + virtual picks for visualization.

5. **Preview live waveforms from IRIS**
   - The dashboard dropdown auto-populates IU network stations via `/iris/stations`.
   - The “实时波形” card pulls a 5-minute plot via `/iris/waveform` so you can watch remote activity without pushing your own data yet.

## Design notes
- **Processing pipeline**: an asyncio worker drains a queue of waveform processing requests. For each waveform it simulates Pg/Sg/Pn/Sn picks, derives an origin time, estimates a simple location around the reporting station, sets a magnitude, and persists everything to SQLite via SQLModel.
- **Storage**: waveforms are written to `app/data/waveforms/` with timestamped filenames; catalog tables live in `app/data/catalog.db`.
- **Extensibility**: replace `app/pipeline.py` logic with real picker/association/location calls while keeping the API surface stable. The pipeline functions are isolated so you can swap in Kafka/Flink producers and consumers as you scale.
- **Resilience**: ingestion is synchronous but processing is async; health and queue depth are exposed at `/health`. FastAPI startup initializes the database and launches the worker task.

## API surface
- `GET /health` – service health and queue size.
- `POST /stations` – create station metadata.
- `GET /stations` – list stations.
- `GET /stations/{id}` – station details.
- `POST /waveforms/ingest` – accept base64 mseed content, persist, enqueue processing.
- `GET /events` – list events ordered by origin time.
- `GET /events/{id}` – event detail with picks.
- `GET /picks` – recent picks.

## Project status
This is a functional single-node demo with stubbed detection/association/location. Integrate your own models or streaming fabric (Kafka + Flink/Spark) by replacing the logic in `app/pipeline.py` and wiring the ingestion endpoint to produce/consume from your chosen message bus.

## Contact
Questions or collaboration: **caiyuqiming@163.com**
