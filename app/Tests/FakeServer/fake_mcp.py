#!/usr/bin/env python3
"""Fake devkit-mcp stdio server for the Swift bridge test harness.

Newline-delimited JSON-RPC 2.0 on stdin/stdout, mirroring the real server's
(mcp SDK 2.3.0) wire behavior observed on 2026-10-21:

- successful tools/call results carry ``structuredContent`` (arrays wrapped as
  ``{"result": [...]}``) plus a pretty-printed text copy — except empty arrays,
  whose ``content`` is empty;
- tool errors are ``isError: true`` results whose text is
  ``Error executing tool <name>: {"code": ..., "message": ...}`` (the SDK
  prefixes the payload JSON) — except the literal "boom" status case, which
  returns the bare payload JSON to cover both client paths;
- resources/read failures are JSON-RPC errors (-32602), not isError results.

Special test hooks (not part of the devkit surface):

- tools/call "fake_emit_garbage": one malformed line, then a normal result —
  exercises the client's record-and-continue path.
- tools/call "fake_emit_unknown_response": a response with an unknown id,
  then a normal result.
- tools/call "fake_slow": answered only after the NEXT request has been
  answered — deterministic out-of-order responses.
- tools/call "fake_exit": process exits without answering — transport death.
- devkit_status with device "boom": isError with the bare payload JSON;
  with "boom-prefix": isError with the SDK-style prefixed payload.
- devkit_deploy with _meta.progressToken: 3 progress notifications BEFORE the
  result and 2 AFTER it, then the JobStarted fixture.

All stdout writes go through one lock; lines never interleave. Stdlib only.
"""

from __future__ import annotations

import json
import os
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.normpath(os.path.join(HERE, "..", "Fixtures"))

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "fake-devkit", "version": "0.0.0"}

# A valid 1x1 red PNG (69 bytes), sha256 b1ff9c8e…4640. Served as the blob of
# devkit://artifact/art_FAKE.
FAKE_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
)

RUNNING_JOB = "job_01J9ZE0000000000000000000A"
SUCCEEDED_JOB = "job_01J9ZE0000000000000000000B"
KNOWN_ARTIFACT = "art_01J9ZE0000000000000000000C"

_write_lock = threading.Lock()


def _write_raw(line: str) -> None:
    with _write_lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def _send(obj: dict) -> None:
    _write_raw(json.dumps(obj))


def _result(req_id, result: dict) -> None:
    _send({"jsonrpc": "2.0", "id": req_id, "result": result})


def _error(req_id, code: int, message: str, data=None) -> None:
    err: dict = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    _send({"jsonrpc": "2.0", "id": req_id, "error": err})


def _fixture(name: str):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return json.load(fh)


def _tool_result(req_id, payload) -> None:
    """Mirror the SDK envelope: structuredContent + pretty text copy.

    Arrays are wrapped as {"result": [...]} and produce no text content when
    empty — both observed on the real server.
    """
    if isinstance(payload, list):
        structured = {"result": payload}
        content = [] if not payload else [{"type": "text", "text": json.dumps(payload, indent=2)}]
    else:
        structured = payload
        content = [{"type": "text", "text": json.dumps(payload, indent=2)}]
    _result(req_id, {"content": content, "isError": False, "structuredContent": structured})


def _tool_error(req_id, tool: str, payload: dict, *, prefixed: bool) -> None:
    text = json.dumps(payload)
    if prefixed:
        text = f"Error executing tool {tool}: {text}"
    _result(req_id, {"content": [{"type": "text", "text": text}], "isError": True})


def _progress(token, value: float, total: float | None, message: str) -> None:
    params: dict = {"progressToken": token, "progress": value}
    if total is not None:
        params["total"] = total
    if message is not None:
        params["message"] = message
    _send({"jsonrpc": "2.0", "method": "notifications/progress", "params": params})


RESULT_OK_TOOLS = {
    "devkit_configure_title",
    "devkit_register_title",
    "devkit_delete_title",
    "devkit_forget_device",
    "devkit_pair",
    "devkit_perf_overlay",
    "devkit_frametimes",
    "devkit_renderdoc_layer",
    "devkit_renderdoc_server",
    "devkit_proton_logging",
    "devkit_set_steam_mode",
    "devkit_set_session",
    "devkit_reload_steam",
    "devkit_job_cancel",
}

JOB_STARTER_TOOLS = {
    "devkit_fetch_logs",
    "devkit_screenshot",
    "devkit_controller_config",
    "devkit_gpu_trace",
    "devkit_rgp_capture",
}

FIXTURE_TOOLS = {
    "devkit_doctor": "doctor.json",
    "devkit_list_devices": "list_devices.json",
    "devkit_capabilities": "capabilities.json",
    "devkit_list_titles": "list_titles.json",
    "devkit_inspect_title": "title_detail.json",
    "devkit_preview_deploy": "deploy_preview.json",
    "devkit_launch_title": "launch_result.json",
    "devkit_list_jobs": "list_jobs.json",
    "devkit_list_artifacts": "list_artifacts.json",
}


def _call_tool(req_id, params: dict, slow_slot: dict) -> bool:
    """Handle one tools/call. Returns True when the answer was deferred
    (fake_slow) — the caller must not flush the slow slot this round."""
    name = params.get("name", "")
    args = params.get("arguments") or {}
    meta = params.get("_meta") or {}

    if name == "fake_exit":
        sys.stdout.flush()
        os._exit(0)
    if name == "fake_emit_garbage":
        _write_raw("this is not json {")
        _tool_result(req_id, _fixture("result_ok.json"))
        return False
    if name == "fake_emit_unknown_response":
        _send({"jsonrpc": "2.0", "id": 999999, "result": {}})
        _tool_result(req_id, _fixture("result_ok.json"))
        return False
    if name == "fake_slow":
        slow_slot["pending"] = (req_id, {"ok": True, "message": "slow answer"})
        return True

    if name == "devkit_status":
        device = args.get("device", "")
        if device == "boom":
            _tool_error(
                req_id,
                name,
                {
                    "code": "DEVICE_UNREACHABLE",
                    "message": "cannot reach boom",
                    "details": {"target": "boom"},
                },
                prefixed=False,
            )
            return False
        if device == "boom-prefix":
            _tool_error(
                req_id,
                name,
                {
                    "code": "DEVICE_UNREACHABLE",
                    "message": "cannot reach boom-prefix",
                    "details": {"target": "boom-prefix"},
                },
                prefixed=True,
            )
            return False
        _tool_result(req_id, _fixture("device_status.json"))
        return False

    if name == "devkit_read_artifact":
        if args.get("artifact_id") == KNOWN_ARTIFACT:
            _tool_result(req_id, _fixture("artifact_read.json"))
        else:
            _tool_error(req_id, name, _fixture("read_artifact_bogus.json"), prefixed=True)
        return False

    if name == "devkit_job_status":
        job_id = args.get("job_id")
        if job_id == RUNNING_JOB:
            _tool_result(req_id, _fixture("job_info_running.json"))
        elif job_id == SUCCEEDED_JOB:
            _tool_result(req_id, _fixture("job_info_succeeded.json"))
        else:
            _tool_error(req_id, name, _fixture("job_status_bogus.json"), prefixed=True)
        return False

    if name == "devkit_deploy":
        token = meta.get("progressToken")
        if token is not None:
            for i in range(3):
                _progress(token, float(i + 1), 5.0, f"phase-{i + 1}")
        _tool_result(req_id, _fixture("job_started.json"))
        if token is not None:
            for i in range(2):
                _progress(token, float(i + 4), 5.0, f"phase-{i + 4}")
        return False

    if name in JOB_STARTER_TOOLS:
        _tool_result(req_id, _fixture("job_started.json"))
        return False
    if name == "devkit_discover":
        _tool_result(req_id, [])
        return False
    if name in RESULT_OK_TOOLS:
        _tool_result(req_id, _fixture("result_ok.json"))
        return False
    if name in FIXTURE_TOOLS:
        _tool_result(req_id, _fixture(FIXTURE_TOOLS[name]))
        return False

    _error(req_id, -32602, f"Unknown tool: {name}")


def _flush_slow(slow_slot: dict) -> None:
    pending = slow_slot.pop("pending", None)
    if pending is not None:
        req_id, payload = pending
        _tool_result(req_id, payload)


def main() -> int:
    slow_slot: dict = {}
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            # Malformed client input: JSON-RPC parse error, keep serving.
            _error(None, -32700, "Parse error")
            continue

        method = msg.get("method")
        req_id = msg.get("id")

        if method is None:
            # A response to something the fake never asked for; ignore.
            continue

        if method == "initialize":
            _result(
                req_id,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}, "resources": {}},
                    "serverInfo": SERVER_INFO,
                },
            )
            continue
        if method == "notifications/initialized" or method == "notifications/cancelled":
            continue  # notifications never get replies
        if method == "ping":
            _result(req_id, {})
            continue
        if method == "tools/list":
            _result(req_id, _fixture("tools_list.json"))
            _flush_slow(slow_slot)
            continue
        if method == "tools/call":
            deferred = _call_tool(req_id, msg.get("params") or {}, slow_slot)
            if not deferred:
                _flush_slow(slow_slot)
            continue
        if method == "resources/read":
            uri = (msg.get("params") or {}).get("uri", "")
            if uri == "devkit://artifact/art_FAKE":
                _result(
                    req_id,
                    {"contents": [{"uri": uri, "mimeType": "image/png", "blob": FAKE_PNG_B64}]},
                )
            else:
                _error(
                    req_id,
                    -32602,
                    f"[ARTIFACT_NOT_FOUND] no such artifact: {uri}",
                    data={"uri": uri},
                )
            _flush_slow(slow_slot)
            continue

        _error(req_id, -32601, f"Method not found: {method}")
        _flush_slow(slow_slot)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
