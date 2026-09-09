# ocpdeploy

A small web console that installs and operates **OpenShift 4 clusters on VMware vSphere** from a
single Linux "installer" host. It wraps `openshift-install`, talks to vCenter directly, manages
HAProxy load balancers over SSH, validates DNS, and streams every job log live in the browser.

Built for home labs and small environments where you have vCenter, your own DNS server and one or
two Linux VMs for HAProxy, and you want repeatable installs without hand-editing YAML.

## Features

* **Wizard per cluster** – cluster & version → vCenter → network → nodes → load balancer → secrets →
  DNS → pre-flight → review & deploy → operate.
* **Two install methods**
  * *IPI* (installer-provisioned): the installer creates and destroys the VMs; static IPs; your own
    load balancer (`loadBalancer: UserManaged`). Needs OpenShift 4.15+.
  * *Agent-based (UPI)*: the app builds the agent ISO, uploads it to a datastore and creates the VMs
    itself (platform `none`).
* **Version list from Red Hat's update graph** (stable / fast / candidate), tools downloaded and
  checksum-verified per version.
* **vCenter integration** – certificate captured and accepted by fingerprint (VMCA root pulled from
  the vCenter CA bundle), inventory-driven dropdowns, privilege check, host clock / NTP check.
* **Load balancer**
  * *HAProxy managed by the app*: SSH with password or key to one or two EL9 VMs; installs HAProxy,
    writes `/etc/haproxy/conf.d/ocp-<cluster>.cfg`, validates and reloads. Role-split or keepalived
    HA pair. Unrelated sections in `haproxy.cfg` are preserved.
  * *External*: paste IP + FQDN; the app validates DNS/ports and prints the required backend pools.
* **DNS is validate-only** – a checklist of every required record with hints, checked against your
  DNS server, including stale reverse records.
* **Pre-flight** – host, secrets, node sanity, DNS, LB ports, vCenter objects, capacity, privileges.
* **Live deploy log**, installer runs as a transient systemd unit and survives app restarts.
* **Day 2** – credentials & ingress CA download, node/operator dashboard, remove bootstrap from the
  LB, add infra nodes (static IPs), move ingress / monitoring / registry to infra, resume an
  interrupted install, destroy.
* Secrets (vCenter password, SSH passwords, pull secret) are encrypted at rest.

## Requirements

* Installer host: RHEL / Rocky / Alma **9**, root access, outbound HTTPS to
  `mirror.openshift.com`, `quay.io`, `registry.redhat.io`, `api.openshift.com`, `pypi.org`,
  `registry.npmjs.org`. 4 vCPU / 8 GB / 50 GB is plenty.
* vCenter 7 or 8 with the ESXi host(s) **inside a cluster object** (IPI refuses standalone hosts),
  a datastore, a port group, and an account with the privileges the installer documents.
* A DNS server you control for `api`, `api-int`, `*.apps` and (recommended) the node names.
* One or two EL9 VMs for HAProxy, or an external load balancer.
* A Red Hat pull secret (console.redhat.com/openshift/install/pull-secret).
* NTP on the ESXi hosts. VMs take the host clock at boot.

## Install

```bash
git clone https://github.com/bentech4u/ocpdeploy.git /opt/ocpdeploy
/opt/ocpdeploy/install.sh
```

Then open `http://<installer-host>:8080/`. Set `OCPDEPLOY_PORT` before running the script for a
different port.

The script installs Python 3.12 and Node 22 from AppStream, creates a venv, builds the frontend,
generates an SSH key for root if none exists, and enables the `ocpdeploy` systemd service.

> **Security:** there is no authentication. Run it on a trusted LAN or put it behind a reverse proxy
> with auth. Anyone who can reach the port can deploy and destroy clusters.

## Layout

```
backend/ocpdeploy/     FastAPI app (routers/, services/, templates/)
ui/                    React + Vite frontend, built into backend/ocpdeploy/static
clusters/<name>/       per-cluster state (git-ignored)
  cluster.json         spec, secrets encrypted with .secret_key
  state.sqlite         jobs, logs, check results
  install/             openshift-install working dir (auth/kubeconfig, auth/kubeadmin-password)
  logs/                installer log copies, job outputs
bin/<version>/         openshift-install, oc (git-ignored)
install.sh             installer for a new host
ocpdeploy.service      systemd unit
```

## Operating

```bash
systemctl status ocpdeploy
systemctl restart ocpdeploy        # safe during a deploy; the job is re-attached
journalctl -u ocpdeploy -f
```

Moving to another host: copy the whole directory including `clusters/` and `.secret_key`, then run
`install.sh` there.

## Development

```bash
PYTHONPATH=backend .venv/bin/uvicorn ocpdeploy.main:app --reload --port 8080
cd ui && npm run dev           # dev server proxies /api to :8080
cd ui && npm run build         # rebuild the static bundle
```

## Notes learned the hard way

* `openshift-install` validates the vCenter certificate against the **installer host's trust
  store**, not `additionalTrustBundle`. The app passes a combined bundle via `SSL_CERT_FILE`.
* IPI needs `/DC/host/<cluster>`; a standalone ESXi host path fails with "cluster not found".
* Slow image pulls on the bootstrap node can exhaust the installer's 15-minute provisioning window.
  Nothing is lost: use *Resume interrupted install* on the Operate page.
* Browser warnings on the console are expected; import the ingress CA from the Operate page.
