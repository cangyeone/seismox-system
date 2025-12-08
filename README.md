# SeismoX System

A blueprint implementation for a real-time seismic cataloging platform designed for regional monitoring. The system focuses on high-throughput streaming (Kafka + Flink/Spark) with thin web services for API/auth and visualization. Detection, association, and location are exposed via robust real-time APIs so that you can plug in your own models and algorithms while the ingestion, storage, and visualization layers remain production-ready.

## Key Capabilities
- **Station management**: Web UI for station inventory, map visualization, health/status monitoring, and channel metadata editing.
- **Waveform ingestion**: Real-time waveform streams ingested to Kafka from digitizers or SeedLink/Arclink gateways; optional batching of historical data. Data are persisted to an object store and mseed waveforms are indexed for retrieval.
- **Realtime phase picking**: Streams feed a Pg/Sg/Pn/Sn picker (e.g., SeismoX picker) that also emits first-motion polarity and coarse event-type labels. Processing errors are surfaced through metrics and retry queues.
- **Association and preliminary location**: Picked phases enter REAL or similar associators to estimate event counts and coarse hypocenters.
- **Precision location**: Candidate events are refined via the SeismoX-Location module for high-quality hypocenters.
- **Magnitude and mechanism**: Magnitude estimation plus first-motion based focal mechanism workflow.
- **Catalog persistence and retrieval**: Events, picks, and station metadata stored in a columnar database (e.g., ClickHouse or Parquet via Hive/Delta); event waveforms stored as mseed in object storage. Catalog entries and waveforms can be visualized in the web client.

## High-Level Architecture
1. **Ingestion layer**
   - **Kafka** as the central message bus; topics for raw waveforms (`waveform.raw`), quality-controlled waveforms (`waveform.qc`), picks (`phase.pick`), association results (`event.assoc`), and finalized events (`event.final`).
   - Optional **Flink** or **Spark Structured Streaming** jobs handle windowing, QC, and routing between topics with at-least-once semantics.
   - **Object storage** (MinIO/S3) hosts archived mseed files referenced by metadata records.

2. **Processing layer**
   - **Realtime picker service** subscribes to `waveform.qc`, runs the SeismoX phase picker, and publishes picks with confidence, polarity, and type labels.
   - **Association** (REAL or alternative) consumes picks and publishes candidate event bundles to `event.assoc`.
   - **SeismoX-Location** consumes associated events, performs precision location, and publishes `event.final` with uncertainties and magnitude.
   - **Magnitude & mechanism** modules run as downstream consumers, producing magnitude updates and focal mechanisms.

3. **Storage layer**
   - **Columnar DB** (ClickHouse/Delta/Hive) for catalog tables: `stations`, `picks`, `events`, `magnitudes`, `mechanisms`, with partitioning by day/region.
   - **mseed repository**: object storage organized by `year/day/network.station.channel.mseed`, referenced by database rows for multi-station waveform retrieval.

4. **Web/API layer**
   - Thin **REST/gRPC API** providing station CRUD, waveform retrieval, event queries, and submission endpoints for detection/association/location.
   - **AuthN/AuthZ** middleware with API keys/JWT; rate limiting on public endpoints.
   - **Web frontend** for station map, event timeline, waveform previews, and health dashboard (Kafka lag, consumer liveness, station uptime).

5. **Observability & resilience**
   - Metrics via Prometheus/OpenTelemetry; dashboards for picker latency, association throughput, location success rate, and Kafka consumer lag.
   - Structured logging with correlation IDs per event; retry and dead-letter topics for malformed messages.
   - Health checks on every microservice with readiness/liveness probes.

## Component Responsibilities
### Station & Waveform Ingestion Service
- Accepts data from SeedLink/Arclink or digitizer gateways, normalizes headers, and writes to `waveform.raw`.
- Performs QC (clipping detection, timing sanity checks) and publishes cleaned streams to `waveform.qc`.
- Periodically rolls mseed segments to object storage and records pointers in the catalog database.

### Realtime Phase Picker API
- `/api/pick` (gRPC/REST): accepts streaming waveforms; returns Pg/Sg/Pn/Sn picks, polarity, and event-type probabilities.
- Runs inference on GPUs/accelerated hardware; exposes model/engine health endpoints.
- Emits pick messages to Kafka with traceability to originating station and file offsets.

### Association Service (REAL or equivalent)
- Consumes `phase.pick`; clusters picks into candidate events with preliminary origin times/locations.
- Supports configurable regional travel-time tables; handles out-of-order picks with watermarking.
- Publishes candidate events to `event.assoc` with quality flags and provenance.

### SeismoX-Location Service
- Consumes `event.assoc` candidates.
- Performs precision relocation using 3-D velocity models; outputs uncertainties and residuals.
- Publishes `event.final` and updates catalog DB; triggers magnitude/mechanism tasks.

### Magnitude & Mechanism Service
- Computes Ml/Md/Mw variants; handles station correction tables.
- Uses first-motion polarities (from picker) to solve focal mechanisms; stores nodal planes and quality metrics.

### Web & Visualization
- Station map with status indicators (latency, data gaps, gain changes).
- Event list with filters (time window, magnitude, region) and detail views.
- Waveform viewer pulling mseed snippets from object storage.
- Admin views for API key management and consumer/stream health.

## Example Data Contracts
```json
// Kafka message: phase pick
{
  "event_id": "candidate-uuid",
  "station": "XX.ABCD.00.BHZ",
  "timestamp": "2024-05-01T12:34:56.789Z",
  "phase": "Pg",
  "polarity": "up",
  "confidence": 0.96,
  "quality": "A",
  "features": {"snr": 14.2}
}
```

```json
// Kafka message: final event
{
  "event_id": "evt-uuid",
  "origin_time": "2024-05-01T12:35:05.120Z",
  "latitude": 35.123,
  "longitude": 105.456,
  "depth_km": 12.3,
  "magnitude": {"ml": 3.5, "mw": 3.4},
  "uncertainty": {"h_km": 1.2, "v_km": 2.1},
  "mechanism": {"strike": 120, "dip": 45, "rake": -90},
  "waveform_refs": ["s3://seismox/2024/122/XX.ABCD.00.BHZ.mseed"]
}
```

## Setup & Deployment (reference)
1. **Prerequisites**: Docker/Podman, docker-compose (or K8s); GPU nodes for realtime picker.
2. **Environment variables**: configure `KAFKA_BOOTSTRAP`, `MINIO_ENDPOINT`, `CATALOG_DSN` (ClickHouse/Hive), `AUTH_JWT_SECRET`, `PICKER_MODEL_PATH`.
3. **Bootstrap services (docker-compose excerpt)**:
   ```yaml
   version: '3.8'
   services:
     zookeeper:
       image: bitnami/zookeeper:3
       environment:
         ZOO_ENABLE_AUTH: 'no'
     kafka:
       image: bitnami/kafka:3
       environment:
         KAFKA_CFG_ZOOKEEPER_CONNECT: zookeeper:2181
         ALLOW_PLAINTEXT_LISTENER: 'yes'
       ports: ["9092:9092"]
     minio:
       image: minio/minio
       command: server /data
       environment:
         MINIO_ACCESS_KEY: seismox
         MINIO_SECRET_KEY: seismox123
       ports: ["9000:9000"]
     clickhouse:
       image: clickhouse/clickhouse-server:23
       ports: ["8123:8123", "9009:9009"]
     jobmanager:
       image: flink:1.18-scala_2.12
       command: jobmanager
       environment:
         - JOB_MANAGER_RPC_ADDRESS=jobmanager
       ports: ["8081:8081"]
     taskmanager:
       image: flink:1.18-scala_2.12
       command: taskmanager
       environment:
         - JOB_MANAGER_RPC_ADDRESS=jobmanager
       depends_on: [jobmanager]
   ```
4. **Run services**: `docker-compose up -d`; deploy Flink/Spark jobs for QC, picking fanout, and association routing.
5. **Configure topics**: create Kafka topics for `waveform.raw`, `waveform.qc`, `phase.pick`, `event.assoc`, `event.final` with replication as needed.
6. **Deploy microservices**: containerize picker, association, location, magnitude, and web API; include health checks and DLQ bindings.
7. **Access web UI**: point the frontend to the API gateway; ensure map baselayer keys and authentication are configured.

## Operational Notes
- **Backfill**: use batch jobs (Spark) to import historical mseed files, create picks via offline picker runs, and load catalog tables.
- **Fault tolerance**: use checkpointed Flink jobs, Kafka consumer groups with idempotent producers, and DLQ topics for malformed payloads.
- **Security**: enforce TLS on external endpoints, rotate API keys/JWT secrets, and store credentials via vault/secret manager.
- **Testing**: include synthetic waveform generators to validate end-to-end latency and detection quality before connecting live feeds.

## Contact
Questions or collaboration: **caiyuqiming@163.com**
