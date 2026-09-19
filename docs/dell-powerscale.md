# Dell PowerScale (Isilon) CSI with ocpdeploy

This guide walks through installing Dell's PowerScale CSI driver on OpenShift with ocpdeploy, using
either the **Helm chart** or the **Dell CSM Operator**, and setting up **CSM Replication** (SyncIQ)
between a primary and a DR cluster, including failover and failback of a real application.

Everything below was run end to end on OpenShift 4.22 with two OneFS 9.15 simulators
(driver 2.17.1 with Helm, 2.17.2 with the CSM Operator 1.12.2, csm-replication 1.15.0).

- [1. What you need](#1-what-you-need)
- [2. Prepare the arrays](#2-prepare-the-arrays)
- [3. Offline bundle](#3-offline-bundle)
- [4. Install the driver](#4-install-the-driver)
- [5. Test a volume without replication](#5-test-a-volume-without-replication)
- [6. Set up replication](#6-set-up-replication)
- [7. Test a replicated volume](#7-test-a-replicated-volume)
- [8. Failover and failback](#8-failover-and-failback)
- [9. Remove everything](#9-remove-everything)
- [10. Troubleshooting](#10-troubleshooting)
- [11. How it works](#11-how-it-works)

## 1. What you need

| | Standalone | With replication |
|---|---|---|
| OpenShift clusters | 1 (installed or connected in ocpdeploy) | 2: primary and DR, **both visible in the same ocpdeploy** |
| PowerScale arrays | 1 or more | 1 per site (a single array with two paths works for tests) |
| OneFS licences | SmartQuotas (size limits), SnapshotIQ (snapshots) | plus SyncIQ |
| Network | nodes → array NFS (2049, 111); controller → array API (8080) | plus array ↔ array SyncIQ, both clusters → both arrays' API |

The page is under **Operate → Dell PowerScale** of every installed or connected cluster, with four
tabs: **Overview**, **Install / upgrade**, **Replication & DR** and **Offline bundle**.

Pick one ocpdeploy to drive replication, normally the installer that installed the primary cluster.
Connect the DR cluster to it (Clusters → Connect). A connected cluster lives in memory only, so connect
it again whenever you set up replication or run a DR action from that installer. If that installer is
down during a real disaster, open the **DR cluster's own** Replication tab on any ocpdeploy: an
unplanned failover only needs the DR cluster.

## 2. Prepare the arrays

On each array (OneFS shell as root), licences, services, the base path and an API user:

```bash
isi license add --evaluation SMARTQUOTAS --evaluation SNAPSHOTIQ --evaluation SYNCIQ   # or real licences
isi services nfs enable
isi sync settings modify --service on            # replication only
mkdir -p /ifs/data/csi && chmod 777 /ifs/data/csi
isi auth users create csiuser --enabled yes --password '<password>' --zone System --password-expires no
isi auth roles create CSIRole --zone System
isi auth roles modify CSIRole --zone System --add-user csiuser \
  --add-priv-read ISI_PRIV_LOGIN_PAPI --add-priv-read ISI_PRIV_IFS_RESTORE \
  --add-priv-read ISI_PRIV_NS_IFS_ACCESS --add-priv-read ISI_PRIV_IFS_BACKUP \
  --add-priv-read ISI_PRIV_AUTH --add-priv-read ISI_PRIV_AUTH_ZONES --add-priv-read ISI_PRIV_STATISTICS \
  --add-priv-write ISI_PRIV_NFS --add-priv-write ISI_PRIV_QUOTA --add-priv-write ISI_PRIV_SNAPSHOT \
  --add-priv-write ISI_PRIV_SYNCIQ
```

Notes:

* OneFS 9.15 refuses basic auth on the Platform API; the driver then needs session auth
  (`isiAuthType 1`). The pre-checks detect which one works.
* Without SmartConnect, NFS mounts use the endpoint IP. Enter it as the **NFS address (AzServiceIP)**.
* **SyncIQ with encryption:** each array needs its SyncIQ server certificate, and each array must trust
  the other's. Note each array's certificate ID (`isi sync certificates server list`); ocpdeploy asks for
  it as **SyncIQ certificate ID** (`replicationCertificateID` in the driver secret).
* Test SyncIQ between the arrays in both directions before you start (any small policy will do).

## 3. Offline bundle

**Offline bundle** tab (shared by all clusters on the installer, stored in `bundles/dell/`, mode 700).

| File | Needed for | Where to get it |
|---|---|---|
| `csi-isilon-<v>.tgz` | Helm method | `https://github.com/dell/helm-charts/releases/download/csi-isilon-<v>/csi-isilon-<v>.tgz` |
| `csm-replication-<v>.tgz` | replication with Helm (controller + CRDs) | `https://github.com/dell/helm-charts/releases/download/csm-replication-<v>/csm-replication-<v>.tgz` |
| `helm` | Helm method | `https://get.helm.sh/helm-<v>-linux-amd64.tar.gz` |
| `repctl` | optional, DR commands by hand | `https://github.com/dell/csm/releases/download/v<csm>/repctl` |

Three ways to add a file: **Download here** (installer with internet), **Upload file…** (from your
browser), or **Import from path** (a file or an unpacked chart directory already on the installer).
Charts are checked against the digest in Dell's chart index, helm against get.helm.sh's checksum and
repctl against the GitHub release digest; the "Published versions" list shows URLs and sha256 values for
downloading on another machine.

**Container images.** The cluster pulls the driver images itself. The *Container images* box lists them
for a chart version together with an `oc image mirror` loop. For a disconnected cluster, mirror them and
set **Image registry** on the Install tab (images are rewritten to `<registry>/<original path>`), or use
an ImageTagMirrorSet. The CSM Operator method uses the operator's images instead: mirror the
certified-operators package `dell-csm-operator-certified` and its images.

## 4. Install the driver

**Install / upgrade** tab.

1. **Method**
   * *Helm chart*: Dell's csi-isilon chart from the bundle.
   * *Dell CSM Operator*: ocpdeploy subscribes to `dell-csm-operator-certified` (namespace
     `dell-csm-operator`, channel stable, certified-operators catalog) and creates a
     `ContainerStorageModule` built from the operator's own example for its version.
   * An existing driver is detected and **adopted** in place (the form is prefilled from it). The method
     of a running driver cannot be switched; uninstall first. The operator cannot take over a Helm
     install, so this is enforced by the pre-checks.
2. **Namespace / release**: `isilon` by default; also the prefix of the secrets `isilon-creds` and
   `isilon-certs-0`.
3. **Arrays**: name (the `clusterName` StorageClasses use), endpoint, port, API user, **password**
   (sent with the request only; never stored by ocpdeploy; blank on an upgrade keeps the one already in
   the cluster), access zone, isiPath, NFS address, and with replication ticked the **SyncIQ certificate
   ID**. Tick *verify the array's TLS certificate* to put the array's CA into `isilon-certs-0`
   ("Use the certificate the array presents" fills it after a pre-check).
4. **Driver**: auth type, controller replicas, volume prefix, log level, image registry, quotas,
   snapshots, expansion, *also run on infra nodes*, and **replication** (adds the replicator sidecar;
   tick it on both clusters if you plan to replicate).
5. **Storage classes**: one or more NFS classes per array / zone / path / NFS address, reclaim policy,
   binding mode, root client, default class. A VolumeSnapshotClass (`isilon-snapclass`) is created too.
6. **Run pre-checks**. Failures block the install:

   | Area | Checks |
   |---|---|
   | Array | DNS, TCP to the API, NFS 2049/111, TLS, auth type, OneFS version, access zone contains isiPath, isiPath exists (**Create path** button), NFS service, licences, the API user's privileges against Dell's list (prints the `isi auth roles modify` fix) |
   | Nodes | TCP from a worker to each array's API and NFS ports (`oc debug`, skipped on read-only connections) |
   | Cluster | Dell's tested OpenShift range (newer is a warning), existing driver and method, snapshot CRDs, helm and chart in the bundle, operator in a catalog, image mirrors on disconnected clusters, StorageClasses that already exist with different (immutable) parameters |

   Expected warnings: OpenShift newer than Dell tested; a self-signed array certificate; SmartConnect
   pools not readable by the API user.
7. **Preview changes**: for Helm, the values and a manifest diff against the running release (identical
   settings show "No changes"); for the operator, the ContainerStorageModule that would be applied.
8. **Install driver** / **Upgrade / re-apply**. The job creates the namespace and secrets, installs the
   replication CRDs first when replication is ticked (Helm), runs `helm upgrade --install` or applies the
   ContainerStorageModule, waits for the controller and node pods, then creates the classes.
   With the operator, a short *"Failed install: CSM state is failed"* while pods start is normal; the job
   waits for *Succeeded*.

The **Overview** tab then shows the method, driver version, pods, arrays, classes and volume count.

## 5. Test a volume without replication

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata: {name: plain-pvc, namespace: ps-test}
spec: {accessModes: [ReadWriteMany], resources: {requests: {storage: 1Gi}}, storageClassName: isilon}
```

The PVC is Bound within seconds and a directory `csivol-…` with a quota appears under the isiPath.
Snapshots use `isilon-snapclass`; expansion works online.

## 6. Set up replication

Prerequisites: the driver runs on both clusters with **replication** ticked, each cluster knows its own
array, and both clusters are open in the same ocpdeploy.

**Primary cluster → Dell PowerScale → Replication & DR → Edit**:

| Field | Example |
|---|---|
| Peer cluster (DR site) | the DR cluster (the list reloads when opened; connect it first) |
| This cluster's ID / Peer cluster's ID | `homelab` / `drhomelab` (filled from the names) |
| Source array (here) / Target array (DR) | `ISIN-MAIN` / `ISIN-DR` (clusterNames) |
| RPO | `Five_Minutes` … `One_Day` |
| Source and target access zone / path | `System` / `/ifs/data/csi` |
| NFS addresses (source / target) | the arrays' NFS IPs or SmartConnect names |
| StorageClass here / on the peer | `isilon-replication` / `isilon-replication` |
| Volume group prefix | `csi` |
| ignoreNamespaces | off = one replication group per namespace |

Without a peer the page shows a warning: that sets up **single-cluster** replication (both copies used
from one cluster, target class `<name>-tgt`).

**Run pre-checks**: both drivers with the replicator sidecar, the replication controller (present or to
be installed), both arrays listed (or to be copied) in both drivers' secrets, TCP/auth/licences/
privileges and the **SyncIQ service** on both arrays, and the StorageClasses.

**Set up replication** does what `repctl cluster inject` and `repctl create sc` do:

1. copies each array's entry into the other cluster's driver secret (both clusters must know both
   arrays);
2. installs the replication controller where missing (Helm: the csm-replication chart; operator: the
   ContainerStorageModule's replication module);
3. on each cluster, stores a kubeconfig for the other one in `dell-replication-controller/<peer ID>`,
   built from the peer's own `dell-replication-controller-sa` token (never the admin login) and verified
   with `oc auth can-i list dellcsireplicationgroups` before use;
4. writes `dell-replication-controller-config` (`clusterId` + `targets`) on each cluster;
5. creates the replicated StorageClass pair.

The controllers pick the new config up within about a minute (kubelet ConfigMap refresh). The
settings are saved with the primary cluster in ocpdeploy.

## 7. Test a replicated volume

Create a PVC with `isilon-replication` in an application namespace on the primary (not in the driver's
own `isilon` namespace). Within the RPO:

* the **Replication groups** table shows the group on both clusters: *source* on the primary, *target*
  on the DR cluster, link **SYNCHRONIZED**;
* the primary array has a SyncIQ policy `<prefix>-<namespace>-<target endpoint>-<RPO>`;
* the DR cluster has a PV that is **Available** and whose claim shows `<namespace>/<pvc name>`. That is a
  reservation (the replication controller does not create the PVC on the target), not an existing PVC.

The link state appears after the first monitor cycle (up to a minute); until then the role column may
say *target* for both.

## 8. Failover and failback

The **Action…** menu on each group only offers what fits its role and link state:

| Action | Row | When | What it does |
|---|---|---|---|
| Sync now | source | replicating | runs the SyncIQ job now |
| Suspend / Resume | source | replicating / suspended | pauses or resumes the policy |
| Planned failover to the other site | source | replicating | final sync, stops writes at the source, makes the DR copy writable |
| Unplanned failover | target | source site lost | makes the DR copy writable at once; changes since the last sync are lost |
| Reprotect | target | after a failover | replicates from the DR site back to the original site (the DR site becomes the source) |
| Failback, bringing back the data written at the DR site | source | after a failover | mirror-syncs the DR changes back, makes the original site writable (driver action `FAILBACK_LOCAL`) |
| Failback, discarding the data written at the DR site | source | after a failover | makes the original site writable again and drops the DR changes (`ACTION_FAILBACK_DISCARD_CHANGES_LOCAL`) |

Failovers, reprotect and failbacks need the group name typed. The job sets `spec.action` on the right
group and follows the driver's result (`Action` annotation, `status.state`, `status.lastAction`).

### Planned failover of an application

On the primary:

1. **Sync now**.
2. Stop the application (keep its PVC): `oc scale deploy/<app> --replicas=0` (or `dc/<app>`).
3. **Planned failover to the other site**, then wait for *FAILOVER_REMOTE succeeded* and link
   **FAILEDOVER**.

On the DR cluster:

4. Create the **same project** as on the primary.
5. Create the claim with the **same name**, bound to the reserved PV (the PVC form has no PV field; use
   *Edit YAML*):

   ```yaml
   apiVersion: v1
   kind: PersistentVolumeClaim
   metadata: {name: <pvc name>, namespace: <project>}
   spec:
     accessModes: [ReadWriteMany]
     resources: {requests: {storage: <same size>}}
     storageClassName: isilon-replication
     volumeName: <the Available PV on the DR cluster>
   ```

   A PVC in another project cannot bind ("volume already has a claim"): the PV is reserved for
   `<project>/<pvc name>`. To use another project, patch the reservation first:
   `oc patch pv <pv> --type=merge -p '{"spec":{"claimRef":{"namespace":"<project>","name":"<pvc>"}}}'`.
6. Deploy the application there with the volume at the same mount path
   (*Actions → Add storage → Use existing claim*).

### Failback

1. Stop the application on the DR cluster; **keep its PVC** (deleting it can delete the DR copy).
2. On the primary's row: **Failback, bringing back the data written at the DR site**. It takes a few
   minutes (resync-prep, mirror sync, writes enabled on the original array).
3. Start the application on the primary again; the DR-written data is there. The group returns to
   source / target, **SYNCHRONIZED**. Keep the DR project and PVC for the next failover.

## 9. Remove everything

Order matters, so that SyncIQ policies and replication groups are removed by the driver instead of
being left on the arrays.

1. Stop the applications.
2. On the DR cluster, set the replicated PVs to **Retain**
   (`oc patch pv <pv> --type=merge -p '{"spec":{"persistentVolumeReclaimPolicy":"Retain"}}'`), then
   delete their PVCs; the PVs become Released without touching the array.
3. On the primary, delete the replicated PVCs; the driver deletes the source volumes.
4. Delete the replication groups, primary first (`oc delete rg <name>`), then on the DR cluster; the
   driver removes the SyncIQ policies and target directories. Then delete the retained DR PVs.
5. Delete the other PVCs and their VolumeSnapshots while the driver still runs.
6. Delete the replicated StorageClasses. With Helm, also remove the replication controller
   (`helm uninstall replication -n dell-replication-controller`), its namespace (it holds the peer
   kubeconfigs) and, once no groups are left, the two `*.replication.storage.dell.com` CRDs. With the
   operator, step 7 removes the controller and the CRDs; delete the `dell-replication-controller`
   namespace afterwards.
7. **Overview → Uninstall the driver…** on each cluster (type the namespace; tick *also delete the
   StorageClasses*). It is refused while PVs use the driver. With the operator and replication enabled,
   deleting the ContainerStorageModule also deletes the replication CRDs and every replication group,
   so ocpdeploy refuses it while groups exist.
8. Delete the `isilon` namespace (it holds the array passwords).
9. On the arrays, check that nothing is left: `ls /ifs/data/csi`, `isi snapshot snapshots list`,
   `isi nfs exports list`, `isi quota quotas list`, `isi sync policies list`, `isi sync target list`.

## 10. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Link state `UNKNOWN`, volumes do not delete, driver log says `Unauthorized to access PAPI` | The driver's OneFS session expired and was not renewed (seen after about 4 hours with session auth). The stored password still works (the pre-checks log in fine). Restart the controller: `oc rollout restart deploy/isilon-controller -n isilon`. |
| PV stuck in Released: *"is still attached to node"* | Same cause: the driver cannot remove the NFS export client. Restart the controller. |
| Setup log says `-> same cluster` | No peer was selected. The page now warns about it; select the connected DR cluster. Delete the `<name>` and `<name>-tgt` classes it created if unused. |
| Reprotect fails: *can't find target policy on the local site* | Reprotect runs on the DR site after a failover. Nothing changed on the arrays; the group's state shows `Error` until the next action. |
| Replication controller event *Config update won't be applied* | Normal right after the controller is installed (empty default config); the setup job writes the real config a moment later. |
| DR PVC does not bind | It must be in the same project and have the same name as on the primary (the PV is reserved for it), with the same size, access mode and class. |
| Namespace stuck in *Terminating* after uninstall | A VolumeSnapshot was left behind and the snapshotter is gone. Delete the snapshot on the array (`isi snapshot snapshots delete`), then remove the finalizers of the VolumeSnapshotContent and the VolumeSnapshot. |
| Leftover NFS export on the DR array | A retained DR volume was never deleted through the driver; remove the export (`isi nfs exports delete`). |
| Operator install briefly shows *Failed* | The operator reports Failed while pods roll out; the job waits for Succeeded. |

## 11. How it works

* Services: `backend/ocpdeploy/services/powerscale.py` (detection, pre-checks, Helm values,
  ContainerStorageModule, install and uninstall), `powerscale_repl.py` (replication without repctl, DR
  actions), `onefs.py` (OneFS Platform API checks), `dellbundle.py` (offline bundle). API under
  `/api/clusters/<name>/powerscale` and `/api/bundles/dell`.
* Helm values are the chart's defaults, then the running release's own values (so an adopted release
  keeps its settings), then the form; images and `version` always follow the target chart.
* Array passwords are never written to `cluster.json`; the driver secret is rebuilt from the form plus
  the existing secret, keeping keys the form does not know.
* Replication peers get a kubeconfig from the other cluster's `dell-replication-controller-sa` token; the
  temporary copy used to verify it lives in tmpfs and is removed afterwards.
* PowerScale supports these replication actions (csi-powerscale `service/replication.go`):
  FAILOVER_REMOTE, UNPLANNED_FAILOVER_LOCAL, REPROTECT_LOCAL, FAILBACK_LOCAL,
  ACTION_FAILBACK_DISCARD_CHANGES_LOCAL, SUSPEND, RESUME and SYNC.

References: [csi-powerscale](https://github.com/dell/csi-powerscale) ·
[helm-charts](https://github.com/dell/helm-charts) ·
[csm-replication](https://github.com/dell/csm-replication) ·
[csm-operator](https://github.com/dell/csm-operator) ·
[CSM documentation](https://dell.github.io/csm-docs/) ·
[support matrix](https://dell.github.io/csm-docs/docs/getting-started/supportmatrix/)
