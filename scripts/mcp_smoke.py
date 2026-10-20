#!/usr/bin/env python3
"""MCP smoke run against a real device (milestone 1, MCP half).

Drives the devkit MCP server over stdio with the official mcp SDK client —
the same JSON-RPC any MCP host speaks, no LLM involved. Exercises the
overview's example workflow: select a paired Deck, upload a build, launch it,
retrieve startup logs and a screenshot.

Usage: uv run python scripts/mcp_smoke.py [device-selector]
Exit code 0 iff every step passes. Prints one JSON report line per step.
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO = Path(__file__).resolve().parent.parent
GAMEID = "zz_mcp_smoke"
GAMEID_RE = GAMEID  # readability alias

REPORT: list[dict[str, Any]] = []


def step(name: str, ok: bool, **details: Any) -> None:
    REPORT.append({"step": name, "ok": ok, **details})
    print(json.dumps({"step": name, "ok": ok, **details}, default=str), flush=True)


def structured(result: Any) -> Any:
    """The tool result payload: structured_content when present, else text JSON."""
    if getattr(result, "isError", False):
        text = "".join(getattr(c, "text", "") for c in result.content)
        raise RuntimeError(f"tool error: {text[:400]}")
    sc = getattr(result, "structured_content", None)
    if sc is not None:
        return sc
    text = "".join(getattr(c, "text", "") for c in result.content)
    return json.loads(text) if text else None


async def wait_job(session: ClientSession, job_id: str, timeout_s: float = 180.0) -> dict[str, Any]:
    """Poll devkit_job_status until terminal."""
    deadline = asyncio.get_event_loop().time() + timeout_s
    while True:
        info = structured(
            await session.call_tool("devkit_job_status", {"job_id": job_id}, None)
        )
        if info["state"] in ("succeeded", "failed", "cancelled", "timeout", "interrupted"):
            return info
        if asyncio.get_event_loop().time() > deadline:
            raise TimeoutError(f"job {job_id} did not finish in {timeout_s}s")
        await asyncio.sleep(1.0)


async def main(device_arg: str | None) -> int:
    params = StdioServerParameters(command="uv", args=["run", "devkit-mcp"], cwd=str(REPO))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            tool_names = {t.name for t in tools.tools}
            expected = {
                "devkit_list_devices",
                "devkit_status",
                "devkit_deploy",
                "devkit_launch_title",
                "devkit_fetch_logs",
                "devkit_screenshot",
                "devkit_job_status",
            }
            step("tools_surface", expected <= tool_names, tool_count=len(tool_names))

            devices = structured(await session.call_tool("devkit_list_devices", {}, None))
            device = device_arg or (devices[0]["name"] if devices else None)
            if device is None:
                step("device_present", False, reason="no paired device in the registry")
                raise SystemExit(1)
            step("device_present", True, device=device)

            # clean slate: a previous crashed run may have left the title deployed
            try:
                await session.call_tool(
                    "devkit_delete_title",
                    {"device": device, "gameid": GAMEID, "confirm": True},
                    None,
                )
            except Exception:
                pass  # absent title is fine

            status = structured(await session.call_tool("devkit_status", {"device": device}, None))
            step(
                "status",
                bool(status.get("steam_status")),
                steam_status=status.get("steam_status"),
                os_version=status.get("os_version"),
            )

            # deploy payload: a tiny shell-script title
            with tempfile.TemporaryDirectory(prefix="devkit-mcp-smoke-") as tmp:
                payload = Path(tmp) / "payload"
                payload.mkdir()
                entry = payload / "run.sh"
                entry.write_text("#!/bin/sh\necho 'mcp smoke alive'\nsleep 2\n", encoding="utf-8")
                entry.chmod(0o755)

                cfg = structured(
                    await session.call_tool(
                        "devkit_configure_title",
                        {"device": device, "gameid": GAMEID, "entrypoint": "run.sh"},
                        None,
                    )
                )
                step("configure_title", bool(cfg.get("ok")))

                preview = structured(
                    await session.call_tool(
                        "devkit_preview_deploy",
                        {"device": device, "gameid": GAMEID, "source_dir": str(payload)},
                        None,
                    )
                )
                step(
                    "preview_deploy",
                    preview.get("files_added", 0) >= 1,
                    files_added=preview.get("files_added"),
                    bytes_to_transfer=preview.get("bytes_to_transfer"),
                )

                deploy = structured(
                    await session.call_tool(
                        "devkit_deploy",
                        {
                            "device": device,
                            "gameid": GAMEID,
                            "source_dir": str(payload),
                            "register": True,
                            "launch": False,
                        },
                        None,
                    )
                )
                deploy_info = await wait_job(session, deploy["job_id"])
                step(
                    "deploy",
                    deploy_info["state"] == "succeeded",
                    job_id=deploy["job_id"],
                    state=deploy_info["state"],
                )

            try:
                launch = structured(
                    await session.call_tool(
                        "devkit_launch_title", {"device": device, "gameid": GAMEID}, None
                    )
                )
                step(
                    "launch",
                    bool(launch.get("launched")) and launch.get("readiness") == "unknown",
                    launched=launch.get("launched"),
                    readiness=launch.get("readiness"),
                )

                logs = structured(
                    await session.call_tool(
                        "devkit_fetch_logs", {"device": device, "kinds": ["steam"]}, None
                    )
                )
                logs_info = await wait_job(session, logs["job_id"], timeout_s=300)
                step(
                    "fetch_logs",
                    logs_info["state"] == "succeeded" and bool(logs_info.get("artifacts")),
                    state=logs_info["state"],
                    artifacts=logs_info.get("artifacts"),
                )

                shot = structured(
                    await session.call_tool(
                        "devkit_screenshot", {"device": device, "mode": 3}, None
                    )
                )
                shot_info = await wait_job(session, shot["job_id"], timeout_s=120)
                shot_ok = shot_info["state"] == "succeeded" and bool(shot_info.get("artifacts"))
                png_ok = False
                if shot_ok:
                    artifact_id = shot_info["artifacts"][0]
                    resource = await session.read_resource(f"devkit://artifact/{artifact_id}")
                    blob = getattr(resource.contents[0], "blob", None)
                    if blob:
                        png_ok = base64.b64decode(blob).startswith(b"\x89PNG")
                step("screenshot", shot_ok and png_ok, png_verified=png_ok)
            finally:
                # the smoke title must not linger on the device, pass or fail
                cleanup = structured(
                    await session.call_tool(
                        "devkit_delete_title",
                        {"device": device, "gameid": GAMEID, "confirm": True},
                        None,
                    )
                )
                step("cleanup_delete_title", bool(cleanup.get("ok")))

    ok = all(item["ok"] for item in REPORT)
    print(json.dumps({"mcp_smoke": "pass" if ok else "fail", "steps": len(REPORT)}), flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    device_arg = sys.argv[1] if len(sys.argv) > 1 else None
    raise SystemExit(asyncio.run(main(device_arg)))
