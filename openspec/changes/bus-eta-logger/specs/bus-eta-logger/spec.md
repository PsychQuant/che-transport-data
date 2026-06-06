## ADDED Requirements

### Requirement: Continuous Dual-Signal Capture

The logger SHALL continuously poll TDX Greater-Taipei (Taipei + NewTaipei) per-city bulk endpoints, capturing A2 `RealTimeNearStop` arrive/depart events as the primary arrival-truth signal and N1 `EstimatedTimeOfArrival` as a comparison baseline, at independent cadences where A2 is polled more frequently than N1.

#### Scenario: Poll cycle persists all returned records

- **WHEN** a poll cycle fires for a city
- **THEN** the logger requests that city's A2 bulk feed (and, on its slower schedule, the N1 bulk feed) and persists every returned record

#### Scenario: A2 polled more often than N1

- **WHEN** A2 cadence is approximately 30 seconds and N1 cadence is approximately 60 to 120 seconds
- **THEN** within any given minute the logger issues more A2 requests than N1 requests

#### Scenario: Transient transport failure skips the cycle without terminating

- **WHEN** a transport-level error (connection failure or timeout) occurs while fetching a feed
- **THEN** the logger retries once and, if it recurs, skips that fetch for the cycle without terminating the process, so a transient network blip does not crash the resident daemon

### Requirement: Full-Fidelity Raw Field Capture

The logger SHALL request TDX raw endpoints directly and persist all fields required to reconstruct arrival events, including A2EventType, PlateNumb, StopUID, StopSequence, GPSTime, and vehicle position. The logger MUST NOT rely on a lossy intermediate model that drops these fields.

#### Scenario: A2 record retains arrival-reconstruction fields

- **WHEN** an A2 record is captured
- **THEN** the stored row contains plate, stop_uid, direction, stop_sequence, event_type, gps_time, latitude, longitude, and captured_at

### Requirement: Arrival-Event Deduplication

The logger SHALL collapse repeated A2 reports of the same (plate, stop_uid, direction) occurring within a 90-second window into a single arrival event, retaining the earliest GPSTime.

#### Scenario: Repeated reports collapse to one event

- **WHEN** the same plate is reported at the same stop and direction multiple times within 90 seconds
- **THEN** exactly one arrival event is recorded, carrying the earliest GPSTime

##### Example: Three reports within the window

| Captured records (plate, stop, dir, gps_time) | Stored arrival events |
| --------------------------------------------- | --------------------- |
| (EAL-5200, S123, 0, 09:24:01), (EAL-5200, S123, 0, 09:24:31), (EAL-5200, S123, 0, 09:24:58) | 1 event: (EAL-5200, S123, 0, gps_time=09:24:01) |

### Requirement: BCNF and SCD Type-2 Storage Schema

Persisted data SHALL follow a thin-fact plus dimension schema in Boyce–Codd Normal Form. Dimension tables for route, stop, vehicle, and route-stop bridge SHALL use Slowly-Changing-Dimension Type-2 versioning with valid_from, valid_to, and is_current columns. Fact tables SHALL store natural foreign keys plus measurements only and MUST NOT embed descriptive names such as stop_name or route_name.

#### Scenario: Fact rows hold foreign keys, not names

- **WHEN** an arrival event is stored
- **THEN** it references stop_uid, route_uid, and plate as foreign keys and contains no stop_name or route_name column

#### Scenario: Dimension change creates a new version

- **WHEN** a stop's name or position differs from its current dimension row during a skeleton refresh
- **THEN** a new dimension version row is inserted with a fresh valid_from and the prior row's valid_to is closed and its is_current set false

##### Example: As-of join selects the version current at event time

- **GIVEN** dim_stop has version A (valid 2026-01-01 to 2026-05-01) and version B (valid 2026-05-01 onward) for stop S123
- **WHEN** an arrival event at S123 with event_ts 2026-03-10 is joined to dim_stop
- **THEN** it resolves to version A, because 2026-03-10 falls in version A's [valid_from, valid_to) interval

### Requirement: Partitioned Parquet on Designated External Storage

Fact data SHALL be written as Parquet partitioned by city and date under the canonical storage root on the mini-che external NVMe. The logger SHALL refuse to write when the external volume is not mounted and MUST NOT fall back to the system disk.

#### Scenario: Partition layout by city and date

- **WHEN** arrival events are written
- **THEN** they are stored under a path of the form city=<code>/date=<YYYY-MM-DD>/

#### Scenario: Unmounted external volume blocks writes

- **WHEN** the external NVMe volume is not mounted
- **THEN** the logger records an error and writes nothing, and MUST NOT write to the boot drive

### Requirement: Unrecoverable-Gap Recording

Because TDX retains only a rolling snapshot of roughly two hours, any logger downtime produces an unrecoverable data gap. The logger SHALL record a gap marker identifying the missing time range so downstream analysis distinguishes "no service" from "not logged".

#### Scenario: Restart records a gap marker

- **WHEN** the logger resumes after a period of downtime
- **THEN** a gap marker covering the missing interval is recorded

### Requirement: Rate-Limit Adherence and Feasibility Budget

The logger SHALL operate within the applicable TDX request limit (free tier is 50 requests per minute). Before continuous operation, a feasibility spike SHALL measure per-cycle A2 and N1 record counts and TDX pagination ($top) behavior for both Taipei and NewTaipei, and produce a request budget with chosen cadences that fit within the limit.

#### Scenario: Chosen budget fits the limit

- **WHEN** the spike computes the per-cycle request count at the chosen A2 and N1 cadences
- **THEN** the computed steady-state request rate is at most the applicable TDX rate limit

#### Scenario: Rate-limit response handling

- **WHEN** TDX returns HTTP 429
- **THEN** the logger performs a single backoff retry, and on a second 429 it records the event and skips the cycle without terminating the process

### Requirement: Capture-Feasibility Metrics

The logger SHALL emit metrics sufficient to evaluate full-coverage capture over a multi-day run, including coverage percentage (cycles with data divided by expected cycles), total gap minutes, and deduplication counts.

#### Scenario: Multi-day run reports coverage

- **WHEN** a multi-day capture run completes
- **THEN** coverage percentage, total gap minutes, and deduplication counts are reported
