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
CREATE TABLE IF NOT EXISTS ps_stations (
    station_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    name TEXT NOT NULL,
    township TEXT NOT NULL,
    populations_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ps_calendars (
    calendar_id TEXT PRIMARY KEY,
    station_id TEXT NOT NULL REFERENCES ps_stations(station_id),
    kind TEXT NOT NULL,
    label TEXT NOT NULL,
    rule_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(station_id, kind)
);
CREATE TABLE IF NOT EXISTS ps_subsidy_agreements (
    agreement_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    station_ids_json TEXT NOT NULL,
    remedy_owner TEXT NOT NULL,
    remedy_organization_id TEXT,
    valid_from TEXT NOT NULL,
    valid_to TEXT NOT NULL,
    terms_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ps_commitments (
    commitment_id TEXT PRIMARY KEY,
    station_id TEXT NOT NULL REFERENCES ps_stations(station_id),
    title TEXT NOT NULL,
    demand_kinds_json TEXT NOT NULL,
    min_stops_on_demand_day INTEGER NOT NULL CHECK(min_stops_on_demand_day >= 1),
    min_stops_per_week INTEGER NOT NULL DEFAULT 0 CHECK(min_stops_per_week >= 0),
    agreement_id TEXT NOT NULL REFERENCES ps_subsidy_agreements(agreement_id),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ps_plans (
    plan_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    title TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT NOT NULL,
    supersedes_plan_id TEXT REFERENCES ps_plans(plan_id),
    status TEXT NOT NULL CHECK(status IN ('draft', 'confirmed', 'superseded')),
    snapshot_json TEXT,
    evidence_hash TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    confirmed_by TEXT,
    confirmed_at TEXT
);
CREATE TABLE IF NOT EXISTS ps_plan_trains (
    train_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES ps_plans(plan_id),
    train_code TEXT NOT NULL,
    weekdays_json TEXT NOT NULL,
    cargo_capacity_kg REAL NOT NULL DEFAULT 0,
    stops_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(plan_id, train_code)
);
CREATE TABLE IF NOT EXISTS ps_blocks (
    block_id TEXT PRIMARY KEY,
    scope TEXT NOT NULL CHECK(scope IN ('station', 'section')),
    station_id TEXT REFERENCES ps_stations(station_id),
    from_station_id TEXT REFERENCES ps_stations(station_id),
    to_station_id TEXT REFERENCES ps_stations(station_id),
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    label TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ps_disasters (
    disaster_id TEXT PRIMARY KEY,
    scope TEXT NOT NULL CHECK(scope IN ('station', 'section')),
    effect TEXT NOT NULL CHECK(effect IN ('no_stop', 'no_service')),
    station_id TEXT REFERENCES ps_stations(station_id),
    from_station_id TEXT REFERENCES ps_stations(station_id),
    to_station_id TEXT REFERENCES ps_stations(station_id),
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    label TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ps_consignments (
    consignment_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES ps_plans(plan_id),
    train_code TEXT NOT NULL,
    origin_station_id TEXT NOT NULL REFERENCES ps_stations(station_id),
    destination_station_id TEXT NOT NULL REFERENCES ps_stations(station_id),
    send_date TEXT NOT NULL,
    weight_kg REAL NOT NULL DEFAULT 0,
    cargo_name TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('accepted', 'carried')),
    accepted_by TEXT NOT NULL,
    accepted_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ps_amendments (
    amendment_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES ps_plans(plan_id),
    kind TEXT NOT NULL CHECK(kind IN ('add_stop', 'skip_stop', 'cancel_train', 'restore')),
    train_code TEXT NOT NULL,
    station_id TEXT REFERENCES ps_stations(station_id),
    event_date TEXT NOT NULL,
    end_date TEXT,
    reason TEXT NOT NULL,
    source_type TEXT,
    source_id TEXT,
    affected_populations_json TEXT NOT NULL,
    restores_amendment_id TEXT REFERENCES ps_amendments(amendment_id),
    replacement_group_id TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ps_replacements (
    replacement_id TEXT PRIMARY KEY,
    group_id TEXT NOT NULL,
    amendment_id TEXT REFERENCES ps_amendments(amendment_id),
    source_type TEXT,
    source_id TEXT,
    event_date TEXT NOT NULL,
    station_ids_json TEXT NOT NULL,
    mode TEXT NOT NULL,
    capacity_seats INTEGER,
    cargo_capacity_kg REAL NOT NULL DEFAULT 0,
    covers_consignments_json TEXT NOT NULL,
    responsible_actor_id TEXT,
    affected_populations_json TEXT NOT NULL DEFAULT '[]',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ps_rounds (
    round_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES ps_plans(plan_id),
    sequence_no INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open', 'frozen')),
    opened_at TEXT NOT NULL,
    frozen_at TEXT,
    summary_json TEXT
);
CREATE TABLE IF NOT EXISTS ps_ridership (
    observation_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES ps_plans(plan_id),
    round_id TEXT REFERENCES ps_rounds(round_id),
    station_id TEXT NOT NULL REFERENCES ps_stations(station_id),
    train_code TEXT NOT NULL,
    event_date TEXT NOT NULL,
    boarding INTEGER NOT NULL CHECK(boarding >= 0),
    alighting INTEGER NOT NULL CHECK(alighting >= 0),
    late INTEGER NOT NULL DEFAULT 0 CHECK(late IN (0, 1)),
    submitted_by TEXT NOT NULL,
    submitted_at TEXT NOT NULL
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
