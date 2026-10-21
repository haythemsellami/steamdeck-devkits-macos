#!/usr/bin/env python3
"""Validate Swift-bridge test fixtures against the real pydantic models.

Every fixture in this directory is the exact JSON the fake MCP server serves
(and the shape the real server produces), so each one must round-trip through
the core's models. Run: ``uv run python app/Tests/Fixtures/validate.py``.
Exit 0 iff all fixtures validate; prints one line per fixture.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from steamos_devkit import models as m
from steamos_devkit.mcp_server import ArtifactEntry, ArtifactRead, JobStarted

HERE = Path(__file__).resolve().parent

MODEL_FIXTURES: dict[str, type] = {
    "doctor.json": m.DoctorReport,
    "capabilities.json": m.Capabilities,
    "device_status.json": m.DeviceStatus,
    "title_detail.json": m.TitleDetail,
    "deploy_preview.json": m.DeployPreview,
    "job_started.json": JobStarted,
    "job_info_running.json": m.JobInfo,
    "job_info_succeeded.json": m.JobInfo,
    "launch_result.json": m.LaunchResult,
    "result_ok.json": m.Result,
    "artifact_read.json": ArtifactRead,
}

LIST_FIXTURES: dict[str, type] = {
    "list_devices.json": m.Device,
    "list_titles.json": m.Title,
    "list_jobs.json": m.JobInfo,
    "list_artifacts.json": ArtifactEntry,
}

# isError payloads (mcp_server._error_payload shape) have no pydantic model;
# validate the contract keys structurally.
ERROR_PAYLOAD_FIXTURES = ("read_artifact_bogus.json", "job_status_bogus.json")


def main() -> int:
    failures = 0
    for name, model in sorted(MODEL_FIXTURES.items()):
        try:
            model(**json.loads((HERE / name).read_text()))
            print(f"ok {name} -> {model.__name__}")
        except Exception as err:
            failures += 1
            print(f"FAIL {name} -> {model.__name__}: {err}")
    for name, model in sorted(LIST_FIXTURES.items()):
        try:
            items = json.loads((HERE / name).read_text())
            assert isinstance(items, list), "top level must be a JSON array"
            for item in items:
                model(**item)
            print(f"ok {name} -> list[{model.__name__}] ({len(items)} entries)")
        except Exception as err:
            failures += 1
            print(f"FAIL {name} -> list[{model.__name__}]: {err}")
    for name in ERROR_PAYLOAD_FIXTURES:
        try:
            payload = json.loads((HERE / name).read_text())
            assert isinstance(payload["code"], str) and isinstance(payload["message"], str)
            assert set(payload) <= {"code", "message", "details", "remote_exit_code"}
            print(f"ok {name} -> error payload ({payload['code']})")
        except Exception as err:
            failures += 1
            print(f"FAIL {name} -> error payload: {err}")
    # MCP SDK shapes (no pydantic model): sanity-check the keys we rely on.
    init = json.loads((HERE / "initialize_result.json").read_text())
    assert init["protocolVersion"] == "2025-06-18", init
    assert "serverInfo" in init and "capabilities" in init
    print("ok initialize_result.json -> handshake (protocolVersion 2025-06-18)")
    tools = json.loads((HERE / "tools_list.json").read_text())
    assert len(tools["tools"]) == 33, len(tools["tools"])
    print("ok tools_list.json -> 33 tools")

    print(f"validate: {'pass' if failures == 0 else 'FAIL'}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
