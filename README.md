# Infrastructure Agent and Management

Gradio-based AI agent for operating a Proxmox fleet and infrastructure control plane.

## What it does

The Infrastructure Agent and Management service provides a chat UI backed by Ollama with tools for:

- **Host maintenance** — system info, apt updates, cleanup, reboot, ansible ad-hoc
- **Proxmox fleet ops** — list/start/stop/reboot guests, deploy LXC/QEMU, resize, node updates via SSH to cluster nodes
- **Array infra control** — MCP gateway bridge for portal, gh-inbox, OpenClaw, KiwiFS, neurolink health and operations

Quick-action buttons in the UI cover fleet inventory, fleet health, and infra health sweeps.

## Architecture

```
sys_agent.py       Gradio UI + LangChain agent + tool routing
fleet_manager.py   Proxmox cluster operations (pct/qm over SSH)
infra_bridge.py    MCP gateway client + infra HTTP probes
fleet.config.json  Cluster nodes, protected VMIDs, infra endpoints
```

## Requirements

- Python 3.11+
- Ollama peer (default `qwen2.5-coder:14b`)
- SSH access from the agent container to Proxmox nodes
- Array MCP gateway token file for infra tools

## Local development

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp config.env.example .env   # adjust paths and URLs
export $(grep -v '^#' .env | xargs)
python sys_agent.py ui
```

CLI modes:

```bash
python sys_agent.py ui            # Gradio on port 7860
python sys_agent.py maintain      # one-shot maintenance pass
python sys_agent.py fleet_refresh # refresh guest map
```

## Deploy to Proxmox

From a Proxmox host or management node with SSH to the cluster:

```bash
export PROXMOX_NODE=your.proxmox.host
export ARA_SYS_VMID=100
export ARA_SYS_IP=10.0.0.11/24
export ARA_SYS_GW=10.0.0.1
./scripts/provision-ara-sys-agent-lxc.sh
```

After deploy, edit `/etc/default/ara-sys-agent` and `fleet.config.json` on the container with your real endpoints. The values committed in this repo are placeholders only (`198.51.100.0/24` documentation addresses and `CHANGE_ME` in Ansible inventory).

Environment overrides:

| Variable | Default | Description |
|----------|---------|-------------|
| `ARA_SYS_VMID` | *(required)* | Proxmox guest ID for the agent container |
| `ARA_SYS_CREATE` | `1` | Create the LXC if missing |
| `PROXMOX_NODE` | *(required)* | Proxmox host for `pct` |
| `ARA_SYS_IP` | *(required on create)* | Static container IP/CIDR |
| `ARA_SYS_GW` | *(required on create)* | Default gateway |
| `ARA_SYS_PORT` | `7860` | Gradio port |

Runtime paths on the container:

- App: `/opt/ara-sys-agent/`
- Config: `/etc/default/ara-sys-agent`
- Service: `systemctl status ara-sys-agent`
- Guest map: `/opt/ara-sys-agent/data/guest-map.json` (copy from `data/guest-map.example.json`)
- MCP tokens: `/opt/ara-sys-agent/secrets/array-agent-tokens.json` (not in repo)

## Fleet config

Edit `fleet.config.json` for cluster nodes, protected VMIDs, and infra endpoint URLs. Use RFC 5737 documentation addresses (`198.51.100.0/24`) in the committed template; replace with your production network before running the agent. Add your manager guest ID to `protectedVmids` so the agent cannot stop or destroy its own container.

## License

Internal ARA / Array fleet tooling.
