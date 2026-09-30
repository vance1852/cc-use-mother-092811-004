"""封装 SQLite 连接、建表和事务边界。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS organizations (
    organization_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actors (
    actor_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sites (
    site_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS domain_records (
    record_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    category TEXT NOT NULL,
    external_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, category, external_key)
);
CREATE TABLE IF NOT EXISTS request_receipts (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS service_commitments (
    commitment_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    station_id TEXT NOT NULL,
    min_stops_per_day INTEGER NOT NULL CHECK(min_stops_per_day >= 1),
    cargo_acceptance INTEGER NOT NULL CHECK(cargo_acceptance IN (0, 1)),
    serve_market_days INTEGER NOT NULL CHECK(serve_market_days IN (0, 1)),
    serve_medical_days INTEGER NOT NULL CHECK(serve_medical_days IN (0, 1)),
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    superseded_by TEXT,
    note TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS demand_calendar_entries (
    entry_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    station_id TEXT NOT NULL,
    date TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('market', 'medical')),
    note TEXT NOT NULL DEFAULT '',
    retracted INTEGER NOT NULL CHECK(retracted IN (0, 1)),
    retracted_by TEXT,
    retracted_at TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plan_versions (
    plan_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    label TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('draft', 'confirmed', 'effective', 'superseded')),
    valid_from TEXT NOT NULL,
    valid_to TEXT NOT NULL,
    confirmed_by TEXT,
    confirmed_at TEXT,
    activated_on TEXT,
    superseded_on TEXT,
    superseded_by TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plan_trains (
    plan_id TEXT NOT NULL REFERENCES plan_versions(plan_id),
    train_no TEXT NOT NULL,
    run_weekdays TEXT NOT NULL,
    passenger_capacity INTEGER NOT NULL CHECK(passenger_capacity >= 0),
    freight_capacity INTEGER NOT NULL CHECK(freight_capacity >= 0),
    PRIMARY KEY(plan_id, train_no)
);
CREATE TABLE IF NOT EXISTS plan_stops (
    plan_id TEXT NOT NULL,
    train_no TEXT NOT NULL,
    station_id TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK(sequence >= 1),
    arrive TEXT NOT NULL,
    depart TEXT NOT NULL,
    handles_freight INTEGER NOT NULL CHECK(handles_freight IN (0, 1)),
    PRIMARY KEY(plan_id, train_no, station_id),
    FOREIGN KEY(plan_id, train_no) REFERENCES plan_trains(plan_id, train_no)
);
CREATE TABLE IF NOT EXISTS blockades (
    blockade_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    station_id TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS disaster_restrictions (
    restriction_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    station_id TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS subsidy_agreements (
    agreement_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    station_id TEXT NOT NULL,
    funder_name TEXT NOT NULL,
    liable_organization TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS temporary_changes (
    change_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    plan_id TEXT NOT NULL REFERENCES plan_versions(plan_id),
    change_type TEXT NOT NULL CHECK(change_type IN
        ('add_stop', 'skip_stop', 'cancel_train', 'replacement', 'restore')),
    service_date TEXT NOT NULL,
    train_no TEXT NOT NULL,
    station_id TEXT,
    arrive TEXT,
    depart TEXT,
    handles_freight INTEGER CHECK(handles_freight IN (0, 1)),
    affected_groups TEXT NOT NULL,
    replacement_mode TEXT,
    replacement_capacity INTEGER,
    replacement_carrier TEXT,
    restores_change_id TEXT,
    forced_over_commitment INTEGER NOT NULL CHECK(forced_over_commitment IN (0, 1)),
    reason TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS goods_acceptances (
    acceptance_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    service_date TEXT NOT NULL,
    train_no TEXT NOT NULL,
    station_id TEXT NOT NULL,
    units INTEGER NOT NULL CHECK(units >= 1),
    description TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('accepted', 'transferred')),
    transferred_to TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ridership_observations (
    observation_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    service_date TEXT NOT NULL,
    train_no TEXT NOT NULL,
    station_id TEXT NOT NULL,
    passengers INTEGER NOT NULL CHECK(passengers >= 0),
    freight_units INTEGER NOT NULL CHECK(freight_units >= 0),
    submitted_at TEXT NOT NULL,
    round_id TEXT,
    created_by TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS coverage_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    from_date TEXT NOT NULL,
    to_date TEXT NOT NULL,
    inputs_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    evidence_hash TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evaluation_rounds (
    round_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    cutoff_at TEXT NOT NULL,
    snapshot_id TEXT NOT NULL REFERENCES coverage_snapshots(snapshot_id),
    low_utilization_threshold REAL NOT NULL,
    result_json TEXT NOT NULL,
    included_count INTEGER NOT NULL,
    deferred_count INTEGER NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交。"""

        self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self.connection
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def close(self) -> None:
        """关闭底层连接。"""

        self.connection.close()
