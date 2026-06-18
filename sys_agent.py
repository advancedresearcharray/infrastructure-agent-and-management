#!/usr/bin/env python3
"""
ARA Enterprise Fleet Manager
Proxmox LXC/VM fleet operations + local host maintenance.
"""

import os
import re
import subprocess
import json
import time
import psutil
import shutil
from datetime import datetime
from pathlib import Path

from langchain_ollama import ChatOllama
from langchain_core.tools import tool
from langchain_core.messages import HumanMessage
import gradio as gr

import fleet_manager as fm
import infra_bridge as ib

OLLAMA_BASE_URL = os.environ.get("ARA_SYS_OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.environ.get("ARA_SYS_OLLAMA_MODEL", "qwen2.5-coder:14b")
UI_PORT = int(os.environ.get("ARA_SYS_PORT", "7860"))
DATA_DIR = Path(os.environ.get("ARA_SYS_DATA_DIR", "/opt/ara-sys-agent/data"))

# ------------------ Tools ------------------


@tool
def get_system_info() -> str:
    """Comprehensive system report."""
    info = {
        "hostname": os.uname().nodename,
        "uptime": str(datetime.now() - datetime.fromtimestamp(psutil.boot_time())),
        "cpu": {"cores": psutil.cpu_count(), "usage": psutil.cpu_percent(interval=0.1)},
        "memory": {
            "total_gb": round(psutil.virtual_memory().total / (1024**3), 1),
            "percent": psutil.virtual_memory().percent,
        },
        "disk": [
            {"mount": p.mountpoint, "percent": psutil.disk_usage(p.mountpoint).percent}
            for p in psutil.disk_partitions()
            if p.fstype and os.path.exists(p.mountpoint)
        ],
        "proxmox": shutil.which("pvesh") is not None,
        "ollama_peer": OLLAMA_BASE_URL,
    }
    return json.dumps(info, indent=2)


@tool
def list_installed_packages(limit: int = 30) -> str:
    """List installed packages."""
    try:
        out = subprocess.check_output(["dpkg", "-l"], text=True, timeout=10)
        pkgs = [line.split()[1] for line in out.splitlines() if line.startswith("ii")]
        return "\n".join(pkgs[:limit])
    except Exception:
        return "Failed to list packages."


@tool
def check_updates() -> str:
    """Check for updates."""
    try:
        subprocess.run(["apt", "update"], capture_output=True, check=False, timeout=30)
        out = subprocess.check_output(["apt", "list", "--upgradable"], text=True, timeout=15)
        lines = [line for line in out.splitlines() if "/" in line]
        return f"{len(lines)} updates available.\nTop: {[l.split('/')[0] for l in lines[:10]]}"
    except Exception as e:
        return f"Update check failed: {e}"


@tool
def apply_updates(auto_confirm: bool = True) -> str:
    """Apply updates (requires root)."""
    if os.geteuid() != 0:
        return "Error: sudo required."
    try:
        subprocess.run(
            ["apt", "upgrade", "-y"] if auto_confirm else ["apt", "upgrade"],
            check=True,
            timeout=300,
        )
        subprocess.run(["apt", "autoremove", "-y"], check=True)
        return "System updated and cleaned."
    except Exception as e:
        return f"Update failed: {e}"


@tool
def cleanup_system() -> str:
    """Full cleanup."""
    actions = []
    try:
        subprocess.run(["apt", "clean"], check=True)
        subprocess.run(["apt", "autoremove", "-y"], check=True)
        actions.append("apt")
        if shutil.which("journalctl"):
            subprocess.run(["journalctl", "--vacuum-time=30d"], check=True)
            actions.append("journal")
        for p in ["/tmp", "/var/tmp"]:
            for f in Path(p).glob("*"):
                try:
                    if f.is_file() and time.time() - f.stat().st_mtime > 7 * 86400:
                        f.unlink(missing_ok=True)
                except OSError:
                    pass
        actions.append("temp")
        return f"Cleanup complete: {', '.join(actions)}"
    except Exception as e:
        return f"Partial cleanup: {e}"


@tool
def optimize_system() -> str:
    """Performance optimizations for AI workloads."""
    if os.geteuid() != 0:
        return "sudo required."
    tweaks = {
        "vm.swappiness": "10",
        "vm.vfs_cache_pressure": "50",
        "net.core.somaxconn": "4096",
    }
    applied = []
    for k, v in tweaks.items():
        r = subprocess.run(["sysctl", "-w", f"{k}={v}"], capture_output=True, check=False)
        if r.returncode == 0:
            applied.append(k)
    return f"Optimizations applied: {', '.join(applied) or 'none (may need privileged CT)'}"


@tool
def reboot_system(delay_seconds: int = 5) -> str:
    """Reboot this host (requires root)."""
    if os.geteuid() != 0:
        return "Error: root required."
    delay = max(3, min(int(delay_seconds), 120))
    subprocess.Popen(
        ["bash", "-c", f"sleep {delay} && systemctl reboot"],
        start_new_session=True,
    )
    return f"Reboot scheduled in {delay} seconds."


@tool
def ansible_adhoc(command: str, hosts: str = "localhost") -> str:
    """Run Ansible ad-hoc command (e.g. 'df -h', 'systemctl status')."""
    try:
        result = subprocess.check_output(
            ["ansible", hosts, "-m", "shell", "-a", command, "-i", "/opt/ara-sys-agent/ansible/hosts"],
            text=True,
            timeout=30,
        )
        return result.strip()
    except Exception as e:
        return f"Ansible failed: {e}"


@tool
def refresh_fleet_inventory() -> str:
    """Refresh Proxmox fleet inventory from all cluster nodes."""
    return json.dumps(fm.refresh_guest_map(), indent=2)


@tool
def list_fleet(running_only: bool = False, guest_type: str = "", node: str = "") -> str:
    """List LXC containers and QEMU VMs across the Proxmox cluster."""
    return fm.list_fleet(running_only=running_only, guest_type=guest_type, node=node)


@tool
def fleet_health() -> str:
    """Fleet-wide health: guest counts, protected services, node status."""
    return fm.fleet_health()


@tool
def node_status() -> str:
    """Proxmox hypervisor node status (load, storage)."""
    return fm.node_status()


@tool
def guest_status(target: str) -> str:
    """Status for a guest by name or VMID (e.g. my-service, 101)."""
    return fm.guest_status(target)


@tool
def guest_start(target: str) -> str:
    """Start an LXC or VM by name or VMID."""
    return fm.guest_start(target)


@tool
def guest_stop(target: str) -> str:
    """Stop an LXC or VM by name or VMID."""
    return fm.guest_stop(target)


@tool
def guest_reboot(target: str) -> str:
    """Reboot an LXC or VM by name or VMID."""
    return fm.guest_reboot(target)


@tool
def guest_exec(target: str, command: str) -> str:
    """Run a shell command inside an LXC guest via pct exec."""
    return fm.guest_exec(target, command)


@tool
def check_node_updates(node: str = "") -> str:
    """Check apt updates on Proxmox hypervisor node(s). Empty node = all nodes."""
    return fm.check_node_updates(node)


@tool
def apply_node_updates(node: str = "") -> str:
    """Apply apt upgrades on Proxmox hypervisor node(s)."""
    return fm.apply_node_updates(node)


@tool
def guest_show_config(target: str) -> str:
    """Show pct/qm config for a guest."""
    return fm.guest_show_config(target)


@tool
def guest_configure(target: str, option: str, value: str) -> str:
    """Configure guest option: memory, cores, onboot, hostname, swap."""
    return fm.guest_configure(target, option, value)


@tool
def deploy_lxc(
    vmid: int,
    hostname: str,
    node: str,
    memory_mb: int = 2048,
    cores: int = 2,
    rootfs_gb: int = 16,
    template: str = "",
    storage: str = "",
    bridge: str = "",
) -> str:
    """Deploy a new LXC container on a Proxmox node."""
    return fm.deploy_lxc(vmid, hostname, node, memory_mb, cores, rootfs_gb, template, storage, bridge)


@tool
def deploy_qemu(
    vmid: int,
    name: str,
    node: str,
    memory_mb: int = 4096,
    cores: int = 2,
    disk_gb: int = 32,
    storage: str = "",
    bridge: str = "",
    iso: str = "",
) -> str:
    """Deploy a new QEMU VM on a Proxmox node."""
    return fm.deploy_qemu(vmid, name, node, memory_mb, cores, disk_gb, storage, bridge, iso)


@tool
def destroy_guest(target: str, purge: bool = False) -> str:
    """Destroy/remove an LXC or VM (protected guests blocked)."""
    return fm.destroy_guest(target, purge=purge)


@tool
def resize_guest(target: str, memory_mb: int = 0, cores: int = 0, disk_gb: int = 0) -> str:
    """Resize guest RAM, CPU cores, or disk."""
    return fm.resize_guest(target, memory_mb=memory_mb, cores=cores, disk_gb=disk_gb)


@tool
def reboot_nodes(node: str = "", delay_seconds: int = 60) -> str:
    """Reboot Proxmox hypervisor node(s). Empty node = all cluster nodes."""
    return fm.reboot_nodes(node=node, delay_seconds=delay_seconds)


@tool
def reboot_all_guests(guest_type: str = "", running_only: bool = True) -> str:
    """Reboot all non-protected guests (optional: lxc or qemu only)."""
    return fm.reboot_all_guests(guest_type=guest_type, running_only=running_only)


@tool
def list_infra_surfaces() -> str:
    """List Array MCP control-plane surfaces (portal, mesh, PVE, neurolink)."""
    return ib.list_infra_surfaces()


@tool
def infra_health() -> str:
    """Health sweep: MCP gateway, portal, OpenClaw, gh-inbox, neurolink peers."""
    return ib.infra_health()


@tool
def mcp_invoke(tool_name: str, arguments_json: str = "{}") -> str:
    """Call any Array MCP gateway tool. Args as JSON object string."""
    return ib.mcp_invoke(tool_name, arguments_json)


@tool
def portal_request(method: str, path: str, body_json: str = "{}") -> str:
    """Portal REST via MCP: GET/POST /api/* paths."""
    return ib.portal_request(method, path, body_json)


@tool
def array_http_request(method: str, url: str, body_json: str = "{}") -> str:
    """Allowlisted internal HTTP via MCP gateway."""
    return ib.array_http_request(method, url, body_json)


@tool
def proxmox_mcp_invoke(tool_name: str, arguments_json: str = "{}") -> str:
    """Invoke upstream Proxmox MCP tool (cluster status, alerts, etc.)."""
    return ib.proxmox_mcp_invoke(tool_name, arguments_json)


@tool
def pve_guest_exec(target: str, command: str) -> str:
    """Run shell in LXC via MCP array_pve_guest_exec (full gateway path)."""
    return ib.pve_guest_exec(target, command)


@tool
def guest_service_control(target: str, action: str, service: str) -> str:
    """systemctl start|stop|restart|status on a guest service."""
    return ib.guest_service_control(target, action, service)


@tool
def openclaw_status() -> str:
    """OpenClaw gateway and automation API health."""
    return ib.openclaw_status()


@tool
def gh_inbox_status() -> str:
    """GitHub inbox agent dashboard and portal agent roster."""
    return ib.gh_inbox_status()


@tool
def innovation_backlog() -> str:
    """MCP innovation backlog / cluster improvement radar."""
    return ib.innovation_backlog()


tools = [
    get_system_info,
    list_installed_packages,
    check_updates,
    apply_updates,
    cleanup_system,
    optimize_system,
    reboot_system,
    ansible_adhoc,
    refresh_fleet_inventory,
    list_fleet,
    fleet_health,
    node_status,
    guest_status,
    guest_start,
    guest_stop,
    guest_reboot,
    guest_exec,
    check_node_updates,
    apply_node_updates,
    guest_show_config,
    guest_configure,
    deploy_lxc,
    deploy_qemu,
    destroy_guest,
    resize_guest,
    reboot_nodes,
    reboot_all_guests,
    list_infra_surfaces,
    infra_health,
    mcp_invoke,
    portal_request,
    array_http_request,
    proxmox_mcp_invoke,
    pve_guest_exec,
    guest_service_control,
    openclaw_status,
    gh_inbox_status,
    innovation_backlog,
]

TOOL_BY_NAME = {t.name: t for t in tools}
RISKY_TOOLS = {
    "apply_updates",
    "cleanup_system",
    "reboot_system",
    "guest_stop",
    "guest_reboot",
    "apply_node_updates",
    "destroy_guest",
    "deploy_lxc",
    "deploy_qemu",
    "resize_guest",
    "reboot_nodes",
    "reboot_all_guests",
    "guest_service_control",
    "mcp_invoke",
    "pve_guest_exec",
}

MAINTENANCE_PROMPT = """You are the ARA Enterprise Fleet Manager for the entire Array infrastructure.
You manage Proxmox LXC/VMs, hypervisor nodes, and the Array control plane via MCP gateway.
Capabilities: fleet inventory, deploy/destroy guests, resize RAM/CPU/disk, guest config,
node apt updates, guest start/stop/reboot/exec, service control (systemctl),
MCP gateway tools (list_array_surfaces, array_portal_request, array_http_request,
proxmox_mcp_invoke, array_pve_guest_exec), portal/OpenClaw/gh-inbox health, local host maintenance.
Protected guests cannot be stopped or destroyed without explicit confirmation."""

llm = ChatOllama(
    model=OLLAMA_MODEL,
    base_url=OLLAMA_BASE_URL,
    temperature=0.2,
    num_predict=512,
    request_timeout=90,
)

MENU_CHOICES = {
    "1": "give me a full system report",
    "2": "check for available updates",
    "3": "perform thorough cleanup",
    "4": "optimize system performance",
    "5": "reboot please",
    "6": "list fleet inventory",
    "7": "fleet health report",
    "8": "node status",
    "9": "check proxmox node updates",
    "10": "show config for workload-a",
}


def _as_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or item))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return str(value)


def _assistant_messages(history: list) -> list[str]:
    return [_as_text(m.get("content", "")) for m in history if m.get("role") == "assistant"]


def _menu_was_offered(history: list) -> bool:
    last = _assistant_messages(history)[-1] if _assistant_messages(history) else ""
    lower = last.lower()
    return "1." in last and "2." in last and any(
        k in lower for k in ("report", "status", "updates", "cleanup", "reboot")
    )


CONFIRM_WORDS = frozenset(
    {"yes", "yes, proceed", "yes proceed", "proceed", "confirm", "approve", "y", "ok", "do it"}
)


def is_confirmation(message: str) -> bool:
    return _as_text(message).strip().lower() in CONFIRM_WORDS


def _confirmation_requested(last_assistant: str) -> bool:
    lower = last_assistant.lower()
    return any(
        k in lower
        for k in (
            "confirm",
            "please type",
            "typing 'yes'",
            "type 'yes'",
            "type yes",
            "to start",
            "before proceeding",
        )
    )


def plan_confirmed_steps(history: list) -> list[tuple[str, dict]]:
    last_assistant = _assistant_messages(history)[-1] if _assistant_messages(history) else ""
    lower = last_assistant.lower()
    steps: list[tuple[str, dict]] = []

    if _confirmation_requested(last_assistant) or any(
        k in lower for k in ("will now apply", "apply the", "start the update", "update and reboot")
    ):
        node = fm.node_from_update_report(last_assistant)
        if "updates available" in lower and node:
            return [("apply_node_updates", {"node": node})]
        if any(k in lower for k in ("update", "upgrade", "install", "packages")):
            steps.append(("apply_updates", {}))
        if "reboot" in lower:
            steps.append(("reboot_system", {}))
        if steps:
            return steps

    if "updates available" in lower:
        node = fm.node_from_update_report(last_assistant)
        if node:
            return [("apply_node_updates", {"node": node})]

    users = [_as_text(m.get("content", "")) for m in history if m.get("role") == "user"]
    for prior in reversed(users):
        if is_confirmation(prior):
            continue
        prior_steps = plan_tool_steps(prior)
        if prior_steps:
            return prior_steps
    return []


def expand_followup(message: str, history: list | None) -> str:
    history = history or []
    message = _as_text(message)
    raw = message.strip()
    text = raw.lower()

    if _menu_was_offered(history):
        pick = text.lstrip("#").strip()
        if pick in MENU_CHOICES:
            return MENU_CHOICES[pick]
        if pick in ("all", "everything", "all of them"):
            return "run full system maintenance cycle"

    if raw.isdigit() and raw in MENU_CHOICES:
        return MENU_CHOICES[raw]

    last_assistant = _assistant_messages(history)[-1] if _assistant_messages(history) else ""
    node_ctx = fm.node_from_update_report(last_assistant)
    if "updates available" in last_assistant.lower() and any(
        x in text for x in ("apply", "install", "upgrade", "those", "them")
    ):
        if node_ctx:
            return f"apply node updates on {node_ctx}"
        return "apply available updates"

    if text in CONFIRM_WORDS and node_ctx and "updates available" in last_assistant.lower():
        return f"apply node updates on {node_ctx}"

    if text in CONFIRM_WORDS:
        pending = plan_confirmed_steps(history)
        if pending:
            names = {name for name, _ in pending}
            if "apply_updates" in names and "reboot_system" in names:
                return "apply available updates and reboot"
            if "apply_updates" in names:
                return "apply available updates"
            if "reboot_system" in names:
                return "reboot please"
        users = [_as_text(m.get("content", "")) for m in history if m.get("role") == "user"]
        for prior_msg in reversed(users):
            if is_confirmation(prior_msg):
                continue
            if any(k in prior_msg.lower() for k in ("maintenance", "maintain", "everything", "all")):
                return "run full system maintenance cycle"
            return prior_msg

    return message


def _conversation_context(history: list, limit: int = 8) -> str:
    lines = []
    for m in history[-limit:]:
        role = "Operator" if m.get("role") == "user" else "Agent"
        lines.append(f"{role}: {_as_text(m.get('content', ''))}")
    return "\n".join(lines)


def plan_tool_steps(message: str) -> list[tuple[str, dict]]:
    message = _as_text(message)
    text = message.lower()

    if any(x in text for x in ("refresh fleet", "sync fleet", "update fleet inventory")):
        return [("refresh_fleet_inventory", {})]
    if any(
        x in text
        for x in (
            "infra health",
            "infrastructure health",
            "full infra health",
            "control plane health",
            "entire infra",
        )
    ):
        return [("infra_health", {})]
    if any(x in text for x in ("list surfaces", "infra surfaces", "array surfaces", "control plane")):
        return [("list_infra_surfaces", {})]
    if any(x in text for x in ("openclaw status", "openclaw health")):
        return [("openclaw_status", {})]
    if any(x in text for x in ("gh inbox", "github inbox", "inbox status")):
        return [("gh_inbox_status", {})]
    if any(x in text for x in ("innovation backlog", "improvement radar", "cluster backlog")):
        return [("innovation_backlog", {})]
    if text.startswith("mcp ") or text.startswith("mcp:"):
        mcp_match = re.search(r"mcp[:\s]+(\w+)\s*(.*)", message, re.I)
        if mcp_match:
            tool = mcp_match.group(1)
            args_raw = mcp_match.group(2).strip()
            args_json = args_raw if args_raw.startswith("{") else "{}"
            return [("mcp_invoke", {"tool_name": tool, "arguments_json": args_json})]
    portal_match = re.search(r"portal\s+(get|post|put|patch|delete)\s+(/api/\S+)", text, re.I)
    if portal_match:
        return [
            (
                "portal_request",
                {
                    "method": portal_match.group(1).upper(),
                    "path": portal_match.group(2),
                },
            )
        ]
    svc_match = re.search(
        r"(start|stop|restart|status|enable|disable)\s+(?:service\s+)?([a-z0-9@._-]+)\s+on\s+([a-z0-9][a-z0-9-]*)",
        text,
        re.I,
    )
    if svc_match:
        return [
            (
                "guest_service_control",
                {
                    "action": svc_match.group(1),
                    "service": svc_match.group(2),
                    "target": svc_match.group(3),
                },
            )
        ]
    if any(
        x in text
        for x in ("list fleet", "fleet inventory", "list containers", "list vms", "show fleet")
    ):
        return [
            (
                "list_fleet",
                {
                    "running_only": "running" in text,
                    "guest_type": "lxc"
                    if any(k in text for k in ("lxc", "container", "ct"))
                    else ("qemu" if any(k in text for k in ("vm", "qemu")) else ""),
                    "node": next((n for n in fm._nodes() if n in text), ""),
                },
            )
        ]
    if any(x in text for x in ("fleet health", "fleet status", "cluster health")):
        return [("fleet_health", {})]
    if any(x in text for x in ("node status", "hypervisor status", "proxmox nodes")):
        return [("node_status", {})]

    node = fm.parse_node_from_text(text)

    if "reboot" in text and any(x in text for x in ("all nodes", "every node", "all hypervisors")):
        return [("reboot_nodes", {"node": ""})]
    if "reboot" in text and "node" in text and "all" in text:
        return [("reboot_nodes", {"node": node})]
    if "reboot" in text and "node" in text and node:
        return [("reboot_nodes", {"node": node})]
    if "reboot" in text and any(
        x in text for x in ("all guests", "all containers", "all lxc", "all vms", "entire fleet", "whole fleet")
    ):
        gtype = "lxc" if any(k in text for k in ("lxc", "container", "ct")) else (
            "qemu" if any(k in text for k in ("vm", "qemu")) else ""
        )
        return [("reboot_all_guests", {"guest_type": gtype, "running_only": True})]

    if re.search(r"\bupdate\s+node\b", text) and not any(
        x in text for x in ("check", "list", "show", "available")
    ):
        if any(x in text for x in ("apply", "install", "upgrade", "run")):
            return [("apply_node_updates", {"node": node})]
        return [("check_node_updates", {"node": node})]

    if any(x in text for x in ("node update", "proxmox update", "hypervisor update")) or (
        "node" in text and "update" in text and "fleet" not in text
    ):
        if any(x in text for x in ("apply", "install", "upgrade")):
            return [("apply_node_updates", {"node": node})]
        return [("check_node_updates", {"node": node})]

    if any(x in text for x in ("deploy lxc", "create lxc", "create container", "provision lxc")):
        vmid = fm.parse_vmid(message)
        mem = fm.parse_memory_mb(message) or 2048
        cores = fm.parse_cores(message) or 2
        disk = fm.parse_disk_gb(message) or 16
        host_match = re.search(
            r"(?:named|called|hostname)\s+([a-z0-9][a-z0-9-]*)", message, re.I
        )
        hostname = host_match.group(1) if host_match else f"ct-{vmid}"
        deploy_node = node or "node9"
        if vmid:
            return [
                (
                    "deploy_lxc",
                    {
                        "vmid": vmid,
                        "hostname": hostname,
                        "node": deploy_node,
                        "memory_mb": mem,
                        "cores": cores,
                        "rootfs_gb": disk,
                    },
                )
            ]

    if any(x in text for x in ("deploy vm", "deploy qemu", "create vm", "create qemu")):
        vmid = fm.parse_vmid(message)
        mem = fm.parse_memory_mb(message) or 4096
        cores = fm.parse_cores(message) or 2
        disk = fm.parse_disk_gb(message) or 32
        name_match = re.search(r"(?:named|called|name)\s+([a-z0-9][a-z0-9-]*)", message, re.I)
        name = name_match.group(1) if name_match else f"vm-{vmid}"
        deploy_node = node or "node9"
        if vmid:
            return [
                (
                    "deploy_qemu",
                    {
                        "vmid": vmid,
                        "name": name,
                        "node": deploy_node,
                        "memory_mb": mem,
                        "cores": cores,
                        "disk_gb": disk,
                    },
                )
            ]

    if any(x in text for x in ("destroy", "delete", "remove")) and any(
        k in text for k in ("ct", "lxc", "vm", "guest", "container")
    ):
        target = fm.parse_guest_target_from_text(message)
        if target:
            return [("destroy_guest", {"target": target, "purge": "purge" in text})]

    resize_target = fm.parse_guest_target_from_text(message)
    if resize_target and any(
        x in text for x in ("resize", "scale", "increase", "add ram", "add memory", "add disk", "more cores")
    ):
        return [
            (
                "resize_guest",
                {
                    "target": resize_target,
                    "memory_mb": fm.parse_memory_mb(message),
                    "cores": fm.parse_cores(message),
                    "disk_gb": fm.parse_disk_gb(message),
                },
            )
        ]

    config_target = fm.parse_guest_target_from_text(message)
    if config_target and "config" in text:
        if any(x in text for x in ("show", "get", "display", "view")):
            return [("guest_show_config", {"target": config_target})]
        set_match = re.search(
            r"(?:set|configure|config)\s+\w+\s+(memory|cores|onboot|hostname|swap)\s+(?:to\s+)?(\S+)",
            message,
            re.I,
        )
        if set_match:
            return [
                (
                    "guest_configure",
                    {
                        "target": config_target,
                        "option": set_match.group(1),
                        "value": set_match.group(2),
                    },
                )
            ]

    exec_match = re.search(r"exec(?:ute)?\s+(?:on\s+)?([^:]+):\s*(.+)", message, re.I)
    if exec_match:
        return [
            (
                "guest_exec",
                {
                    "target": exec_match.group(1).strip(),
                    "command": exec_match.group(2).strip(),
                },
            )
        ]
    run_match = re.search(r"run\s+(.+?)\s+on\s+([a-z0-9][a-z0-9-]*)", message, re.I)
    if run_match:
        return [
            (
                "guest_exec",
                {"target": run_match.group(2).strip(), "command": run_match.group(1).strip()},
            )
        ]

    target = fm.parse_guest_target_from_text(message)
    if target and target.lower() in ("all", "every", "nodes", "guests"):
        target = None
    if target and not any(x in text for x in ("this host", "local host", "manager host")):
        if any(x in text for x in ("start", "power on", "boot")) and "restart" not in text:
            return [("guest_start", {"target": target})]
        if any(x in text for x in ("stop", "shutdown", "power off")):
            return [("guest_stop", {"target": target})]
        if any(x in text for x in ("reboot", "restart")):
            return [("guest_reboot", {"target": target})]
        if "status" in text:
            return [("guest_status", {"target": target})]

    if any(x in text for x in ("full maintenance", "maintenance cycle", "maintain everything")):
        return [
            ("get_system_info", {}),
            ("check_updates", {}),
            ("cleanup_system", {}),
            ("optimize_system", {}),
        ]
    if any(x in text for x in ("system report", "full system report", "system info", "status report")):
        return [("get_system_info", {})]
    if "cleanup" in text or "clean up" in text:
        return [("cleanup_system", {})]
    if "optim" in text:
        return [("optimize_system", {})]
    if "update" in text and any(x in text for x in ("apply", "install", "upgrade", "those", "them")):
        steps = [("apply_updates", {})]
        if "reboot" in text:
            steps.append(("reboot_system", {}))
        return steps
    if "update" in text or "updates" in text:
        if any(k in text for k in ("node", "proxmox", "hypervisor", "pve")):
            if any(x in text for x in ("apply", "install", "upgrade")):
                return [("apply_node_updates", {"node": fm.parse_node_from_text(text)})]
            return [("check_node_updates", {"node": fm.parse_node_from_text(text)})]
        return [("check_updates", {})]
    if "package" in text:
        return [("list_installed_packages", {})]
    if any(x in text for x in ("reboot", "restart system", "restart host")):
        return [("reboot_system", {})]
    if "ansible" in text:
        cmd = message.split("ansible", 1)[-1].strip().strip(":").strip()
        if cmd:
            return [("ansible_adhoc", {"command": cmd})]
    return []


def run_agent(message: str, history: list | None = None) -> str:
    history = history or []
    message = _as_text(message)
    expanded = _as_text(expand_followup(message, history))
    steps = plan_tool_steps(expanded)
    if not steps and is_confirmation(message):
        steps = plan_confirmed_steps(history)
    if not steps:
        ctx = _conversation_context(history)
        prompt = MAINTENANCE_PROMPT
        if ctx:
            prompt += f"\n\nRecent conversation:\n{ctx}"
        prompt += f"\n\nOperator: {message}"
        reply = llm.invoke([HumanMessage(content=prompt)]).content
        return reply or "No response."

    risky = [name for name, _ in steps if name in RISKY_TOOLS]
    explicit = is_confirmation(message) or any(
        w in expanded.lower()
        for w in ("confirm", "approve", "yes", "proceed", "apply", "cleanup", "maintenance", "reboot")
    )
    if risky and not explicit:
        return (
            f"Blocked risky actions ({', '.join(risky)}). "
            "Repeat the request with 'yes, proceed' to confirm."
        )

    outputs = []
    for name, args in steps:
        try:
            result = TOOL_BY_NAME[name].invoke(args)
        except Exception as exc:
            result = f"{name} failed: {exc}"
        outputs.append(f"### {name}\n{result}")

    combined = "\n\n".join(outputs)
    if len(steps) == 1 and steps[0][0] in (
        "get_system_info",
        "reboot_system",
        "check_updates",
        "apply_updates",
        "list_fleet",
        "fleet_health",
        "node_status",
        "guest_status",
        "guest_start",
        "guest_stop",
        "guest_reboot",
        "guest_exec",
        "refresh_fleet_inventory",
        "check_node_updates",
        "apply_node_updates",
        "guest_show_config",
        "guest_configure",
        "deploy_lxc",
        "deploy_qemu",
        "destroy_guest",
        "resize_guest",
        "reboot_nodes",
        "reboot_all_guests",
        "list_infra_surfaces",
        "infra_health",
        "mcp_invoke",
        "portal_request",
        "array_http_request",
        "proxmox_mcp_invoke",
        "pve_guest_exec",
        "guest_service_control",
        "openclaw_status",
        "gh_inbox_status",
        "innovation_backlog",
    ):
        return combined.split(f"### {steps[0][0]}\n", 1)[-1]

    summary = llm.invoke(
        [
            HumanMessage(
                content=(
                    f"Operator request: {expanded}\n\nTool results:\n{combined}\n\n"
                    "Summarize briefly for the operator."
                )
            )
        ]
    ).content
    return summary or combined


# ------------------ Gradio Web UI ------------------


def chat_interface(message: str, history):
    message = _as_text(message)
    if not message or not message.strip():
        return history, ""
    history = list(history or [])
    try:
        response = run_agent(message, history)
    except Exception as exc:
        response = f"Agent error: {exc}"
    history.append({"role": "user", "content": message})
    history.append({"role": "assistant", "content": response or "No response from agent."})
    return history, ""


def build_demo():
    with gr.Blocks(title="ARA Fleet Manager") as demo:
        gr.Markdown("# ARA Enterprise Fleet Manager")
        gr.Markdown(
            "Manage Proxmox nodes, LXC, VMs, and the full Array control plane. Examples: "
            "`infra health`, `list surfaces`, `portal GET /api/agents`, "
            "`restart portal-service on guest 101`, `mcp list_array_surfaces`, "
            "`deploy lxc 102 named myapp on node9 4gb 2 cores`"
        )
        chatbot = gr.Chatbot(height=600)
        msg = gr.Textbox(
            placeholder="Fleet ops: list fleet, start guest 101, reboot workload-a, node status..."
        )
        with gr.Row():
            gr.Button("Fleet Inventory").click(
                lambda h: chat_interface("list fleet inventory", h),
                chatbot,
                [chatbot, msg],
            )
            gr.Button("Fleet Health").click(
                lambda h: chat_interface("fleet health report", h),
                chatbot,
                [chatbot, msg],
            )
            gr.Button("Node Status").click(
                lambda h: chat_interface("proxmox node status", h),
                chatbot,
                [chatbot, msg],
            )
            gr.Button("Node Updates").click(
                lambda h: chat_interface("check proxmox node updates on all nodes", h),
                chatbot,
                [chatbot, msg],
            )
            gr.Button("Infra Health").click(
                lambda h: chat_interface("full infra health report", h),
                chatbot,
                [chatbot, msg],
            )
            gr.Button("Array Surfaces").click(
                lambda h: chat_interface("list array control plane surfaces", h),
                chatbot,
                [chatbot, msg],
            )
        with gr.Row():
            gr.Button("Local Maintenance").click(
                lambda h: chat_interface("Run full system maintenance cycle", h),
                chatbot,
                [chatbot, msg],
            )
            gr.Button("Local Report").click(
                lambda h: chat_interface("Give me a full system report", h),
                chatbot,
                [chatbot, msg],
            )

        msg.submit(chat_interface, [msg, chatbot], [chatbot, msg])
    return demo


demo = None

# ------------------ CLI Fallback ------------------

if __name__ == "__main__":
    import typer

    app = typer.Typer()

    @app.command()
    def ui():
        """Launch Gradio Web UI"""
        global demo
        if demo is None:
            demo = build_demo()
        demo.launch(server_name="0.0.0.0", server_port=UI_PORT, share=False)

    @app.command()
    def maintain():
        """CLI full maintenance"""
        print(run_agent("Perform full maintenance: report, updates, cleanup, optimize."))

    @app.command()
    def fleet_refresh():
        """Refresh fleet inventory from Proxmox nodes"""
        print(json.dumps(fm.refresh_guest_map(), indent=2))

    app()
