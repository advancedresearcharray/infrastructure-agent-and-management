"""Array infrastructure control via MCP gateway and direct HTTP health probes."""

from __future__ import annotations

import asyncio
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import fleet_manager as fm

FLEET_CONFIG_PATH = Path(
    os.environ.get("ARA_FLEET_CONFIG", "/opt/ara-sys-agent/fleet.config.json")
)
TOKEN_FILE = Path(
    os.environ.get(
        "ARA_AGENT_TOKEN_FILE",
        "/opt/ara-sys-agent/secrets/array-agent-tokens.json",
    )
)
MCP_AGENT_ID = os.environ.get("ARA_SYS_MCP_AGENT_ID", "node9-sre")
MCP_GATEWAY_URL = os.environ.get("ARA_MCP_GATEWAY_URL", "").rstrip("/")
MCP_URL = (
    MCP_GATEWAY_URL
    if MCP_GATEWAY_URL.endswith("/mcp")
    else f"{MCP_GATEWAY_URL}/mcp" if MCP_GATEWAY_URL else ""
)


def _load_fleet_config() -> dict[str, Any]:
    if FLEET_CONFIG_PATH.is_file():
        return json.loads(FLEET_CONFIG_PATH.read_text())
    return {}


def _infra_endpoints() -> dict[str, Any]:
    cfg = _load_fleet_config()
    return dict(cfg.get("infra", {}))


def _infra_url(key: str) -> str | None:
    val = _infra_endpoints().get(key)
    if not val:
        return None
    return str(val).strip()


def _load_mcp_token() -> str:
    direct = os.environ.get("ARA_SYS_MCP_TOKEN", "").strip()
    if direct:
        return direct
    if TOKEN_FILE.is_file():
        try:
            tokens = json.loads(TOKEN_FILE.read_text())
            return str(tokens.get(MCP_AGENT_ID) or tokens.get("node9-sre") or "").strip()
        except (json.JSONDecodeError, OSError):
            pass
    return ""


def _text_from_tool_result(result: Any) -> str:
    content = getattr(result, "content", None) or []
    parts: list[str] = []
    for block in content:
        text = getattr(block, "text", None)
        if text:
            parts.append(str(text))
        elif isinstance(block, dict) and block.get("text"):
            parts.append(str(block["text"]))
    if parts:
        return "\n".join(parts).strip()
    if getattr(result, "structuredContent", None):
        return json.dumps(result.structuredContent, indent=2)
    return str(result)


def _http_probe(url: str, timeout: int = 8) -> dict[str, Any]:
    started = __import__("time").time()
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(4096).decode("utf-8", errors="replace")
            return {
                "url": url,
                "ok": 200 <= resp.status < 300,
                "status": resp.status,
                "latencyMs": int((__import__("time").time() - started) * 1000),
                "bodyPreview": body[:500],
            }
    except urllib.error.HTTPError as exc:
        body = exc.read(1024).decode("utf-8", errors="replace") if exc.fp else ""
        return {
            "url": url,
            "ok": False,
            "status": exc.code,
            "latencyMs": int((__import__("time").time() - started) * 1000),
            "bodyPreview": body[:500],
            "error": str(exc),
        }
    except Exception as exc:
        return {
            "url": url,
            "ok": False,
            "status": None,
            "latencyMs": int((__import__("time").time() - started) * 1000),
            "error": str(exc),
        }


async def _mcp_session_call(tool_name: str, arguments: dict[str, Any] | None = None) -> str:
    token = _load_mcp_token()
    if not token:
        return (
            "MCP token missing. Set ARA_SYS_MCP_TOKEN or deploy "
            f"{TOKEN_FILE} with key '{MCP_AGENT_ID}'."
        )

    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client

    headers = {"Authorization": f"Bearer {token}"}
    httpx_client = create_mcp_http_client(headers=headers)

    async with httpx_client:
        async with streamable_http_client(MCP_URL, http_client=httpx_client) as streams:
            read, write, _ = streams
            async with ClientSession(read, write) as session:
                await session.initialize()
                await session.call_tool(
                    "agent_bootstrap",
                    {
                        "agentName": MCP_AGENT_ID,
                        "role": "operator",
                        "profile": "unrestricted",
                        "token": token,
                        "metadata": {"hook": "infra-agent"},
                    },
                )
                result = await session.call_tool(tool_name, arguments or {})
                return _text_from_tool_result(result)


def mcp_invoke(tool_name: str, arguments_json: str = "{}") -> str:
    """Call any Array MCP gateway tool (JSON args)."""
    try:
        arguments = json.loads(arguments_json or "{}")
        if not isinstance(arguments, dict):
            return "arguments_json must be a JSON object."
    except json.JSONDecodeError as exc:
        return f"Invalid arguments_json: {exc}"
    return asyncio.run(_mcp_session_call(tool_name, arguments))


def list_infra_surfaces() -> str:
    """List Array control-plane surfaces from the MCP gateway."""
    return mcp_invoke("list_array_surfaces", "{}")


def portal_request(method: str, path: str, body_json: str = "{}") -> str:
    """Call the chat portal REST API via MCP array_portal_request."""
    try:
        body = json.loads(body_json or "{}")
    except json.JSONDecodeError as exc:
        return f"Invalid body_json: {exc}"
    args = {"method": method.upper(), "path": path, "body": body}
    return mcp_invoke("array_portal_request", json.dumps(args))


def array_http_request(method: str, url: str, body_json: str = "{}") -> str:
    """HTTP request to allowlisted Array internal URLs via MCP."""
    try:
        body = json.loads(body_json or "{}")
    except json.JSONDecodeError as exc:
        return f"Invalid body_json: {exc}"
    args = {"method": method.upper(), "url": url, "body": body}
    return mcp_invoke("array_http_request", json.dumps(args))


def proxmox_mcp_invoke(tool_name: str, arguments_json: str = "{}") -> str:
    """Invoke upstream Proxmox MCP tools (cluster status, alerts, etc.)."""
    try:
        arguments = json.loads(arguments_json or "{}")
    except json.JSONDecodeError as exc:
        return f"Invalid arguments_json: {exc}"
    args = {"toolName": tool_name, "arguments": arguments}
    return mcp_invoke("proxmox_mcp_invoke", json.dumps(args))


def pve_guest_exec(target: str, command: str) -> str:
    """Run a command in an LXC guest via MCP array_pve_guest_exec."""
    guest = fm.resolve_guest(target)
    if not guest:
        return f"Guest not found: {target}"
    if guest["type"] != "lxc":
        return f"array_pve_guest_exec supports LXC only ({guest['name']} is {guest['type']})."
    args = {
        "node": guest["node"],
        "vmid": guest["vmid"],
        "guestType": guest["type"],
        "argv": ["bash", "-lc", command],
    }
    return mcp_invoke("array_pve_guest_exec", json.dumps(args))


def guest_service_control(target: str, action: str, service: str) -> str:
    """systemctl start|stop|restart|status on an LXC guest."""
    action = action.strip().lower()
    if action not in {"start", "stop", "restart", "status", "enable", "disable"}:
        return f"Unsupported action '{action}'."
    service = service.strip()
    if not re.fullmatch(r"[a-zA-Z0-9@._-]+", service):
        return f"Invalid service name: {service}"
    return pve_guest_exec(target, f"systemctl {action} {service}")


def infra_health() -> str:
    """Health sweep across MCP gateway, portal, OpenClaw, gh-inbox, and neurolink peers."""
    checks: list[dict[str, Any]] = []

    gateway = _infra_url("mcpGateway") or MCP_GATEWAY_URL
    if gateway:
        checks.append(_http_probe(f"{gateway.rstrip('/')}/healthz"))
        checks.append(_http_probe(f"{gateway.rstrip('/')}/health"))

    portal = _infra_url("portalBase")
    if portal:
        checks.append(_http_probe(f"{portal.rstrip('/')}/api/health"))

    openclaw = _infra_url("openclawAutomationApi")
    if openclaw:
        checks.append(_http_probe(f"{openclaw.rstrip('/')}/health"))

    gh_inbox = _infra_url("ghInboxDashboard")
    if gh_inbox:
        checks.append(_http_probe(f"{gh_inbox.rstrip('/')}/api/status"))

    for peer in _infra_endpoints().get("neurolinkPeers") or []:
        if peer:
            checks.append(_http_probe(f"{str(peer).rstrip('/')}/api/tags"))

    kiwifs = _infra_url("kiwifsServeBase")
    if kiwifs:
        checks.append(_http_probe(f"{kiwifs.rstrip('/')}/health"))

    summary = {
        "mcpAgentId": MCP_AGENT_ID,
        "mcpUrl": MCP_URL,
        "tokenConfigured": bool(_load_mcp_token()),
        "checks": checks,
        "ok": sum(1 for c in checks if c.get("ok")),
        "total": len(checks),
    }
    return json.dumps(summary, indent=2)


def openclaw_status() -> str:
    """OpenClaw gateway + automation API status."""
    gateway_host = _infra_url("openclawGatewayHost")
    gateway_port = _infra_endpoints().get("openclawGatewayWsPort", 18789)
    automation = _infra_url("openclawAutomationApi")
    report: dict[str, Any] = {}
    if gateway_host:
        report["gateway"] = _http_probe(f"http://{gateway_host}:{gateway_port}/")
    if automation:
        report["automationApi"] = _http_probe(f"{automation.rstrip('/')}/health")
        report["schema"] = _http_probe(f"{automation.rstrip('/')}/v1/automation/openclaw/schema")
    if not report:
        return json.dumps({"error": "openclaw endpoints not configured in fleet.config.json"}, indent=2)
    return json.dumps(report, indent=2)


def gh_inbox_status() -> str:
    """GitHub inbox agent dashboard status."""
    base = _infra_url("ghInboxDashboard")
    if not base:
        return json.dumps({"error": "ghInboxDashboard not configured in fleet.config.json"}, indent=2)
    return json.dumps(
        {
            "dashboard": _http_probe(f"{base.rstrip('/')}/api/status"),
            "portalAgents": portal_request("GET", "/api/agents"),
        },
        indent=2,
    )


def innovation_backlog() -> str:
    """Fetch MCP innovation backlog / cluster improvement radar."""
    return mcp_invoke("innovation_backlog", "{}")


def list_mcp_tools() -> str:
    """List upstream Proxmox MCP tools available via the gateway."""
    return mcp_invoke("proxmox_mcp_tools_list", "{}")
