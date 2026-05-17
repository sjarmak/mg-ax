"""Cassette discovery, replay, and recording (R27).

Cassettes are recorded trace JSONL files, content-addressed via Git-LFS in
production (`.gitattributes` marks fixtures/cassettes/*.jsonl as LFS) so the
harness repo stays lightweight.

The trace CLI looks up a cassette by `(scenario_id, mode)`; replay copies the
cassette's bytes verbatim into `traces/<scenario_id>.<mode>.jsonl` so byte-equal
rerun is the default and live and replay traces are interchangeable downstream.

Live recording (`mcp-ax-ph3.14`): `write_cassette` serialises an in-memory list
of trace records into the cassette format. The recorder itself lives in
`harness.runtime.live_agent`; the CLI wires LLM + MCP clients per request.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

CASSETTE_MAX_BYTES = 500 * 1024  # 500 KB per scenario (P2 mitigation)


class CassetteNotFoundError(FileNotFoundError):
    pass


class CassetteTooLargeError(ValueError):
    pass


class LiveRecordingNotConfiguredError(RuntimeError):
    """Raised when --record is requested but the live runtime isn't wired."""


@dataclass(frozen=True)
class Cassette:
    scenario_id: str
    mode: str
    path: Path
    bytes: bytes

    @property
    def size_bytes(self) -> int:
        return len(self.bytes)

    @property
    def hash(self) -> str:
        return hashlib.sha256(self.bytes).hexdigest()[:16]


def cassette_path(cassettes_dir: Path, scenario_id: str, mode: str) -> Path:
    return cassettes_dir / f"{scenario_id}.{mode}.jsonl"


def load_cassette(cassettes_dir: Path, scenario_id: str, mode: str) -> Cassette:
    path = cassette_path(cassettes_dir, scenario_id, mode)
    if not path.is_file():
        raise CassetteNotFoundError(
            f"no cassette for scenario={scenario_id!r} mode={mode!r} at {path}; "
            "run with --record to capture (requires live MCP server credentials)."
        )
    raw = path.read_bytes()
    if len(raw) > CASSETTE_MAX_BYTES:
        raise CassetteTooLargeError(
            f"cassette {path.name} is {len(raw)} bytes; per-scenario size budget "
            f"is {CASSETTE_MAX_BYTES} (P2 mitigation, premortem Theme C)."
        )
    return Cassette(
        scenario_id=scenario_id, mode=mode, path=path, bytes=raw
    )


def replay_to(cassette: Cassette, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = dest_dir / f"{cassette.scenario_id}.{cassette.mode}.jsonl"
    out.write_bytes(cassette.bytes)
    return out


def estimated_cost(cassette: Cassette) -> float:
    """Sum of per-record `cost_usd` in the cassette — used as the replay cost
    estimate by the budget cap (R12). Live recording would track running cost
    against the same cap; this stays consistent across modes."""
    total = 0.0
    for line in cassette.bytes.decode("utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        total += float(rec.get("cost_usd", 0.0) or 0.0)
    return total


def serialise_records(records: Iterable[dict]) -> bytes:
    """Render trace records as canonical JSONL bytes (one compact object per line)."""
    out = []
    for rec in records:
        out.append(json.dumps(rec, separators=(",", ":"), sort_keys=True))
    if not out:
        return b""
    return ("\n".join(out) + "\n").encode("utf-8")


def write_cassette(
    cassettes_dir: Path, scenario_id: str, mode: str, records: Iterable[dict]
) -> Cassette:
    """Write `records` as a JSONL cassette and return the loaded handle.

    The CASSETTE_MAX_BYTES budget (P2 mitigation) is enforced before the file
    lands on disk so an oversized live capture fails loud rather than being
    truncated downstream.
    """
    cassettes_dir.mkdir(parents=True, exist_ok=True)
    payload = serialise_records(records)
    if len(payload) > CASSETTE_MAX_BYTES:
        raise CassetteTooLargeError(
            f"recorded cassette for scenario={scenario_id!r} mode={mode!r} is "
            f"{len(payload)} bytes; per-scenario size budget is {CASSETTE_MAX_BYTES} "
            "(P2 mitigation, premortem Theme C). Trim the scenario or split it."
        )
    path = cassette_path(cassettes_dir, scenario_id, mode)
    path.write_bytes(payload)
    return Cassette(scenario_id=scenario_id, mode=mode, path=path, bytes=payload)


def live_record_not_configured() -> None:
    raise LiveRecordingNotConfiguredError(
        "live recording requires both ANTHROPIC_API_KEY and MCP_SERVER_ENDPOINT "
        "environment variables. Set them and retry, or omit --record / --no-cassette "
        "to use cassette replay (fixtures/cassettes/<scenario>.<mode>.jsonl)."
    )
