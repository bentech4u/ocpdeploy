#!/usr/bin/env bash
# Install ocpdeploy on a RHEL-family 9 host (RHEL, Rocky, Alma). Run as root.
#   git clone <repo> /opt/ocpdeploy && /opt/ocpdeploy/install.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
PORT="${OCPDEPLOY_PORT:-8080}"

echo "== packages"
dnf -y -q install python3.12 python3.12-pip git tar
if ! command -v node >/dev/null || [[ "$(node --version)" != v2[2-9]* ]]; then
  dnf -y -q module reset nodejs >/dev/null 2>&1 || true
  dnf -y -q module enable nodejs:22
  dnf -y -q install nodejs npm
fi

echo "== python venv"
[ -d "$ROOT/.venv" ] || python3.12 -m venv "$ROOT/.venv"
"$ROOT/.venv/bin/pip" install -q --upgrade pip
"$ROOT/.venv/bin/pip" install -q -r "$ROOT/backend/requirements.txt"

echo "== frontend build"
(cd "$ROOT/ui" && npm install --no-audit --no-fund --silent && npm run build --silent)

echo "== ssh key for the installer host (used for nodes and HAProxy VMs)"
[ -f /root/.ssh/id_ed25519 ] || ssh-keygen -t ed25519 -N "" -f /root/.ssh/id_ed25519 -C "root@$(hostname -f)" >/dev/null

echo "== command line"
ln -sf "$ROOT/ocpdeployctl" /usr/local/bin/ocpdeployctl

echo "== systemd service on port $PORT"
mkdir -p "$ROOT/clusters" "$ROOT/bin"
sed -e "s#/opt/ocpdeploy#$ROOT#g" -e "s#OCPDEPLOY_PORT=8080#OCPDEPLOY_PORT=$PORT#" "$ROOT/ocpdeploy.service" > /etc/systemd/system/ocpdeploy.service
systemctl daemon-reload
systemctl enable --now ocpdeploy
if systemctl is-active firewalld >/dev/null 2>&1; then
  firewall-cmd -q --permanent --add-port="$PORT"/tcp && firewall-cmd -q --reload
fi
sleep 2
systemctl --no-pager --lines=0 status ocpdeploy | head -3
echo
echo "ocpdeploy is running: http://$(hostname -f):$PORT/"
if [ -z "$(OCPDEPLOY_ROOT="$ROOT" "$ROOT/ocpdeployctl" user list 2>/dev/null)" ]; then
  echo "No console account yet. Create one now (recommended, so nobody else on the network can):"
  echo "  ocpdeployctl user set admin"
  echo "or open the web UI, which asks for it on the first visit."
fi
