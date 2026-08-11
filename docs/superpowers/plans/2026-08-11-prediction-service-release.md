# Prediction Service Checkout Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make one clean local Git checkout an independently identifiable, installable, stoppable, upgradable, and compatibly rollbackable production Prediction Service on `127.0.0.1:8769` without performing the live traffic cutover.

**Architecture:** Extend the existing Prediction Runtime, SQLite Store, health endpoint, launchd label, installer, and uninstaller. The service validates a tracked release manifest before touching data; the production Runtime acquires its existing owner lock and then performs one read-only SQLite generation check before constructing the writable Store or any client/thread. The existing launchd scripts perform a planned-downtime handoff and atomically write observed runtime evidence; the selected release is the clean checkout itself, not an archive or registry entry.

**Tech Stack:** Python 3 standard library (`dataclasses`, `json`, `sqlite3`, `tempfile`, `os.replace`), Bash, macOS launchd/plutil/lsof, pytest.

## Global Constraints

- A release is a clean local Git checkout at one exact Git SHA; do not create an archive, copied application tree, package repository, or release registry.
- Planned downtime is allowed; do not add dual writers, shared writable SQLite, leader election, rolling upgrade, or hot handoff.
- Use the existing launchd label `com.open-trader.prediction-service` and loopback listener `127.0.0.1:8769`.
- The tracked manifest schema is exactly `open_trader.prediction_service.release.v1`; `reader_generation` and `contract_generation` are positive integers and start at `1`.
- The runtime-record schema is exactly `open_trader.prediction_service.runtime.v1`; allowed states are `maintenance`, `ready`, `failed`, and `stopped`.
- The candidate process owns the authoritative compatibility decision: acquire the production owner lock non-blockingly, read `minimum_reader_generation` once through SQLite URI `mode=ro`, close it, and only then construct the writable Store.
- An incompatible reader must create no writable connection, exchange client, execution service, monitor, or background thread, and must release the owner lock.
- Unknown listener, label, PID, cwd, owner, health identity, wrong SHA, dirty checkout, invalid manifest, or unproven shutdown fails closed without killing an unknown process.
- Failed install or rollback remains non-ready and does not automatically restart the previous checkout.
- Issue #44 must not change Gateway routing, stop the live Legacy owner, install the real production launchd job, submit real orders, or change Prediction API/strategy/solver/notification/Dashboard behavior.
- Preserve the existing Shadow installer behavior while adding explicit production release mode.
- Verification uses temporary runtime roots, temporary SQLite databases, fake exchange clients, and fake launchd/process commands. Do not run live launchd mutation or `make acceptance`; #44 has no Dashboard/Gateway/UI/deployment change.

## File Map

- Create `ops/prediction-service-release.json`: tracked reader/HTTP contract generation identity.
- Create `src/open_trader/prediction_release.py`: strict manifest parsing plus validated atomic runtime-record reads/writes; no service manager abstraction.
- Modify `src/open_trader/prediction_arbitrage_store.py`: one read-only minimum-generation probe and the singleton metadata row created by the normal writable Store.
- Modify `src/open_trader/prediction_runtime.py`: optional release-reader gate on the production path, directly after the existing owner lock.
- Modify `src/open_trader/prediction_service.py`: production manifest requirement, Runtime wiring, CLI flag, and generation fields in health.
- Modify `ops/launchd/com.open-trader.prediction-service.plist.template`: render mode and exact manifest path.
- Modify `scripts/install_prediction_service_launchd.sh`: retain the Shadow path; add production preflight, downtime handoff, candidate proof, rollback, and atomic evidence.
- Modify `scripts/uninstall_prediction_service_launchd.sh`: verify managed identity and owner absence, then preserve evidence as `stopped`.
- Create `tests/test_prediction_release.py`: manifest and runtime-record contract tests.
- Modify `tests/test_prediction_arbitrage_store.py`: read-only generation and bootstrap tests.
- Modify `tests/test_prediction_runtime.py`: locked generation ordering, compatible start, and fail-before-resource tests.
- Modify `tests/test_prediction_service.py`: production manifest and health identity tests.
- Modify `tests/test_prediction_service_launchd.py`: preserve Shadow rendering/install/uninstall behavior with the parameterized template.
- Create `tests/test_prediction_release_launchd.py`: production installer, rollback, failure, and uninstall workflow tests using fake commands only.
- Modify `CHANGELOG.md`: dated operator-facing #44 entry after behavior is verified and before merge.

---

### Task 1: Define the tracked release and runtime-record contracts

**Files:**
- Create: `ops/prediction-service-release.json`
- Create: `src/open_trader/prediction_release.py`
- Create: `tests/test_prediction_release.py`

**Interfaces:**
- Consumes: `Path`, JSON objects, and the existing filesystem; no Runtime or Store import.
- Produces: `PredictionReleaseManifest`, `load_prediction_release_manifest(path: Path) -> PredictionReleaseManifest`, `load_prediction_runtime_record(path: Path) -> dict[str, object] | None`, and `write_prediction_runtime_record(path: Path, payload: Mapping[str, object]) -> None`.

- [ ] **Step 1: Write strict manifest tests**

Create `tests/test_prediction_release.py` with the exact valid payload and rejection matrix:

```python
from __future__ import annotations

import json
from pathlib import Path

import pytest

from open_trader.prediction_release import (
    load_prediction_release_manifest,
    load_prediction_runtime_record,
    write_prediction_runtime_record,
)


def test_tracked_prediction_release_manifest_is_generation_one() -> None:
    root = Path(__file__).resolve().parents[1]
    release = load_prediction_release_manifest(
        root / "ops" / "prediction-service-release.json"
    )

    assert release.schema_version == "open_trader.prediction_service.release.v1"
    assert release.reader_generation == 1
    assert release.contract_generation == 1


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"schema_version": "wrong", "reader_generation": 1, "contract_generation": 1},
        {"schema_version": "open_trader.prediction_service.release.v1", "reader_generation": 0, "contract_generation": 1},
        {"schema_version": "open_trader.prediction_service.release.v1", "reader_generation": True, "contract_generation": 1},
        {"schema_version": "open_trader.prediction_service.release.v1", "reader_generation": 1, "contract_generation": 1, "extra": 1},
    ],
)
def test_release_manifest_rejects_wrong_shape(tmp_path: Path, payload: object) -> None:
    path = tmp_path / "release.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="prediction release manifest"):
        load_prediction_release_manifest(path)
```

- [ ] **Step 2: Write atomic runtime-record tests**

Append tests that prove schema/state validation, replacement, and prior-release preservation:

```python
def test_runtime_record_is_atomically_replaced_and_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "prediction-service-runtime.json"
    previous = {"git_sha": "old", "reader_generation": 1, "contract_generation": 1}
    write_prediction_runtime_record(path, {
        "state": "maintenance",
        "candidate": {"git_sha": "new", "checkout": "/tmp/new", "source_state": "clean", "reader_generation": 1, "contract_generation": 1},
        "previous_release": previous,
        "transition_started_at": "2026-08-11T10:00:00+08:00",
        "updated_at": "2026-08-11T10:00:00+08:00",
        "failure_reason": "",
    })
    first_inode = path.stat().st_ino
    write_prediction_runtime_record(path, {
        "state": "failed",
        "candidate": {"git_sha": "new", "checkout": "/tmp/new", "source_state": "clean", "reader_generation": 1, "contract_generation": 1},
        "previous_release": previous,
        "transition_started_at": "2026-08-11T10:00:00+08:00",
        "updated_at": "2026-08-11T10:01:00+08:00",
        "failure_reason": "candidate_not_ready",
    })

    record = load_prediction_runtime_record(path)
    assert record is not None
    assert record["schema_version"] == "open_trader.prediction_service.runtime.v1"
    assert record["state"] == "failed"
    assert record["previous_release"] == previous
    assert path.stat().st_ino != first_inode
    assert list(path.parent.glob(f".{path.name}.*.tmp")) == []


def test_runtime_record_rejects_unknown_state_and_schema(tmp_path: Path) -> None:
    path = tmp_path / "prediction-service-runtime.json"
    with pytest.raises(ValueError, match="runtime state"):
        write_prediction_runtime_record(path, {"state": "starting"})
    path.write_text('{"schema_version":"wrong","state":"ready"}', encoding="utf-8")
    with pytest.raises(ValueError, match="runtime record"):
        load_prediction_runtime_record(path)
```

- [ ] **Step 3: Run the RED test**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_prediction_release.py
```

Expected: collection fails with `ModuleNotFoundError: No module named 'open_trader.prediction_release'`.

- [ ] **Step 4: Add the exact tracked manifest**

Create `ops/prediction-service-release.json`:

```json
{
  "schema_version": "open_trader.prediction_service.release.v1",
  "reader_generation": 1,
  "contract_generation": 1
}
```

- [ ] **Step 5: Implement the minimal parser and atomic record writer**

Create `src/open_trader/prediction_release.py` with these contracts. Keep exact-key checking in the manifest parser; `type(value) is int` rejects booleans. The runtime writer supplies the fixed schema, validates only the four allowed states, flushes and fsyncs the sibling temporary file, and replaces the destination with `os.replace`.

```python
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Mapping


RELEASE_SCHEMA = "open_trader.prediction_service.release.v1"
RUNTIME_SCHEMA = "open_trader.prediction_service.runtime.v1"
RUNTIME_STATES = {"maintenance", "ready", "failed", "stopped"}


@dataclass(frozen=True)
class PredictionReleaseManifest:
    schema_version: str
    reader_generation: int
    contract_generation: int


def load_prediction_release_manifest(path: Path) -> PredictionReleaseManifest:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"prediction release manifest is unreadable: {path}") from exc
    required = {"schema_version", "reader_generation", "contract_generation"}
    if not isinstance(payload, dict) or set(payload) != required:
        raise ValueError("prediction release manifest has invalid keys")
    if payload["schema_version"] != RELEASE_SCHEMA:
        raise ValueError("prediction release manifest has invalid schema")
    for key in ("reader_generation", "contract_generation"):
        if type(payload[key]) is not int or payload[key] < 1:
            raise ValueError(f"prediction release manifest has invalid {key}")
    return PredictionReleaseManifest(
        schema_version=RELEASE_SCHEMA,
        reader_generation=payload["reader_generation"],
        contract_generation=payload["contract_generation"],
    )


def load_prediction_runtime_record(path: Path) -> dict[str, object] | None:
    path = Path(path)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"prediction runtime record is unreadable: {path}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != RUNTIME_SCHEMA:
        raise ValueError("prediction runtime record has invalid schema")
    if payload.get("state") not in RUNTIME_STATES:
        raise ValueError("prediction runtime record has invalid state")
    return payload


def write_prediction_runtime_record(
    path: Path, payload: Mapping[str, object]
) -> None:
    state = payload.get("state")
    if state not in RUNTIME_STATES:
        raise ValueError("prediction runtime state is invalid")
    record = {"schema_version": RUNTIME_SCHEMA, **dict(payload)}
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = ""
    try:
        with NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temporary = handle.name
            json.dump(record, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = ""
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)
```

- [ ] **Step 6: Run GREEN and commit**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_prediction_release.py
git diff --check
git add -f ops/prediction-service-release.json src/open_trader/prediction_release.py tests/test_prediction_release.py
git commit -m "feat: define prediction release identity (#44)"
```

Expected: all `tests/test_prediction_release.py` tests pass and the diff check has no output.

---

### Task 2: Add the persistent reader-generation gate to the Store

**Files:**
- Modify: `src/open_trader/prediction_arbitrage_store.py:267-315,491-548`
- Modify: `tests/test_prediction_arbitrage_store.py`

**Interfaces:**
- Consumes: the existing data-directory layout `<data-dir>/prediction_arbitrage/prediction_arbitrage.sqlite3`.
- Produces: `read_minimum_reader_generation(data_dir: Path) -> int`; the normal `PredictionArbitrageStore` persists singleton `schema_metadata.minimum_reader_generation = 1` without lowering an existing higher value.

- [ ] **Step 1: Write read-only and bootstrap tests**

Append these focused cases to `tests/test_prediction_arbitrage_store.py`:

```python
def test_missing_database_has_baseline_reader_generation_without_creation(tmp_path: Path) -> None:
    from open_trader.prediction_arbitrage_store import read_minimum_reader_generation

    database = tmp_path / "prediction_arbitrage" / "prediction_arbitrage.sqlite3"
    assert read_minimum_reader_generation(tmp_path) == 1
    assert not database.exists()


def test_missing_metadata_table_reads_baseline_without_mutating_database(tmp_path: Path) -> None:
    from open_trader.prediction_arbitrage_store import read_minimum_reader_generation

    database = tmp_path / "prediction_arbitrage" / "prediction_arbitrage.sqlite3"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE existing(value INTEGER)")
    before = database.read_bytes()

    assert read_minimum_reader_generation(tmp_path) == 1
    assert database.read_bytes() == before
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name='schema_metadata'"
        ).fetchone() is None


def test_store_persists_baseline_and_probe_reads_future_minimum(tmp_path: Path) -> None:
    from open_trader.prediction_arbitrage_store import read_minimum_reader_generation

    PredictionArbitrageStore(tmp_path)
    database = tmp_path / "prediction_arbitrage" / "prediction_arbitrage.sqlite3"
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT minimum_reader_generation FROM schema_metadata WHERE singleton=1"
        ).fetchone() == (1,)
        connection.execute(
            "UPDATE schema_metadata SET minimum_reader_generation=2 WHERE singleton=1"
        )

    assert read_minimum_reader_generation(tmp_path) == 2
    PredictionArbitrageStore(tmp_path)
    assert read_minimum_reader_generation(tmp_path) == 2


def test_reader_probe_closes_its_mode_ro_connection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import open_trader.prediction_arbitrage_store as store_module

    PredictionArbitrageStore(tmp_path)
    real_connect = sqlite3.connect
    observed: dict[str, object] = {}

    class RecordingConnection:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self.connection = connection
        def execute(self, statement: str, parameters: tuple[object, ...] = ()):
            return self.connection.execute(statement, parameters)
        def close(self) -> None:
            observed["closed"] = True
            self.connection.close()

    def connect(uri: str, **kwargs: object) -> RecordingConnection:
        observed.update(uri=uri, kwargs=kwargs)
        return RecordingConnection(real_connect(uri, **kwargs))

    monkeypatch.setattr(store_module.sqlite3, "connect", connect)
    assert store_module.read_minimum_reader_generation(tmp_path) == 1
    assert str(observed["uri"]).endswith("?mode=ro")
    assert observed["kwargs"] == {"uri": True}
    assert observed["closed"] is True


def test_existing_metadata_table_without_singleton_fails_closed(tmp_path: Path) -> None:
    from open_trader.prediction_arbitrage_store import read_minimum_reader_generation

    database = tmp_path / "prediction_arbitrage" / "prediction_arbitrage.sqlite3"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE schema_metadata(singleton INTEGER PRIMARY KEY, minimum_reader_generation INTEGER NOT NULL)"
        )
    with pytest.raises(ValueError, match="generation is missing"):
        read_minimum_reader_generation(tmp_path)
```

- [ ] **Step 2: Run RED**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_prediction_arbitrage_store.py -k 'reader_generation or metadata_table'
```

Expected: import failures for `read_minimum_reader_generation`.

- [ ] **Step 3: Implement the direct read-only probe**

Add a module-level function before `PredictionArbitrageStore`. It must query `sqlite_master` first so only an actually missing table receives the baseline; corruption and other SQLite errors must propagate.

```python
def read_minimum_reader_generation(data_dir: Path) -> int:
    path = Path(data_dir) / "prediction_arbitrage" / "prediction_arbitrage.sqlite3"
    if not path.exists():
        return 1
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_metadata'"
        ).fetchone()
        if table is None:
            return 1
        row = connection.execute(
            "SELECT minimum_reader_generation FROM schema_metadata WHERE singleton=1"
        ).fetchone()
        if row is None:
            raise ValueError("prediction minimum reader generation is missing")
        generation = row[0]
        if type(generation) is not int or generation < 1:
            raise ValueError("prediction minimum reader generation is invalid")
        return generation
    finally:
        connection.close()
```

- [ ] **Step 4: Persist the baseline through normal schema creation**

Inside `_create_schema`, add the table and a non-lowering insert:

```sql
CREATE TABLE IF NOT EXISTS schema_metadata (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    minimum_reader_generation INTEGER NOT NULL
        CHECK (minimum_reader_generation >= 1)
);

INSERT INTO schema_metadata(singleton, minimum_reader_generation)
VALUES (1, 1)
ON CONFLICT(singleton) DO NOTHING;
```

Advance `PRAGMA user_version` from `6` to `7` only after this schema block succeeds:

```python
if version < 7:
    connection.execute("PRAGMA user_version=7")
```

- [ ] **Step 5: Run GREEN, the full Store suite, and commit**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_prediction_arbitrage_store.py -k 'reader_generation or metadata_table'
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_prediction_arbitrage_store.py
git diff --check
git add src/open_trader/prediction_arbitrage_store.py tests/test_prediction_arbitrage_store.py
git commit -m "feat: persist prediction reader generation (#44)"
```

Expected: focused and complete Store suites pass; no database is created by the missing-database probe.

---

### Task 3: Enforce the locked generation gate and publish release health

**Files:**
- Modify: `src/open_trader/prediction_runtime.py:319-470,655-728`
- Modify: `src/open_trader/prediction_service.py:22-29,268-295,549-622`
- Modify: `tests/test_prediction_runtime.py`
- Modify: `tests/test_prediction_service.py`

**Interfaces:**
- Consumes: `load_prediction_release_manifest`, `read_minimum_reader_generation`, and existing `_RuntimeOwnershipLock`.
- Produces: `PredictionRuntime(..., reader_generation: int | None = None)`; `PredictionRuntimeCompatibilityError`; `serve_prediction_service(..., release_manifest_path: Path | None = None)`; CLI `--release-manifest PATH`; health fields `release_schema_version`, `reader_generation`, and `contract_generation`.
- Compatibility: `reader_generation=None` retains the Legacy Runtime path until Issue #45; the production `prediction-service` entry point requires a manifest.

- [ ] **Step 1: Write Runtime ordering and refusal tests**

Extend the existing `test_runtime_owns_startup_and_shutdown_order` fake-resource test rather than creating another fake factory. Add the probe and owner events to that test:

```python
original_acquire = runtime_module._RuntimeOwnershipLock.acquire
original_release = runtime_module._RuntimeOwnershipLock.release

def acquire(lock: object) -> None:
    original_acquire(lock)  # type: ignore[arg-type]
    events.append("owner.acquire")

def release(lock: object) -> None:
    original_release(lock)  # type: ignore[arg-type]
    events.append("owner.release")

monkeypatch.setattr(runtime_module._RuntimeOwnershipLock, "acquire", acquire)
monkeypatch.setattr(runtime_module._RuntimeOwnershipLock, "release", release)
monkeypatch.setattr(
    runtime_module,
    "read_minimum_reader_generation",
    lambda _data_dir: events.append("generation.read") or 1,
)

# In FakeStore.__init__, before its current body:
events.append("store.construct")

# In FakeTradingClient.from_keychain, before returning FakeTrading:
events.append("client.construct")

# Add this keyword to the existing PredictionRuntime construction:
reader_generation=1,

# Add these assertions after runtime.start():
assert events.index("owner.acquire") < events.index("generation.read")
assert events.count("generation.read") == 1
assert events.index("generation.read") < events.index("store.construct")
assert events.index("store.construct") < events.index("client.construct")
assert events.index("reconcile") < events.index("polymarket.start")

# Add this assertion after runtime.stop():
assert events.index("store.close") < events.index("owner.release")
```

Add the incompatible case with counters rather than a broad mock:

```python
def test_incompatible_release_stops_before_writable_resources_and_releases_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import open_trader.prediction_runtime as runtime_module

    constructed: list[str] = []
    probes: list[Path] = []
    monkeypatch.setattr(
        runtime_module,
        "read_minimum_reader_generation",
        lambda path: probes.append(path) or 2,
    )
    monkeypatch.setattr(
        runtime_module,
        "PredictionArbitrageStore",
        lambda _path: constructed.append("store") or object(),
    )
    monkeypatch.setattr(
        runtime_module.PolymarketTradingClient,
        "from_keychain",
        lambda _config: constructed.append("client") or object(),
    )
    runtime = PredictionRuntime(
        data_dir=tmp_path,
        prediction_config_path=tmp_path / "prediction.json",
        dashboard_url="http://127.0.0.1:8769",
        reader_generation=1,
    )

    with pytest.raises(
        runtime_module.PredictionRuntimeCompatibilityError,
        match="reader generation 1 is below required 2",
    ):
        runtime.start()

    assert constructed == []
    assert probes == [tmp_path]
    assert runtime.production_owner is False
    probe = runtime_module._RuntimeOwnershipLock(
        tmp_path / "prediction_arbitrage" / "runtime.lock"
    )
    probe.acquire()
    probe.release()
```

Also add `test_legacy_runtime_without_release_generation_keeps_existing_start_path` to assert that `read_minimum_reader_generation` is not called when `reader_generation=None`.

```python
def test_legacy_runtime_without_release_generation_skips_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import open_trader.prediction_runtime as runtime_module

    class FakeStore:
        def __init__(self, _data_dir: Path) -> None:
            pass
        def close(self) -> None:
            pass

    monkeypatch.setattr(
        runtime_module,
        "read_minimum_reader_generation",
        lambda _path: (_ for _ in ()).throw(AssertionError("generation probed")),
    )
    monkeypatch.setattr(runtime_module, "PredictionArbitrageStore", FakeStore)
    monkeypatch.setattr(
        runtime_module,
        "load_trading_config",
        lambda _path: (_ for _ in ()).throw(RuntimeError("config reached")),
    )
    runtime = PredictionRuntime(
        data_dir=tmp_path,
        prediction_config_path=tmp_path / "prediction.json",
        dashboard_url="http://127.0.0.1:8766",
    )

    with pytest.raises(RuntimeError, match="config reached"):
        runtime.start()
    runtime.stop()
```

- [ ] **Step 2: Write service manifest/health tests**

Add one new pre-construction test, extend the existing production bind-failure test to capture release data, and extend the existing Shadow health test:

```python
def test_production_service_requires_release_manifest_before_runtime_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import open_trader.prediction_service as service

    monkeypatch.setattr(
        service, "PredictionRuntime",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("runtime constructed")),
    )
    with pytest.raises(ValueError, match="release manifest is required"):
        service.serve_prediction_service(
            data_dir=tmp_path,
            prediction_config_path=tmp_path / "prediction.json",
            port=0,
            mode="production",
        )


# In test_production_bind_failure_stops_runtime_and_uses_one_metadata_snapshot,
# add this argument to serve_prediction_service:
release_manifest_path=Path(__file__).resolve().parents[1]
    / "ops" / "prediction-service-release.json",

# Keep fail_bind's existing assertion and add:
assert kwargs["runtime_metadata"] == {
    **metadata,
    "release_schema_version": "open_trader.prediction_service.release.v1",
    "reader_generation": 1,
    "contract_generation": 1,
}

# After the existing exception assertion:
assert instances[0].kwargs["reader_generation"] == 1

# In test_shadow_health_has_the_read_only_identity, append:
assert "release_schema_version" not in payload
assert "reader_generation" not in payload
assert "contract_generation" not in payload
```

Use `ops/prediction-service-release.json` for the production test; do not manufacture a second valid contract.

- [ ] **Step 3: Run RED**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_prediction_runtime.py tests/test_prediction_service.py -k 'release or reader_generation'
```

Expected: constructor/keyword/import failures because the release gate is not wired yet.

- [ ] **Step 4: Implement the Runtime gate at the one correct seam**

Import the Store probe and add the exception:

```python
from .prediction_arbitrage_store import (
    PredictionArbitrageStore,
    read_minimum_reader_generation,
)


class PredictionRuntimeCompatibilityError(RuntimeError):
    pass
```

Add `reader_generation: int | None = None` to `PredictionRuntime.__init__`, reject non-positive/bool values, and store it. In the production `start()` path insert only this block between `self._owner.acquire()` and `PredictionArbitrageStore(...)`:

```python
self._owner.acquire()
if self._reader_generation is not None:
    minimum_reader_generation = read_minimum_reader_generation(self._data_dir)
    if self._reader_generation < minimum_reader_generation:
        raise PredictionRuntimeCompatibilityError(
            f"prediction reader generation {self._reader_generation} "
            f"is below required {minimum_reader_generation}"
        )
self.store = PredictionArbitrageStore(self._data_dir)
```

Leave `_cleanup_resources()` as the single owner-release path. Export `PredictionRuntimeCompatibilityError` from `__all__`.

- [ ] **Step 5: Validate and publish the same manifest in the service process**

In `serve_prediction_service`, before Runtime construction:

```python
release = None
if mode == "production":
    if release_manifest_path is None:
        raise ValueError("production release manifest is required")
    release = load_prediction_release_manifest(release_manifest_path)
metadata = _runtime_metadata()
if release is not None:
    metadata.update({
        "release_schema_version": release.schema_version,
        "reader_generation": release.reader_generation,
        "contract_generation": release.contract_generation,
    })
runtime = PredictionRuntime(
    data_dir=Path(data_dir),
    prediction_config_path=Path(prediction_config_path),
    dashboard_url=f"http://{host}:{port}",
    mode=mode,
    git_sha=str(metadata.get("git_sha", "")),
    reader_generation=None if release is None else release.reader_generation,
)
```

Add `release_manifest_path: Path | None = None` to the function signature, `parser.add_argument("--release-manifest", type=Path)`, and pass it from `main`. Do not add a second health schema; the existing `**metadata` publishes the three fields only for a release-backed production process.

- [ ] **Step 6: Run GREEN and regression, then commit**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_prediction_runtime.py tests/test_prediction_service.py -k 'release or reader_generation'
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_prediction_runtime.py tests/test_prediction_service.py tests/test_prediction_api_contract.py
git diff --check
git add src/open_trader/prediction_runtime.py src/open_trader/prediction_service.py tests/test_prediction_runtime.py tests/test_prediction_service.py
git commit -m "feat: gate production prediction releases (#44)"
```

Expected: focused and regression suites pass; Legacy-without-release and Shadow behavior remain green.

---

### Task 4: Add the managed production install, upgrade, and rollback flow

**Files:**
- Modify: `ops/launchd/com.open-trader.prediction-service.plist.template`
- Modify: `scripts/install_prediction_service_launchd.sh`
- Modify: `tests/test_prediction_service_launchd.py`
- Create: `tests/test_prediction_release_launchd.py`

**Interfaces:**
- Consumes: tracked manifest, `python -c` imports from `open_trader.prediction_release`, existing launchd label, existing `/healthz`, `_RuntimeOwnershipLock`, and fakeable `LAUNCHCTL_BIN`, `LSOF_BIN`, `CURL_BIN`, `PS_BIN`, `OWNER_PROBE_BIN`.
- Produces: installer flags `--mode shadow|production` (default `shadow`), `--release-manifest PATH`, and `--expected-sha SHA`; atomic `<runtime-root>/prediction-service-runtime.json`; exact observed `ready` evidence.

- [ ] **Step 1: Preserve Shadow rendering while making mode/manifest explicit**

First change the existing template test to expect placeholders:

```python
assert payload["ProgramArguments"] == [
    "OPEN_TRADER_PYTHON", "-m", "open_trader", "prediction-service",
    "--mode", "OPEN_TRADER_PREDICTION_MODE",
    "--data-dir", "OPEN_TRADER_DATA_DIR",
    "--config", "OPEN_TRADER_PREDICTION_CONFIG",
    "--host", "127.0.0.1", "--port", "8769",
    "--release-manifest", "OPEN_TRADER_RELEASE_MANIFEST",
]
```

Update the Shadow dry-run assertion to contain `--mode shadow` and the canonical tracked manifest path. This is the only intended Shadow output change.

- [ ] **Step 2: Write production preflight refusal tests**

In `tests/test_prediction_release_launchd.py`, copy a minimal clean Git checkout containing the installer, uninstaller, template, manifest, and `src/open_trader`. Use these concrete test-only types so later tests do not depend on production helpers:

```python
from dataclasses import dataclass
import json
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from open_trader.prediction_release import load_prediction_runtime_record


ROOT = Path(__file__).resolve().parents[1]
LABEL = "com.open-trader.prediction-service"


@dataclass(frozen=True)
class ReleaseCheckout:
    path: Path
    sha: str
    reader_generation: int = 1
    contract_generation: int = 1


class CommandCalls:
    def __init__(self, path: Path) -> None:
        self.path = path

    def all(self) -> list[str]:
        if not self.path.exists():
            return []
        return self.path.read_text(encoding="utf-8").splitlines()

    def named(self, command: str) -> list[str]:
        return [line for line in self.all() if line.split(" ", 1)[0] == command]

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


class ReleaseHarness:
    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path
        self.runtime_root = tmp_path / "runtime"
        self.agents = tmp_path / "LaunchAgents"
        self.agents.mkdir()
        self.state_path = tmp_path / "fake-state.json"
        self.calls = CommandCalls(tmp_path / "fake-calls.log")
        self.fake_bin = tmp_path / "fake-bin"
        self.fake_bin.mkdir()
        self._write_dispatcher()
        self.candidate = self.make_checkout("candidate")
        self.configure("absent")

    def make_checkout(self, name: str) -> ReleaseCheckout:
        path = self.root / name
        (path / "scripts").mkdir(parents=True)
        (path / "ops" / "launchd").mkdir(parents=True)
        (path / "src").mkdir(parents=True)
        shutil.copytree(ROOT / "src" / "open_trader", path / "src" / "open_trader")
        for script in (
            "install_prediction_service_launchd.sh",
            "uninstall_prediction_service_launchd.sh",
        ):
            shutil.copy2(ROOT / "scripts" / script, path / "scripts" / script)
        shutil.copy2(
            ROOT / "ops" / "launchd" / f"{LABEL}.plist.template",
            path / "ops" / "launchd" / f"{LABEL}.plist.template",
        )
        shutil.copy2(
            ROOT / "ops" / "prediction-service-release.json",
            path / "ops" / "prediction-service-release.json",
        )
        subprocess.run(["git", "init", "-q", str(path)], check=True)
        subprocess.run(["git", "-C", str(path), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(path), "-c", "user.name=test", "-c",
             "user.email=test@example.com", "commit", "-qm", name],
            check=True,
        )
        sha = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        return ReleaseCheckout(path=path, sha=sha)

    def _write_dispatcher(self) -> None:
        dispatcher = self.fake_bin / "fake-command"
        dispatcher.write_text(FAKE_COMMAND_SOURCE, encoding="utf-8")
        dispatcher.chmod(0o755)
        for name in ("launchctl", "lsof", "curl", "ps", "owner-probe"):
            (self.fake_bin / name).symlink_to(dispatcher)

    def configure(self, case: str) -> None:
        state = {
            "case": case,
            "loaded": False,
            "pid": 0,
            "cwd": "",
            "listener": False,
            "owner_available": True,
            "health": {},
            "max_pids": 0,
        }
        if case == "unknown_listener":
            state.update(pid=9999, cwd="/tmp/unknown", listener=True)
        if case == "unknown_label_identity":
            state.update(loaded=True, pid=9999, cwd="/tmp/unknown", listener=True)
        if case == "unknown_owner":
            state["owner_available"] = False
        self.state_path.write_text(json.dumps(state), encoding="utf-8")

    def _env(self, checkout: ReleaseCheckout) -> dict[str, str]:
        return {
            **os.environ,
            "PYTHONPATH": str(checkout.path / "src"),
            "LAUNCHCTL_BIN": str(self.fake_bin / "launchctl"),
            "LSOF_BIN": str(self.fake_bin / "lsof"),
            "CURL_BIN": str(self.fake_bin / "curl"),
            "PS_BIN": str(self.fake_bin / "ps"),
            "OWNER_PROBE_BIN": str(self.fake_bin / "owner-probe"),
            "FAKE_STATE": str(self.state_path),
            "FAKE_CALLS": str(self.calls.path),
            "FAKE_CANDIDATE_CWD": str(checkout.path),
            "FAKE_CANDIDATE_SHA": checkout.sha,
            "FAKE_READER_GENERATION": str(checkout.reader_generation),
            "FAKE_CONTRACT_GENERATION": str(checkout.contract_generation),
        }

    def install(
        self, checkout: ReleaseCheckout | None = None, *, mode: str = "production",
        dry_run: bool = False, expected_sha: str | None = None, check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        checkout = self.candidate if checkout is None else checkout
        command = [
            str(checkout.path / "scripts" / "install_prediction_service_launchd.sh"),
            "--mode", mode, "--repo-root", str(checkout.path),
            "--runtime-root", str(self.runtime_root), "--python", sys.executable,
            "--config", str(self.root / "prediction.json"),
            "--launch-agents-dir", str(self.agents), "--wait-seconds", "1",
            "--release-manifest", str(checkout.path / "ops" / "prediction-service-release.json"),
        ]
        if dry_run:
            command.append("--dry-run")
        if expected_sha is not None:
            command.extend(("--expected-sha", expected_sha))
        return subprocess.run(
            command, check=check, capture_output=True, text=True,
            env=self._env(checkout),
        )

    def uninstall(self, *, mode: str = "production") -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                str(self.candidate.path / "scripts" / "uninstall_prediction_service_launchd.sh"),
                "--mode", mode, "--runtime-root", str(self.runtime_root),
                "--launch-agents-dir", str(self.agents), "--python", sys.executable,
            ],
            capture_output=True, text=True, env=self._env(self.candidate),
        )

    @property
    def runtime_record(self) -> dict[str, object] | None:
        return load_prediction_runtime_record(
            self.runtime_root / "prediction-service-runtime.json"
        )

    @property
    def plist(self) -> Path:
        return self.agents / f"{LABEL}.plist"

    @property
    def state(self) -> dict[str, object]:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    @property
    def listener_pids(self) -> list[int]:
        state = self.state
        return [int(state["pid"])] if state["listener"] else []

    def owner_is_available(self) -> bool:
        return self.state["owner_available"] is True

    @property
    def manifest(self) -> Path:
        return self.candidate.path / "ops" / "prediction-service-release.json"

    @property
    def sha(self) -> str:
        return self.candidate.sha

    @property
    def pid(self) -> int:
        return int(self.state["pid"])

    @property
    def started_at(self) -> str:
        return str(self.state["health"]["started_at"])

    @property
    def database(self) -> Path:
        return self.runtime_root / "data" / "prediction_arbitrage" / "prediction_arbitrage.sqlite3"

    @property
    def stdout_log(self) -> Path:
        return self.runtime_root / "logs" / "prediction_service" / "launchd.out.log"


@pytest.fixture
def release_harness(tmp_path: Path) -> ReleaseHarness:
    return ReleaseHarness(tmp_path)
```

Define `FAKE_COMMAND_SOURCE` immediately above `ReleaseHarness`. It is one executable Python script selected by `Path(sys.argv[0]).name`. Its exact state transitions are:

```python
FAKE_COMMAND_SOURCE = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path

state_path = Path(os.environ["FAKE_STATE"])
calls_path = Path(os.environ["FAKE_CALLS"])
state = json.loads(state_path.read_text(encoding="utf-8"))
command = Path(sys.argv[0]).name
with calls_path.open("a", encoding="utf-8") as calls:
    calls.write(command + " " + " ".join(sys.argv[1:]) + "\n")

def save():
    state_path.write_text(json.dumps(state), encoding="utf-8")

if command == "launchctl":
    action = sys.argv[1]
    if action == "print":
        if state["loaded"]:
            print(f"pid = {state['pid']}")
            raise SystemExit(0)
        print("Could not find service", file=sys.stderr)
        raise SystemExit(113)
    if action == "bootout":
        state.update(loaded=False, pid=0, cwd="", listener=False, owner_available=True, health={})
        save()
        raise SystemExit(0)
    if action == "bootstrap":
        if state["case"] in {"bind_failure", "reconcile_failure", "incompatible_reader"}:
            state.update(loaded=False, pid=0, listener=False, owner_available=True)
        else:
            pid = 4242
            health = {
                "schema_version": "open_trader.prediction_service.health.v1",
                "module": "prediction_service", "status": "running",
                "mode": "production", "production_owner": True,
                "mutations": "enabled", "pid": pid,
                "cwd": os.environ["FAKE_CANDIDATE_CWD"],
                "git_sha": os.environ["FAKE_CANDIDATE_SHA"],
                "started_at": "2026-08-11T10:00:00+08:00",
                "release_schema_version": "open_trader.prediction_service.release.v1",
                "reader_generation": int(os.environ["FAKE_READER_GENERATION"]),
                "contract_generation": int(os.environ["FAKE_CONTRACT_GENERATION"]),
            }
            if state["case"] == "wrong_health_sha":
                health["git_sha"] = "wrong"
            if state["case"] == "wrong_health_generation":
                health["reader_generation"] += 1
            state.update(loaded=True, pid=pid, cwd=health["cwd"], listener=True,
                         owner_available=False, health=health,
                         max_pids=max(int(state["max_pids"]), 1))
        save()
        raise SystemExit(0)

if command == "lsof":
    if "-d" in sys.argv and "cwd" in sys.argv:
        if state["pid"]:
            print(f"p{state['pid']}\nfcwd\nn{state['cwd']}")
            raise SystemExit(0)
        raise SystemExit(1)
    if state["listener"]:
        print(f"p{state['pid']}\nn127.0.0.1:8769")
        raise SystemExit(0)
    raise SystemExit(1)

if command == "curl":
    if state["health"]:
        print(json.dumps(state["health"]))
        raise SystemExit(0)
    raise SystemExit(22)

if command == "ps":
    raise SystemExit(0 if state["pid"] and str(state["pid"]) in sys.argv else 1)

if command == "owner-probe":
    raise SystemExit(0 if state["owner_available"] else 1)
'''
```

The fixture deliberately simulates only process-manager boundaries. The real Runtime generation/resource boundary remains covered by Task 3.

Add a parameterized fake-command test with these exact cases and assertions:

```python
@pytest.mark.parametrize(
    ("case", "expected_error"),
    [
        ("dirty_checkout", "release root is dirty"),
        ("wrong_sha", "requested SHA does not match checkout"),
        ("invalid_manifest", "prediction release manifest"),
        ("unknown_listener", "unknown listener on 8769"),
        ("unknown_label_identity", "managed launchd identity is not verified"),
        ("unknown_owner", "prediction runtime owner is held by an unknown process"),
    ],
)
def test_production_preflight_refuses_before_shutdown(
    release_harness: ReleaseHarness, case: str, expected_error: str
) -> None:
    checkout = release_harness.candidate
    install_kwargs: dict[str, object] = {"mode": "production"}
    if case == "dirty_checkout":
        (checkout.path / "dirty.txt").write_text("dirty", encoding="utf-8")
    elif case == "wrong_sha":
        install_kwargs["expected_sha"] = "0" * 40
    elif case == "invalid_manifest":
        manifest = checkout.path / "ops" / "prediction-service-release.json"
        manifest.write_text('{"reader_generation":0}\n', encoding="utf-8")
        subprocess.run(["git", "-C", str(checkout.path), "add", str(manifest)], check=True)
        subprocess.run(
            ["git", "-C", str(checkout.path), "-c", "user.name=test", "-c",
             "user.email=test@example.com", "commit", "-qm", "invalid manifest"],
            check=True,
        )
        checkout = ReleaseCheckout(
            path=checkout.path,
            sha=subprocess.run(
                ["git", "-C", str(checkout.path), "rev-parse", "HEAD"],
                check=True, capture_output=True, text=True,
            ).stdout.strip(),
        )
    else:
        release_harness.configure(case)
    result = release_harness.install(checkout, **install_kwargs)
    assert result.returncode == 1
    assert expected_error in result.stderr
    assert all(" bootout " not in f" {call} " for call in release_harness.calls.all())
    assert release_harness.runtime_record is None
```

Add a dry-run test proving no runtime directory, plist, log, runtime record, launchctl call, or SQLite file is created:

```python
def test_production_dry_run_is_side_effect_free(release_harness: ReleaseHarness) -> None:
    result = release_harness.install(mode="production", dry_run=True)
    assert result.returncode == 0
    payload = plistlib.loads(result.stdout.encode())
    assert payload["WorkingDirectory"] == str(release_harness.candidate.path)
    assert "--mode" in payload["ProgramArguments"]
    assert "production" in payload["ProgramArguments"]
    assert str(release_harness.manifest) in payload["ProgramArguments"]
    assert not release_harness.runtime_root.exists()
    assert release_harness.calls.all() == []
```

- [ ] **Step 3: Write first-install, same-SHA, upgrade, and rollback tests**

Create two clean candidate checkouts/commits with generation `1`; make the fake launchctl expose the PID from the candidate plist, fake lsof expose the plist cwd/listener, and fake curl expose health for that exact SHA/generations.

```python
def test_production_first_install_records_only_observed_ready_evidence(
    release_harness: ReleaseHarness,
) -> None:
    result = release_harness.install(mode="production")
    assert result.returncode == 0
    record = release_harness.runtime_record
    assert record["state"] == "ready"
    assert record["candidate"] == {
        "checkout": str(release_harness.candidate.path),
        "git_sha": release_harness.sha,
        "source_state": "clean",
        "reader_generation": 1,
        "contract_generation": 1,
    }
    assert record["previous_release"] is None
    assert record["ready"]["pid"] == release_harness.pid
    assert record["ready"]["cwd"] == str(release_harness.candidate.path)
    assert record["ready"]["listener"] == "127.0.0.1:8769"
    assert record["ready"]["health_schema"] == "open_trader.prediction_service.health.v1"
    assert record["ready"]["health_module"] == "prediction_service"
    assert record["ready"]["process_started_at"] == release_harness.started_at
    assert record["ready"]["logs"]["stdout"].endswith("launchd.out.log")


def test_same_sha_ready_install_is_a_noop(release_harness: ReleaseHarness) -> None:
    release_harness.install(mode="production", check=True)
    release_harness.calls.clear()
    result = release_harness.install(mode="production")
    assert result.returncode == 0
    assert "already ready" in result.stdout
    assert all(" bootout " not in f" {call} " for call in release_harness.calls.all())
    assert all(" bootstrap " not in f" {call} " for call in release_harness.calls.all())


@pytest.mark.parametrize("direction", ["upgrade", "rollback"])
def test_compatible_transition_uses_one_downtime_handoff(
    release_harness: ReleaseHarness, direction: str
) -> None:
    old = release_harness.candidate
    new = release_harness.make_checkout("new-candidate")
    first, candidate = (old, new) if direction == "upgrade" else (new, old)
    release_harness.install(first, check=True)
    release_harness.calls.clear()
    result = release_harness.install(candidate)
    assert result.returncode == 0
    calls = release_harness.calls.all()
    assert next(i for i, call in enumerate(calls) if call.startswith("launchctl bootout")) \
        < next(i for i, call in enumerate(calls) if call.startswith("launchctl bootstrap"))
    assert release_harness.state["max_pids"] == 1
    record = release_harness.runtime_record
    assert record is not None
    assert record["candidate"]["git_sha"] == candidate.sha
    assert record["previous_release"]["git_sha"] == first.sha
```

The rollback parameter installs the newer checkout first and then selects the older clean checkout explicitly. This proves the same command handles rollback without a separate rollback registry or search.

- [ ] **Step 4: Run RED**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_prediction_service_launchd.py tests/test_prediction_release_launchd.py -k 'template or dry_run or preflight or first_install or same_sha or compatible_transition'
```

Expected: template assertions and all production cases fail because the installer has no production release mode.

- [ ] **Step 5: Parameterize the existing plist and installer**

Replace the fixed `shadow` token with `OPEN_TRADER_PREDICTION_MODE` and append the manifest argument in the template. In the installer:

```bash
MODE="shadow"
RELEASE_MANIFEST=""
EXPECTED_SHA=""
PS_BIN="${PS_BIN:-/bin/ps}"
```

Parse `--mode`, `--release-manifest`, and `--expected-sha`; accept only `shadow|production`. Resolve the repo and manifest canonically with `Path.resolve()` without creating the runtime root during `--dry-run`. Validate the manifest by importing the Task 1 parser with candidate `PYTHONPATH`.

```bash
RELEASE_MANIFEST="${RELEASE_MANIFEST:-$REPO_ROOT/ops/prediction-service-release.json}"
```

Keep the current Shadow bottom-half as a conditional branch. In production, complete all preflight checks before the first runtime-record write or `bootout`:

```bash
ACTUAL_SHA="$(git -C "$REPO_ROOT" rev-parse HEAD)"
[[ -z "$(git -C "$REPO_ROOT" status --porcelain)" ]] || fail "release root is dirty: $REPO_ROOT"
[[ -z "$EXPECTED_SHA" || "$EXPECTED_SHA" == "$ACTUAL_SHA" ]] || fail "requested SHA does not match checkout"
MANIFEST_JSON="$(PYTHONPATH="$REPO_ROOT/src" "$PYTHON_BIN" - "$RELEASE_MANIFEST" <<'PY'
import json, sys
from pathlib import Path
from open_trader.prediction_release import load_prediction_release_manifest
release = load_prediction_release_manifest(Path(sys.argv[1]))
print(json.dumps({
    "schema_version": release.schema_version,
    "reader_generation": release.reader_generation,
    "contract_generation": release.contract_generation,
}, separators=(",", ":")))
PY
)"
```

Inspect current label/PID, cwd, listener, health, owner lock, and runtime record once. A loaded label is managed only when label PID, lsof cwd, lsof listener, production health PID/SHA/generations, and ready runtime-record identity agree. A listener/owner without that identity aborts.

Use the fake binary only when the test environment explicitly supplies it; production uses the existing lock class:

```bash
owner_available() {
  if [[ -n "${OWNER_PROBE_BIN:-}" ]]; then
    "$OWNER_PROBE_BIN" "$DATA_DIR"
    return
  fi
  PYTHONPATH="$REPO_ROOT/src" "$PYTHON_BIN" - "$DATA_DIR" <<'PY'
from pathlib import Path
import sys
from open_trader.prediction_runtime import _RuntimeOwnershipLock
lock = _RuntimeOwnershipLock(Path(sys.argv[1]) / "prediction_arbitrage" / "runtime.lock")
lock.acquire()
lock.release()
PY
}
```

- [ ] **Step 6: Implement the downtime transition and observed ready record**

Use one shell helper to invoke `write_prediction_runtime_record`; do not duplicate JSON-file replacement in Bash:

```bash
write_record() {
  local state="$1" failure_reason="$2" ready_json="$3"
  PYTHONPATH="$REPO_ROOT/src" "$PYTHON_BIN" - \
    "$RUNTIME_RECORD" "$state" "$CANDIDATE_JSON" "$PREVIOUS_JSON" \
    "$TRANSITION_STARTED_AT" "$failure_reason" "$ready_json" <<'PY'
import json, sys
from datetime import datetime
from pathlib import Path
from open_trader.prediction_release import write_prediction_runtime_record
path, state, candidate, previous, started, failure, ready = sys.argv[1:]
payload = {
    "state": state,
    "candidate": json.loads(candidate),
    "previous_release": None if previous == "null" else json.loads(previous),
    "transition_started_at": started,
    "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    "failure_reason": failure,
}
if ready != "null":
    payload["ready"] = json.loads(ready)
write_prediction_runtime_record(Path(path), payload)
PY
}
```

For a real transition:

1. write `maintenance` while preserving the previous ready identity;
2. `bootout` only the fully verified managed old label;
3. prove label absent, old PID absent through `PS_BIN`, listener absent, and owner lock acquirable/releasable through `_RuntimeOwnershipLock`;
4. render candidate production plist and bootstrap it;
5. require health schema/module/status/mode/owner/mutations/PID/cwd/SHA and both generations to match, then re-run `git status --porcelain` so the recorded `source_state=clean` is observed after startup rather than copied from preflight;
6. derive `ready` PID/cwd/listener/start time/health/log values from the observed launchctl/lsof/health payload;
7. atomically write `ready`, then print success.

The old monitor and execution workers are threads in the managed PID, so the verified PID absence is the worker-absence proof; do not invent a second worker registry.

- [ ] **Step 7: Implement fail-closed candidate cleanup**

Wrap candidate bootstrap/readiness so any failure after maintenance:

```bash
FAILURE_REASON="candidate_timeout"
if ! bootstrap_and_wait_for_exact_ready; then
  cleanup_verified_candidate
  if candidate_absent && listener_absent && owner_available; then
    write_record failed "$FAILURE_REASON" null
  else
    write_record failed "candidate_cleanup_not_proven" null
  fi
  exit 1
fi
```

Set `FAILURE_REASON` to `candidate_exited`, `wrong_health_identity`, `candidate_timeout`, or `candidate_source_became_dirty` at the observation that proves it. Runtime stderr retains the typed incompatible/store/client/reconciliation/monitor failure. Never call the previous checkout's installer in this branch and never report `ready` from candidate expectations.

- [ ] **Step 8: Run GREEN, shell/plist validation, and commit**

Run:

```bash
bash -n scripts/install_prediction_service_launchd.sh scripts/uninstall_prediction_service_launchd.sh
plutil -lint ops/launchd/com.open-trader.prediction-service.plist.template
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_prediction_service_launchd.py tests/test_prediction_release_launchd.py -k 'template or dry_run or preflight or first_install or same_sha or compatible_transition'
git diff --check
git add ops/launchd/com.open-trader.prediction-service.plist.template scripts/install_prediction_service_launchd.sh tests/test_prediction_service_launchd.py tests/test_prediction_release_launchd.py
git commit -m "feat: install managed prediction releases (#44)"
```

Expected: focused tests pass; shell and plist lint pass; no real launchd label or 8769 listener is touched.

---

### Task 5: Complete failed rollback and verified uninstall behavior

**Files:**
- Modify: `scripts/uninstall_prediction_service_launchd.sh`
- Modify: `tests/test_prediction_release_launchd.py`

**Interfaces:**
- Consumes: the ready/failed runtime record from Task 4, the same fakeable command variables, `_RuntimeOwnershipLock`, and the existing shared 20-poll cleanup budget.
- Produces: production uninstaller flags `--mode shadow|production` and `--runtime-root PATH`; a preserved runtime record with `state=stopped`; isolated end-to-end evidence for first install, upgrade, rollback, incompatible refusal, and repeated uninstall.

- [ ] **Step 1: Write incompatible and failed-candidate tests**

Add cases that simulate the candidate process exiting before health. Task 3 already proves that an incompatible real Runtime stops before Store/client/thread construction; this task proves the installer cleans up that exited candidate without starting the old release:

```python
def test_incompatible_rollback_stays_single_owner_and_non_ready(
    release_harness: ReleaseHarness,
) -> None:
    older = release_harness.candidate
    newer = release_harness.make_checkout("newer-candidate")
    release_harness.install(newer, check=True)
    state = release_harness.state
    state["case"] = "incompatible_reader"
    release_harness.state_path.write_text(json.dumps(state), encoding="utf-8")
    result = release_harness.install(older)
    assert result.returncode == 1
    assert "candidate_exited" in result.stderr
    assert release_harness.state["max_pids"] == 1
    record = release_harness.runtime_record
    assert record is not None
    assert record["state"] == "failed"
    assert record["previous_release"]["git_sha"] == newer.sha
    assert release_harness.listener_pids == []
    assert release_harness.owner_is_available()


@pytest.mark.parametrize(
    "failure",
    ["reconcile_failure", "wrong_health_sha", "wrong_health_generation", "bind_failure"],
)
def test_failed_candidate_is_removed_and_previous_is_not_auto_restarted(
    release_harness: ReleaseHarness, failure: str
) -> None:
    state = release_harness.state
    state["case"] = failure
    release_harness.state_path.write_text(json.dumps(state), encoding="utf-8")
    result = release_harness.install(mode="production")
    assert result.returncode == 1
    record = release_harness.runtime_record
    assert record is not None
    assert record["state"] == "failed"
    assert len(release_harness.calls.named("launchctl")) >= 2
    assert sum(" bootstrap " in f" {call} " for call in release_harness.calls.all()) == 1
    assert sum(" bootout " in f" {call} " for call in release_harness.calls.all()) == 1
    assert release_harness.listener_pids == []
    assert release_harness.owner_is_available()
```

- [ ] **Step 2: Write verified uninstall and idempotence tests**

```python
def test_production_uninstall_preserves_data_logs_and_marks_stopped(
    release_harness: ReleaseHarness,
) -> None:
    release_harness.install(mode="production", check=True)
    release_harness.database.parent.mkdir(parents=True, exist_ok=True)
    release_harness.database.write_bytes(b"test-database")
    release_harness.stdout_log.parent.mkdir(parents=True, exist_ok=True)
    release_harness.stdout_log.write_text("keep-log\n", encoding="utf-8")
    database_before = release_harness.database.read_bytes()
    log_before = release_harness.stdout_log.read_text(encoding="utf-8")
    result = release_harness.uninstall(mode="production")
    assert result.returncode == 0
    record = release_harness.runtime_record
    assert record is not None
    assert record["state"] == "stopped"
    assert record["candidate"]["git_sha"] == release_harness.sha
    assert release_harness.database.read_bytes() == database_before
    assert release_harness.stdout_log.read_text(encoding="utf-8") == log_before
    assert not release_harness.plist.exists()
    assert release_harness.listener_pids == []
    assert release_harness.owner_is_available()

    repeated = release_harness.uninstall(mode="production")
    assert repeated.returncode == 0
    assert sum(" bootout " in f" {call} " for call in release_harness.calls.all()) == 1


def test_uninstall_refuses_unknown_identity_without_bootout_or_plist_removal(
    release_harness: ReleaseHarness,
) -> None:
    release_harness.install(mode="production", check=True)
    release_harness.configure("unknown_label_identity")
    release_harness.calls.clear()
    result = release_harness.uninstall(mode="production")
    assert result.returncode == 1
    assert all(" bootout " not in f" {call} " for call in release_harness.calls.all())
    assert release_harness.plist.exists()
```

Add this boundary table for incomplete cleanup. Each fake leaves exactly one proof false after `bootout`; the script must retain the plist and a non-stopped record:

```python
@pytest.mark.parametrize("case", ["pid_still_present", "listener_still_present", "owner_still_held"])
def test_uninstall_requires_every_absence_proof(
    release_harness: ReleaseHarness, case: str
) -> None:
    release_harness.install(mode="production", check=True)
    state = release_harness.state
    state["case"] = case
    release_harness.state_path.write_text(json.dumps(state), encoding="utf-8")
    result = release_harness.uninstall(mode="production")
    assert result.returncode == 1
    assert release_harness.plist.exists()
    record = release_harness.runtime_record
    assert record is not None
    assert record["state"] != "stopped"
```

Replace the fake dispatcher's `bootout` state update with this finite boundary switch:

```python
case = state["case"]
old_pid = state["pid"]
state.update(loaded=False, pid=0, cwd="", listener=False,
             owner_available=True, health={})
if case == "pid_still_present":
    state["pid"] = old_pid
if case == "listener_still_present":
    state.update(pid=old_pid, listener=True)
if case == "owner_still_held":
    state["owner_available"] = False
save()
raise SystemExit(0)
```

This is test-only; production keeps one generic absence check.

- [ ] **Step 3: Run RED**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_prediction_release_launchd.py -k 'incompatible or failed_candidate or uninstall'
```

Expected: production uninstall arguments are rejected and evidence is not marked stopped.

- [ ] **Step 4: Extend the uninstaller without weakening the Shadow path**

Add `MODE=shadow`, `RUNTIME_ROOT=`, `PYTHON_BIN`, `CURL_BIN`, and `PS_BIN` parsing. Preserve the existing Shadow branch. In production:

1. require and canonically resolve the runtime root;
2. read and validate `prediction-service-runtime.json` through `load_prediction_runtime_record`;
3. if the label is loaded, require PID/cwd/listener/health to match the runtime record before bootout;
4. if the label is absent, refuse any listener or unavailable owner as unknown;
5. after bootout, share the existing 20-poll budget across label and listener checks, then prove PID and owner absence;
6. remove only this label's plist;
7. preserve candidate, previous release, ready evidence, data, config, and logs while atomically writing `state=stopped` and `failure_reason=""`;
8. repeated uninstall succeeds without another bootout.

Use the Task 1 writer through one embedded Python call; do not delete the runtime record.

- [ ] **Step 5: Run the isolated direct workflow**

Run the actual scripts through the fake-command harness in one named test:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q -s tests/test_prediction_release_launchd.py::test_checkout_release_direct_workflow
```

Implement the named test with explicit observations after each actual script call:

```python
def test_checkout_release_direct_workflow(release_harness: ReleaseHarness) -> None:
    from open_trader.prediction_arbitrage_store import PredictionArbitrageStore

    old = release_harness.candidate
    new = release_harness.make_checkout("direct-new")
    observed: list[dict[str, object]] = []
    PredictionArbitrageStore(release_harness.runtime_root / "data")

    for checkout in (old, new, old):
        result = release_harness.install(checkout)
        assert result.returncode == 0, result.stderr
        record = release_harness.runtime_record
        assert record is not None
        observed.append({
            "state": record["state"],
            "git_sha": record["candidate"]["git_sha"],
            "reader_generation": record["candidate"]["reader_generation"],
            "contract_generation": record["candidate"]["contract_generation"],
        })

    stopped = release_harness.uninstall()
    assert stopped.returncode == 0, stopped.stderr
    final_record = release_harness.runtime_record
    assert final_record is not None
    evidence = {
        "states": [item["state"] for item in observed] + [final_record["state"]],
        "candidate_shas": [item["git_sha"] for item in observed],
        "reader_generations": [item["reader_generation"] for item in observed],
        "contract_generations": [item["contract_generation"] for item in observed],
        "max_simultaneous_managed_pids": release_harness.state["max_pids"],
        "final_listener": release_harness.listener_pids or None,
        "final_owner_available": release_harness.owner_is_available(),
    }
    print(json.dumps(evidence, sort_keys=True))

assert evidence == {
    "states": ["ready", "ready", "ready", "stopped"],
    "candidate_shas": [old.sha, new.sha, old.sha],
    "reader_generations": [1, 1, 1],
    "contract_generations": [1, 1, 1],
    "max_simultaneous_managed_pids": 1,
    "final_listener": None,
    "final_owner_available": True,
}
```

This is the required direct first-install → upgrade → compatible rollback → uninstall workflow. It uses temporary SQLite and fake launchctl/lsof/curl/ps/owner-probe only. The test asserts the fake-command log contains no `/bin/launchctl`, `/usr/sbin/lsof`, or real runtime-root path, so it cannot mutate the real service.

- [ ] **Step 6: Run GREEN, combined launchd regression, and commit**

Run:

```bash
bash -n scripts/install_prediction_service_launchd.sh scripts/uninstall_prediction_service_launchd.sh
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_prediction_service_launchd.py tests/test_prediction_release_launchd.py
git diff --check
git add scripts/uninstall_prediction_service_launchd.sh tests/test_prediction_release_launchd.py
git commit -m "feat: stop and roll back prediction releases (#44)"
```

Expected: all Shadow and production script tests pass, including the real-script isolated workflow; no real process remains.

---

### Task 6: Record the operator change and run the final gate

**Files:**
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: Tasks 1-5 and the approved design at `docs/superpowers/specs/2026-08-11-prediction-service-release-design.md`.
- Produces: one dated #44 operator entry plus final automated/direct/process evidence at one exact Git SHA.

- [ ] **Step 1: Run the relevant release/runtime regression before editing the changelog**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q \
  tests/test_prediction_release.py \
  tests/test_prediction_arbitrage_store.py \
  tests/test_prediction_runtime.py \
  tests/test_prediction_service.py \
  tests/test_prediction_service_launchd.py \
  tests/test_prediction_release_launchd.py \
  tests/test_prediction_arbitrage_execution.py \
  tests/test_prediction_api_contract.py
```

Expected: PASS with only already-known warnings. Fix any failure in the task that owns it and commit that fix separately before continuing.

- [ ] **Step 2: Add the dated operator-facing changelog entry**

Under `## 2026-08-11`, add:

```markdown
- Prediction Service release (#44): clean local Git checkouts can now be installed,
  upgraded, stopped, or compatibly rolled back as the single managed 8769 owner.
  Production startup checks the persisted minimum reader generation under the
  owner lock before writable Store/client/thread construction, and launchd
  transitions retain exact ready/failed/stopped evidence for the later #45 cutover.
```

- [ ] **Step 3: Commit the changelog before any merge**

Run:

```bash
git add CHANGELOG.md
git commit -m "docs: record prediction service release (#44)"
```

- [ ] **Step 4: Run syntax, focused workflow, full Python regression, and cleanliness checks**

Run in this order:

```bash
bash -n scripts/install_prediction_service_launchd.sh scripts/uninstall_prediction_service_launchd.sh
plutil -lint ops/launchd/com.open-trader.prediction-service.plist.template
PREDICTION_8769_BEFORE="$(lsof -nP -iTCP:8769 -sTCP:LISTEN 2>/dev/null || true)"
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q -s tests/test_prediction_release_launchd.py::test_checkout_release_direct_workflow
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q
PREDICTION_8769_AFTER="$(lsof -nP -iTCP:8769 -sTCP:LISTEN 2>/dev/null || true)"
test "$PREDICTION_8769_AFTER" = "$PREDICTION_8769_BEFORE"
git diff --check main...HEAD
git status --short
pgrep -fal 'open_trader.*prediction-service|pytest.*prediction_release' || true
```

Expected:

- both shell scripts parse;
- plist lint passes;
- isolated direct workflow prints exact SHA/generation/state/PID/cwd/listener/owner evidence and passes;
- the full Python suite passes;
- `git diff --check` has no output and the worktree is clean;
- no test Prediction Service/pytest process remains (a pre-existing real service is reported, not stopped);
- the real 8769 listener snapshot is byte-for-byte unchanged by the fake-command workflow and tests.

- [ ] **Step 5: Run the final two-axis code review**

Use the `code-review` skill against fixed point `d408925cb742438893a6edaf5daaeb8a3b74c7c6` and resolve every blocking Standards or Spec finding. Re-run the smallest affected test after each fix, then repeat Step 4 at the final SHA.

- [ ] **Step 6: Stop for merge authorization**

Report the exact final SHA and verification outputs. Explicitly state that #44 has not stopped Legacy, installed the production launchd job, changed Gateway routing, or enabled real traffic; those actions remain #45. Do not merge, push, deploy, or run `make acceptance` without the user's next instruction.
