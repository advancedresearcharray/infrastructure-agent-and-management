#!/usr/bin/env bash
# Proxmox LXC — Infrastructure Agent and Management (Gradio UI).
#
# Required env (set before first create):
#   PROXMOX_NODE   Proxmox host for pct/ssh
#   ARA_SYS_VMID   Proxmox guest ID for the agent container
#   ARA_SYS_IP     Container static IP with CIDR (e.g. 10.0.0.11/24)
#   ARA_SYS_GW     Default gateway
#
# Optional:
#   ARA_SYS_CREATE=1
#   ARA_SYS_DNS=198.51.100.1
#   ARA_SYS_DNS2=
#   ARA_SYS_SEARCHDOMAIN=lan.local
#
set -euo pipefail

VMID="${ARA_SYS_VMID:?Set ARA_SYS_VMID to your Proxmox guest ID}"
CREATE="${ARA_SYS_CREATE:-1}"
ROOTFS="${ARA_SYS_ROOTFS:-local-lvm:16}"
TEMPLATE="${ARA_SYS_TEMPLATE:-local:vztmpl/debian-12-standard_12.12-1_amd64.tar.zst}"
PROXMOX_NODE="${PROXMOX_NODE:?Set PROXMOX_NODE to your Proxmox host}"
ARA_SYS_IP="${ARA_SYS_IP:?Set ARA_SYS_IP e.g. 10.0.0.11/24}"
ARA_SYS_GW="${ARA_SYS_GW:?Set ARA_SYS_GW to your default gateway}"
ARA_SYS_DNS="${ARA_SYS_DNS:-$ARA_SYS_GW}"
ARA_SYS_DNS2="${ARA_SYS_DNS2:-}"
ARA_SYS_SEARCHDOMAIN="${ARA_SYS_SEARCHDOMAIN:-lan.local}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
STAGE="$(cd "${SCRIPT_DIR}/.." && pwd)"
HOSTNAME="${ARA_SYS_HOSTNAME:-infra-agent}"
PORT="${ARA_SYS_PORT:-7860}"

run_pct() {
  if command -v pct >/dev/null 2>&1 && pct status "$VMID" &>/dev/null 2>&1; then
    pct "$@"
  else
    ssh "root@${PROXMOX_NODE}" pct "$@"
  fi
}

push_file() {
  local local_path="$1"
  local remote_path="$2"
  if command -v pct >/dev/null 2>&1 && pct status "$VMID" &>/dev/null 2>&1; then
    pct push "$VMID" "$local_path" "$remote_path"
  else
    ssh "root@${PROXMOX_NODE}" "pct exec ${VMID} -- mkdir -p $(dirname "$remote_path")"
    cat "$local_path" | ssh "root@${PROXMOX_NODE}" "pct exec ${VMID} -- tee ${remote_path} >/dev/null"
  fi
}

remote_bash() {
  if command -v pct >/dev/null 2>&1 && pct status "$VMID" &>/dev/null 2>&1; then
    pct exec "$VMID" -- bash -s
  else
    ssh "root@${PROXMOX_NODE}" "pct exec ${VMID} -- bash -s"
  fi
}

if [[ ! -d "$STAGE" ]]; then
  echo "Missing $STAGE" >&2
  exit 1
fi

if ! run_pct status "$VMID" &>/dev/null; then
  if [[ "$CREATE" != "1" ]]; then
    echo "Guest $VMID missing; set ARA_SYS_CREATE=1" >&2
    exit 1
  fi
  echo "[infra-agent] Creating guest $VMID on ${PROXMOX_NODE} (2G RAM, 2 cores, 16G rootfs)"
  NS_ARGS=(--nameserver "$ARA_SYS_DNS")
  [[ -n "$ARA_SYS_DNS2" ]] && NS_ARGS+=(--nameserver "$ARA_SYS_DNS2")
  if command -v pct >/dev/null 2>&1; then
  if ! pveam list local 2>/dev/null | grep -q debian-12-standard; then
    pveam download local debian-12-standard_12.12-1_amd64.tar.zst 2>/dev/null || true
  fi
  pct create "$VMID" "$TEMPLATE" \
    --hostname "$HOSTNAME" \
    --memory 2048 \
    --cores 2 \
    --swap 512 \
    --rootfs "$ROOTFS" \
    --net0 "name=eth0,bridge=vmbr0,ip=${ARA_SYS_IP},gw=${ARA_SYS_GW}" \
    "${NS_ARGS[@]}" \
    --searchdomain "$ARA_SYS_SEARCHDOMAIN" \
    --unprivileged 0 \
    --onboot 1 \
    --features nesting=1,keyctl=1
  else
    ssh "root@${PROXMOX_NODE}" bash -s -- "$VMID" "$TEMPLATE" "$ROOTFS" "$HOSTNAME" "$ARA_SYS_IP" "$ARA_SYS_GW" "$ARA_SYS_DNS" "$ARA_SYS_SEARCHDOMAIN" <<'CREATE'
set -euo pipefail
VMID="$1"
TEMPLATE="$2"
ROOTFS="$3"
HOSTNAME="$4"
ARA_SYS_IP="$5"
ARA_SYS_GW="$6"
ARA_SYS_DNS="$7"
ARA_SYS_SEARCHDOMAIN="$8"
pveam download local debian-12-standard_12.12-1_amd64.tar.zst 2>/dev/null || true
pct create "$VMID" "$TEMPLATE" \
  --hostname "$HOSTNAME" \
  --memory 2048 \
  --cores 2 \
  --swap 512 \
  --rootfs "$ROOTFS" \
  --net0 "name=eth0,bridge=vmbr0,ip=${ARA_SYS_IP},gw=${ARA_SYS_GW}" \
  --nameserver "$ARA_SYS_DNS" \
  --searchdomain "$ARA_SYS_SEARCHDOMAIN" \
  --unprivileged 0 \
  --onboot 1 \
  --features nesting=1,keyctl=1
CREATE
  fi
fi

run_pct start "$VMID" 2>/dev/null || true
for _ in $(seq 1 30); do
  run_pct exec "$VMID" -- true 2>/dev/null && break
  sleep 2
done

echo "[infra-agent] Install base packages + Python venv"
remote_bash <<REMOTE
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
cat > /etc/resolv.conf <<RESOLV
nameserver ${ARA_SYS_DNS}
$( [[ -n "${ARA_SYS_DNS2}" ]] && echo "nameserver ${ARA_SYS_DNS2}" )
search ${ARA_SYS_SEARCHDOMAIN}
RESOLV
apt-get update -qq
  apt-get install -y -qq \
  python3 python3-pip python3-venv \
  ansible curl ca-certificates openssh-client \
  procps systemd
mkdir -p /opt/ara-sys-agent/data /opt/ara-sys-agent/ansible /opt/ara-sys-agent/secrets
REMOTE

echo "[infra-agent] Push application"
push_file "$STAGE/sys_agent.py" /opt/ara-sys-agent/sys_agent.py
push_file "$STAGE/fleet_manager.py" /opt/ara-sys-agent/fleet_manager.py
push_file "$STAGE/infra_bridge.py" /opt/ara-sys-agent/infra_bridge.py
push_file "$STAGE/fleet.config.json" /opt/ara-sys-agent/fleet.config.json
push_file "$STAGE/requirements.txt" /opt/ara-sys-agent/requirements.txt
push_file "$STAGE/config.env.example" /opt/ara-sys-agent/config.env.example
push_file "$STAGE/ara-sys-agent.service" /etc/systemd/system/ara-sys-agent.service
push_file "$STAGE/ansible/hosts" /opt/ara-sys-agent/ansible/hosts
if [[ -f /root/data/guest-map.json ]]; then
  push_file /root/data/guest-map.json /opt/ara-sys-agent/data/guest-map.json
fi
if [[ -f /root/array-agent-tokens.json ]]; then
  push_file /root/array-agent-tokens.json /opt/ara-sys-agent/secrets/array-agent-tokens.json
  run_pct exec "$VMID" -- chmod 600 /opt/ara-sys-agent/secrets/array-agent-tokens.json
fi

echo "[infra-agent] SSH keys for Proxmox node access"
run_pct exec "$VMID" -- mkdir -p /root/.ssh
run_pct exec "$VMID" -- chmod 700 /root/.ssh
for key in /root/.ssh/id_ed25519 /root/.ssh/id_rsa; do
  [[ -f "$key" ]] || continue
  push_file "$key" "/root/.ssh/$(basename "$key")"
  run_pct exec "$VMID" -- chmod 600 "/root/.ssh/$(basename "$key")"
done
if [[ -f /root/.ssh/known_hosts ]]; then
  push_file /root/.ssh/known_hosts /root/.ssh/known_hosts
fi

remote_bash <<'REMOTE'
set -euo pipefail
mkdir -p /etc/default
if [[ ! -f /etc/default/ara-sys-agent ]]; then
  cp /opt/ara-sys-agent/config.env.example /etc/default/ara-sys-agent
fi
# Merge new env keys without overwriting operator overrides
grep -q '^ARA_SYS_MCP_AGENT_ID=' /etc/default/ara-sys-agent 2>/dev/null || \
  echo 'ARA_SYS_MCP_AGENT_ID=node9-sre' >> /etc/default/ara-sys-agent
grep -q '^ARA_AGENT_TOKEN_FILE=' /etc/default/ara-sys-agent 2>/dev/null || \
  echo 'ARA_AGENT_TOKEN_FILE=/opt/ara-sys-agent/secrets/array-agent-tokens.json' >> /etc/default/ara-sys-agent
cd /opt/ara-sys-agent
python3 -m venv venv
venv/bin/pip install --upgrade pip wheel
venv/bin/pip install -r requirements.txt
chmod +x /opt/ara-sys-agent/sys_agent.py
systemctl daemon-reload
systemctl enable ara-sys-agent.service
systemctl restart ara-sys-agent.service
sleep 3
systemctl is-active ara-sys-agent.service
REMOTE

IP="$(run_pct exec "$VMID" -- hostname -I 2>/dev/null | awk '{print $1}')"
echo ""
echo "[infra-agent] Deployed guest ${VMID} (${HOSTNAME}) — Infrastructure Agent and Management"
echo "  IP:        ${IP:-dhcp}"
echo "  UI:        http://${IP:-<container-ip>}:${PORT}/"
echo "  CLI:       pct exec ${VMID} -- /opt/ara-sys-agent/venv/bin/python /opt/ara-sys-agent/sys_agent.py maintain"
echo "  Ollama:    \$(grep ARA_SYS_OLLAMA /etc/default/ara-sys-agent 2>/dev/null || echo 'see /etc/default/ara-sys-agent')"
echo ""
echo "Logs: pct exec ${VMID} -- journalctl -u ara-sys-agent -f"
