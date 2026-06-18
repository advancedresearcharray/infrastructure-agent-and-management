"""Proxmox fleet operations for ARA enterprise manager."""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

FLEET_CONFIG_PATH = Path(
    os.environ.get("ARA_FLEET_CONFIG", "/opt/ara-sys-agent/fleet.config.json")
)
DEFAULT_GUEST_MAP = Path(
    os.environ.get("ARA_GUEST_MAP", "/opt/ara-sys-agent/data/guest-map.json")
)


def _load_fleet_config() -> dict[str, Any]:
    if FLEET_CONFIG_PATH.is_file():
        return json.loads(FLEET_CONFIG_PATH.read_text())
    return {
        "nodes": {},
        "protectedVmids": [],
        "guestMapPath": str(DEFAULT_GUEST_MAP),
    }


def _guest_map_path() -> Path:
    cfg = _load_fleet_config()
    return Path(cfg.get("guestMapPath", str(DEFAULT_GUEST_MAP)))


def _protected_vmids() -> set[int]:
    return {int(v) for v in _load_fleet_config().get("protectedVmids", [])}


def _nodes() -> dict[str, str]:
    return dict(_load_fleet_config().get("nodes", {}))


def _ssh(node: str, command: str, timeout: int = 90) -> str:
    nodes = _nodes()
    host = nodes.get(node)
    if not host:
        return f"Unknown node '{node}'. Known: {', '.join(sorted(nodes))}"
    proc = subprocess.run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            "ConnectTimeout=10",
            f"root@{host}",
            command,
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode != 0:
        return f"SSH {node} ({host}) failed ({proc.returncode}): {err or out or 'no output'}"
    return out or "(ok)"


def _parse_pct_qm_table(output: str, guest_type: str, node: str) -> list[dict[str, Any]]:
    guests: list[dict[str, Any]] = []
    for line in output.splitlines():
        line = line.strip()
        if not line or line.lower().startswith("vmid"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            vmid = int(parts[0])
        except ValueError:
            continue
        status = parts[1]
        name = parts[-1] if len(parts) >= 3 else f"{guest_type}-{vmid}"
        guests.append(
            {
                "vmid": vmid,
                "name": name,
                "node": node,
                "type": guest_type,
                "status": status,
                "ip": "",
            }
        )
    return guests


def refresh_guest_map() -> dict[str, Any]:
    guests: list[dict[str, Any]] = []
    errors: list[str] = []
    for node in sorted(_nodes()):
        try:
            lxc_out = _ssh(node, "pct list")
            guests.extend(_parse_pct_qm_table(lxc_out, "lxc", node))
        except Exception as exc:
            errors.append(f"{node} lxc: {exc}")
        try:
            qm_out = _ssh(node, "qm list")
            guests.extend(_parse_pct_qm_table(qm_out, "qemu", node))
        except Exception as exc:
            errors.append(f"{node} qemu: {exc}")

    payload = {
        "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "guests": sorted(guests, key=lambda g: (g["node"], g["vmid"])),
        "errors": errors,
    }
    path = _guest_map_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2))
    return {
        "guests": len(guests),
        "nodes": len(_nodes()),
        "errors": errors,
        "path": str(path),
    }


def _load_guests() -> list[dict[str, Any]]:
    path = _guest_map_path()
    if not path.is_file():
        refresh_guest_map()
    if not path.is_file():
        return []
    data = json.loads(path.read_text())
    return list(data.get("guests", []))


def resolve_guest(target: str) -> dict[str, Any] | None:
    target = str(target).strip()
    guests = _load_guests()
    if not guests:
        return None

    if target.isdigit():
        vmid = int(target)
        for g in guests:
            if g["vmid"] == vmid:
                return g

    needle = target.lower()
    matches = [
        g
        for g in guests
        if needle in g.get("name", "").lower() or needle in str(g.get("vmid", ""))
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        names = ", ".join(f"{g['name']}({g['vmid']})" for g in matches[:8])
        raise ValueError(f"Ambiguous target '{target}': {names}")
    return None


def _guest_cmd(guest: dict[str, Any], action: str) -> str:
    vmid = guest["vmid"]
    node = guest["node"]
    if guest["type"] == "lxc":
        return _ssh(node, f"pct {action} {vmid}")
    return _ssh(node, f"qm {action} {vmid}")


def _check_protected(guest: dict[str, Any], action: str) -> str | None:
    if guest["vmid"] not in _protected_vmids():
        return None
    blocked = {"stop", "shutdown", "reboot", "destroy", "delete", "remove"}
    if action in blocked:
        return (
            f"Refusing {action} on protected guest {guest['name']} (CT/VM {guest['vmid']}). "
            "Remove from protectedVmids in fleet.config.json or confirm with explicit override."
        )
    return None


def _resolve_nodes(node: str = "") -> list[str]:
    nodes = _nodes()
    if not node:
        return sorted(nodes)
    key = node.strip().lower()
    if key in nodes:
        return [key]
    for name in nodes:
        if key in name.lower():
            return [name]
    raise ValueError(f"Unknown node '{node}'. Known: {', '.join(sorted(nodes))}")


def _cfg_defaults() -> dict[str, str]:
    cfg = _load_fleet_config()
    return {
        "template": cfg.get(
            "defaultLxcTemplate", "local:vztmpl/debian-12-standard_12.12-1_amd64.tar.zst"
        ),
        "storage": cfg.get("defaultLxcStorage", "local-lvm"),
        "bridge": cfg.get("defaultBridge", "vmbr0"),
        "nameserver": cfg.get("defaultNameserver", ""),
    }


def list_fleet(
    running_only: bool = False,
    guest_type: str = "",
    node: str = "",
) -> str:
    guests = _load_guests()
    if not guests:
        summary = refresh_guest_map()
        guests = _load_guests()
        if not guests:
            return f"No fleet inventory. Refresh errors: {summary.get('errors')}"

    if running_only:
        guests = [g for g in guests if g.get("status") == "running"]
    if guest_type in ("lxc", "qemu"):
        guests = [g for g in guests if g.get("type") == guest_type]
    if node:
        guests = [g for g in guests if g.get("node", "").lower() == node.lower()]

    lines = [
        f"{'VMID':>5}  {'TYPE':<5}  {'STATUS':<10}  {'NODE':<12}  NAME",
        "-" * 64,
    ]
    for g in guests:
        lines.append(
            f"{g['vmid']:>5}  {g.get('type', '?'):<5}  {g.get('status', '?'):<10}  "
            f"{g.get('node', '?'):<12}  {g.get('name', '')}"
        )
    return f"Fleet inventory ({len(guests)} guests)\n" + "\n".join(lines)


def guest_status(target: str) -> str:
    guest = resolve_guest(target)
    if not guest:
        return f"Guest not found: {target}"
    live = _guest_cmd(guest, "status")
    return json.dumps({**guest, "liveStatus": live}, indent=2)


def guest_start(target: str) -> str:
    guest = resolve_guest(target)
    if not guest:
        return f"Guest not found: {target}"
    return _guest_cmd(guest, "start")


def guest_stop(target: str) -> str:
    guest = resolve_guest(target)
    if not guest:
        return f"Guest not found: {target}"
    blocked = _check_protected(guest, "stop")
    if blocked:
        return blocked
    return _guest_cmd(guest, "stop")


def guest_reboot(target: str) -> str:
    guest = resolve_guest(target)
    if not guest:
        return f"Guest not found: {target}"
    blocked = _check_protected(guest, "reboot")
    if blocked:
        return blocked
    if guest["type"] == "lxc":
        return _ssh(guest["node"], f"pct reboot {guest['vmid']}")
    return _ssh(guest["node"], f"qm reboot {guest['vmid']}")


def guest_exec(target: str, command: str) -> str:
    guest = resolve_guest(target)
    if not guest:
        return f"Guest not found: {target}"
    if guest["type"] != "lxc":
        return (
            f"Guest exec via hypervisor only supported for LXC ({guest['name']}). "
            "Use SSH to the VM or qemu-guest-agent."
        )
    safe_cmd = command.replace('"', '\\"')
    return _ssh(guest["node"], f'pct exec {guest["vmid"]} -- bash -lc "{safe_cmd}"', timeout=120)


def node_status() -> str:
    lines = []
    for node, host in sorted(_nodes().items()):
        try:
            uptime = _ssh(node, "uptime -p")
            load = _ssh(node, "cat /proc/loadavg")
            storage = _ssh(node, "pvesm status 2>/dev/null | head -8")
            lines.append(f"## {node} ({host})\nUptime: {uptime}\nLoad: {load}\n{storage}")
        except Exception as exc:
            lines.append(f"## {node} ({host})\nError: {exc}")
    return "\n\n".join(lines) if lines else "No Proxmox nodes configured."


def fleet_health() -> str:
    refresh = refresh_guest_map()
    guests = _load_guests()
    running = sum(1 for g in guests if g.get("status") == "running")
    stopped = sum(1 for g in guests if g.get("status") == "stopped")
    lxc = sum(1 for g in guests if g.get("type") == "lxc")
    vms = sum(1 for g in guests if g.get("type") == "qemu")
    protected = [g for g in guests if g["vmid"] in _protected_vmids()]

    report = {
        "generatedAt": refresh.get("path"),
        "totals": {
            "guests": len(guests),
            "running": running,
            "stopped": stopped,
            "lxc": lxc,
            "qemu": vms,
        },
        "protected": [
            {"vmid": g["vmid"], "name": g["name"], "status": g.get("status")}
            for g in protected
        ],
        "nodes": node_status(),
    }
    return json.dumps(report, indent=2)


def parse_guest_target_from_text(text: str) -> str | None:
    patterns = [
        r"(?:ct|lxc|vm|guest)\s*#?(\d+)",
        r"(?:start|stop|reboot|restart|status|exec on|destroy|remove|delete|resize|config)\s+([a-z0-9][a-z0-9-]*)",
        r"\b(\d{3,4})\b",
    ]
    lower = text.lower()
    for pattern in patterns:
        match = re.search(pattern, lower)
        if match:
            return match.group(1)
    for token in re.findall(r"[a-z][a-z0-9-]{2,}", lower):
        if token in (
            "fleet",
            "node",
            "nodes",
            "cluster",
            "system",
            "available",
            "please",
            "deploy",
            "create",
            "proxmox",
            "updates",
            "update",
            "all",
            "every",
            "entire",
            "guests",
            "containers",
        ):
            continue
        if "-" in token or token.startswith("array") or token.endswith("ai"):
            return token
    return None


def parse_node_from_text(text: str) -> str:
    lower = text.lower()
    for node in _nodes():
        if node.lower() in lower:
            return node
    return ""


def parse_memory_mb(text: str) -> int:
    match = re.search(r"(\d+(?:\.\d+)?)\s*(gb|g|mb|m)\b", text, re.I)
    if not match:
        return 0
    value = float(match.group(1))
    unit = match.group(2).lower()
    if unit.startswith("g"):
        return int(value * 1024)
    return int(value)


def parse_disk_gb(text: str) -> int:
    match = re.search(r"(\d+(?:\.\d+)?)\s*(gb|g)\s*(?:disk|storage|rootfs|root)", text, re.I)
    if match:
        return int(float(match.group(1)))
    match = re.search(r"(?:disk|storage|rootfs|add)\s*(\d+)\s*g", text, re.I)
    return int(match.group(1)) if match else 0


def parse_cores(text: str) -> int:
    match = re.search(r"(\d+)\s*cores?", text, re.I)
    return int(match.group(1)) if match else 0


def parse_vmid(text: str) -> int:
    match = re.search(r"\b(\d{3,4})\b", text)
    return int(match.group(1)) if match else 0


def node_from_update_report(text: str) -> str:
    """Extract Proxmox node name from a check_node_updates report."""
    match = re.search(r"^##\s+(\S+)", text, re.MULTILINE)
    if not match:
        return ""
    name = match.group(1).strip().lower()
    for node in _nodes():
        if node.lower() == name:
            return node
    return ""


def check_node_updates(node: str = "") -> str:
    """Check apt upgradable packages on Proxmox hypervisor node(s)."""
    lines = []
    for n in _resolve_nodes(node):
        out = _ssh(
            n,
            "export DEBIAN_FRONTEND=noninteractive; "
            "apt-get update -qq 2>/dev/null; "
            "apt list --upgradable 2>/dev/null | grep -v '^Listing' | head -20",
            timeout=180,
        )
        count_match = re.findall(r"/", out)
        count = max(0, len(count_match))
        lines.append(f"## {n}\n{count} updates available\n{out}")
    body = "\n\n".join(lines)
    if node or len(_resolve_nodes(node)) == 1:
        n = _resolve_nodes(node)[0]
        body += f"\n\nTo upgrade, reply: apply node updates on {n}"
    else:
        body += "\n\nTo upgrade all nodes, reply: apply proxmox node updates"
    return body


def apply_node_updates(node: str = "") -> str:
    """Apply apt upgrade on Proxmox hypervisor node(s)."""
    lines = []
    for n in _resolve_nodes(node):
        out = _ssh(
            n,
            "export DEBIAN_FRONTEND=noninteractive; "
            "apt-get update -qq && apt-get upgrade -y && apt-get autoremove -y",
            timeout=900,
        )
        lines.append(f"## {n}\n{out}")
    return "\n\n".join(lines)


def guest_show_config(target: str) -> str:
    guest = resolve_guest(target)
    if not guest:
        return f"Guest not found: {target}"
    if guest["type"] == "lxc":
        return _ssh(guest["node"], f"pct config {guest['vmid']}")
    return _ssh(guest["node"], f"qm config {guest['vmid']}")


def guest_configure(target: str, option: str, value: str) -> str:
    """Set a guest option (memory, cores, onboot, hostname, etc.)."""
    guest = resolve_guest(target)
    if not guest:
        return f"Guest not found: {target}"
    key = option.strip().lstrip("-").lower()
    aliases = {
        "memory": "memory",
        "ram": "memory",
        "cores": "cores",
        "cpu": "cores",
        "onboot": "onboot",
        "hostname": "hostname",
        "swap": "swap",
    }
    field = aliases.get(key, key)
    prefix = "pct" if guest["type"] == "lxc" else "qm"
    return _ssh(guest["node"], f"{prefix} set {guest['vmid']} -{field} {value}")


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
    """Create a new LXC container on a Proxmox node."""
    defaults = _cfg_defaults()
    template = template or defaults["template"]
    storage = storage or defaults["storage"]
    bridge = bridge or defaults["bridge"]
    nameserver = defaults["nameserver"]
    node_name = _resolve_nodes(node)[0]
    rootfs = f"{storage}:{rootfs_gb}"
    cmd = (
        f"pct create {vmid} {template} "
        f"--hostname {hostname} "
        f"--memory {memory_mb} --cores {cores} --swap 256 "
        f"--rootfs {rootfs} "
        f"--net0 name=eth0,bridge={bridge},ip=dhcp "
        f"--nameserver {nameserver} --searchdomain array.local "
        f"--unprivileged 1 --onboot 1 --features nesting=1"
    )
    result = _ssh(node_name, cmd, timeout=300)
    refresh_guest_map()
    return f"Created LXC {vmid} ({hostname}) on {node_name}\n{result}"


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
    """Create a basic QEMU VM (empty disk or from ISO)."""
    defaults = _cfg_defaults()
    storage = storage or defaults["storage"]
    bridge = bridge or defaults["bridge"]
    node_name = _resolve_nodes(node)[0]
    if iso:
        disk_arg = f"--scsi0 {storage}:{disk_gb},format=qcow2 --ide2 {iso},media=cdrom"
        boot = "--boot order=ide2;scsi0"
    else:
        disk_arg = f"--scsi0 {storage}:{disk_gb},format=qcow2"
        boot = "--boot order=scsi0"
    cmd = (
        f"qm create {vmid} --name {name} --memory {memory_mb} --cores {cores} "
        f"--net0 virtio,bridge={bridge} {disk_arg} {boot} --ostype l26 --agent 1 --onboot 1"
    )
    result = _ssh(node_name, cmd, timeout=300)
    refresh_guest_map()
    return f"Created VM {vmid} ({name}) on {node_name}\n{result}"


def destroy_guest(target: str, purge: bool = False) -> str:
    """Destroy/remove an LXC or QEMU guest."""
    guest = resolve_guest(target)
    if not guest:
        return f"Guest not found: {target}"
    blocked = _check_protected(guest, "destroy")
    if blocked:
        return blocked
    vmid = guest["vmid"]
    node = guest["node"]
    try:
        _guest_cmd(guest, "stop")
    except Exception:
        pass
    purge_flag = " --purge 1" if purge else ""
    if guest["type"] == "lxc":
        result = _ssh(node, f"pct destroy {vmid}{purge_flag}")
    else:
        purge_flag = " --purge" if purge else ""
        result = _ssh(node, f"qm destroy {vmid}{purge_flag}")
    refresh_guest_map()
    return result


def resize_guest(
    target: str,
    memory_mb: int = 0,
    cores: int = 0,
    disk_gb: int = 0,
) -> str:
    """Add memory, CPU cores, or disk to a guest."""
    guest = resolve_guest(target)
    if not guest:
        return f"Guest not found: {target}"
    vmid = guest["vmid"]
    node = guest["node"]
    actions: list[str] = []
    if memory_mb > 0:
        tool = "pct" if guest["type"] == "lxc" else "qm"
        actions.append(_ssh(node, f"{tool} set {vmid} -memory {memory_mb}"))
    if cores > 0:
        tool = "pct" if guest["type"] == "lxc" else "qm"
        actions.append(_ssh(node, f"{tool} set {vmid} -cores {cores}"))
    if disk_gb > 0:
        if guest["type"] == "lxc":
            actions.append(_ssh(node, f"pct resize {vmid} rootfs +{disk_gb}G"))
        else:
            actions.append(_ssh(node, f"qm resize {vmid} scsi0 +{disk_gb}G"))
    if not actions:
        return "No resize parameters provided (memory_mb, cores, disk_gb)."
    return "\n".join(actions)


def reboot_nodes(node: str = "", delay_seconds: int = 60) -> str:
    """Schedule reboot of Proxmox hypervisor node(s). Empty node = all cluster nodes."""
    delay = max(30, min(int(delay_seconds), 600))
    minutes = max(1, delay // 60)
    lines = []
    targets = _resolve_nodes(node)
    for n in targets:
        result = _ssh(
            n,
            f"shutdown -r +{minutes} 'ARA fleet manager scheduled hypervisor reboot'",
            timeout=30,
        )
        lines.append(f"## {n}\nScheduled reboot in ~{minutes} min\n{result}")
    if len(targets) > 1:
        lines.append(
            "\nWARNING: Multiple hypervisors rebooting — cluster services will be disrupted."
        )
    return "\n\n".join(lines)


def reboot_all_guests(guest_type: str = "", running_only: bool = True) -> str:
    """Reboot all non-protected guests (optionally LXC or QEMU only)."""
    guests = _load_guests()
    if running_only:
        guests = [g for g in guests if g.get("status") == "running"]
    if guest_type in ("lxc", "qemu"):
        guests = [g for g in guests if g.get("type") == guest_type]

    if not guests:
        return "No matching guests to reboot."

    lines = []
    for g in guests:
        blocked = _check_protected(g, "reboot")
        if blocked:
            lines.append(blocked)
            continue
        if g["type"] == "lxc":
            result = _ssh(g["node"], f"pct reboot {g['vmid']}")
        else:
            result = _ssh(g["node"], f"qm reboot {g['vmid']}")
        lines.append(f"{g['name']} ({g['vmid']}): {result}")
    return "\n".join(lines)
