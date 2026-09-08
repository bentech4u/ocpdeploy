# ocpdeploy

Web console that deploys OpenShift clusters on vSphere from this installer host.

* Backend: FastAPI (`backend/ocpdeploy`), Python 3.12 venv in `.venv`
* Frontend: React + Vite (`ui/`), built into `backend/ocpdeploy/static`
* State: one folder per cluster under `clusters/<name>/`
  * `cluster.json` – the spec (secrets encrypted with the key in `.secret_key`)
  * `state.sqlite` – jobs, logs, check results
  * `install/` – openshift-install working dir (kubeconfig in `install/auth/`)
  * `logs/` – copies of installer logs
* Tools: `bin/<version>/openshift-install|oc`, downloaded and checksum-verified from mirror.openshift.com

## Run

```bash
systemctl enable --now ocpdeploy
```

Open http://installer.bentech.work:8080/. No authentication (LAN only).

## Develop

```bash
# backend with auto-reload
PYTHONPATH=backend .venv/bin/uvicorn ocpdeploy.main:app --reload --port 8080
# frontend dev server (proxies /api to :8080)
cd ui && npm run dev
# rebuild the static bundle
cd ui && npm run build
```

## Flow

1. Cluster & version – name, base domain, IPI or agent-based, version from the Red Hat update graph
2. vCenter – credentials, certificate acceptance, inventory-driven dropdowns, privilege check
3. Load balancer – HAProxy VMs managed over SSH (split or keepalived HA pair) or an external LB
4. Network – machine CIDR, gateway, DNS, NTP
5. Nodes – generated node table with static IPs and MACs
6. Secrets – pull secret, SSH key
7. DNS records – required records with hints, validated against your DNS server
8. Pre-flight – everything checked in one run
9. Review & deploy – masked install-config preview, live installer log
10. Operate – status, credentials, remove bootstrap from LB, add infra nodes, move ingress to infra, destroy
