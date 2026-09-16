import { useState } from 'react'
import { api } from '../api.js'
import { Field, Text, Num, Select, Alert } from '../components/Field.jsx'
import { Badge } from '../components/CheckTable.jsx'
import Footer from '../components/Footer.jsx'
import JobLog from '../components/JobLog.jsx'
import { useFetch, jobRunner } from '../components/ops.js'

export default function Storage(p) {
  const { spec, update } = p
  const sto = spec.day2.storage
  const { data: st, err, reload, setErr } = useFetch(p.name, '/storage')
  const [job, setJob] = useState(null)
  const run = jobRunner(p.name, setJob, setErr, () => p.dirty ? p.save() : true)
  const setN = (k, v) => update(s => s.day2.storage.nfs[k] = v)
  const setR = (k, v) => update(s => s.day2.storage.registry[k] = v)
  const makeDefault = async (name) => { setErr(''); try { await api.post(`/api/clusters/${p.name}/storage/default-sc`, { name }); reload() } catch (e) { setErr(e.message) } }
  const done = () => { reload(); p.reload() }
  const reg = st?.registry
  const scNames = (st?.storage_classes || []).map(s => s.name)
  return (
    <div>
      <div className="panel">
        <div className="row"><h2 style={{ margin: 0 }}>Storage</h2><span className="spacer" /><button onClick={reload}>Refresh</button></div>
        <p className="lead">On IPI the vSphere CSI driver provides the <span className="mono">thin-csi</span> class out of the box. Add an NFS class for shared (RWX) volumes, LVM Storage for single-node and compact clusters, or ODF for a full Ceph stack on a storage pool.</p>
        <Alert kind="error">{err}</Alert>
        <h3>Storage classes</h3>
        {st && (st.storage_classes.length === 0 ? <p className="muted">No storage classes yet.</p> : (
          <table className="tbl"><thead><tr><th>Name</th><th>Provisioner</th><th>Binding</th><th>Default</th><th></th></tr></thead>
            <tbody>{st.storage_classes.map(s => <tr key={s.name}><td className="mono">{s.name}</td><td className="help">{s.provisioner}</td><td className="help">{s.binding}</td><td>{s.default ? <Badge s="pass" /> : ''}</td><td>{!s.default && <button className="small" onClick={() => makeDefault(s.name)}>Make default</button>}</td></tr>)}</tbody></table>
        ))}
        {st && <p className="help" style={{ marginTop: 8 }}>{st.pv_count} persistent volume(s) in the cluster.</p>}
      </div>

      <div className="panel">
        <h2>Image registry</h2>
        <p className="lead">The internal registry starts in state <b>Removed</b> on vSphere because no shared storage exists. Give it a volume so builds and image streams work.</p>
        {reg && <p className="help">Current: management <b>{reg.management}</b>{Object.keys(reg.storage || {}).length ? `, storage ${Object.keys(reg.storage).join('/')}` : ''}{reg.replicas ? `, ${reg.replicas} replica(s)` : ''} · operator {reg.ready?.available === 'True' ? <Badge s="pass" /> : <Badge s="warn" />}</p>}
        <div className="grid4">
          <Field label="Mode"><Select value={sto.registry.mode} onChange={v => setR('mode', v)} options={[{ value: 'pvc', label: 'Persistent volume (recommended)' }, { value: 'emptydir', label: 'emptyDir (lost on restart, testing only)' }, { value: 'removed', label: 'Removed (disable)' }]} /></Field>
          {sto.registry.mode === 'pvc' && <>
            <Field label="Storage class" help="RWO classes need 1 replica; NFS/CephFS (RWX) allow more."><Select value={sto.registry.storage_class} onChange={v => setR('storage_class', v)} placeholder="cluster default" options={scNames} /></Field>
            <Field label="Size GB"><Num value={sto.registry.size_gb} onChange={v => setR('size_gb', v)} /></Field>
            <Field label="Replicas"><Num value={sto.registry.replicas} onChange={v => setR('replicas', v)} /></Field></>}
        </div>
        <div className="toolbar"><span className="spacer" /><button className="primary" onClick={() => run('/storage/registry', {}, `Configure the image registry (${sto.registry.mode})?`)}>Configure registry</button></div>
      </div>

      <div className="panel">
        <h2>NFS storage class</h2>
        <p className="lead">Deploys the upstream nfs-subdir-external-provisioner against an export you already have (NAS, Linux server). Gives ReadWriteMany volumes; ideal for the registry and shared app data.</p>
        <div className="grid4">
          <Field label="NFS server"><Text value={sto.nfs.server} onChange={v => setN('server', v)} placeholder="192.168.1.20" /></Field>
          <Field label="Export path"><Text value={sto.nfs.path} onChange={v => setN('path', v)} placeholder="/volume1/openshift" /></Field>
          <Field label="Storage class name"><Text value={sto.nfs.sc_name} onChange={v => setN('sc_name', v)} /></Field>
          <Field label=" "><label className="row" style={{ marginTop: 8 }}><input type="checkbox" checked={!!sto.nfs.make_default} onChange={e => setN('make_default', e.target.checked)} /> make it the default class</label></Field>
        </div>
        <div className="toolbar">{st?.nfs_deployed && <span className="help"><Badge s="pass" /> provisioner deployed</span>}<span className="spacer" /><button className="primary" onClick={() => run('/storage/nfs', {}, `Deploy the NFS provisioner for ${sto.nfs.server}:${sto.nfs.path}?`)} disabled={!sto.nfs.server || !sto.nfs.path}>Deploy NFS provisioner</button></div>
      </div>

      <div className="panel">
        <h2>LVM Storage <span className="help">— single node / compact</span></h2>
        <p className="lead">Installs the LVM Storage operator and creates an LVMCluster that turns every unused disk on the nodes into a thin-provisioned volume group (storage class <span className="mono">lvms-vg1</span>). Attach an extra disk to the VMs first (a storage pool with data disks, or by hand in vSphere).</p>
        <div className="toolbar">{st?.operators?.['lvms-operator']?.installed && <span className="help"><Badge s="pass" /> operator installed{st.lvm_clusters.length ? ` · LVMCluster ${st.lvm_clusters.map(l => l.join(': ')).join(', ')}` : ''}</span>}<span className="spacer" /><button className="primary" onClick={() => run('/storage/lvms', {}, 'Install LVM Storage and claim all unused disks on the nodes?')}>Install LVM Storage</button></div>
      </div>

      <div className="panel">
        <h2>OpenShift Data Foundation <span className="help">— internal mode on local disks</span></h2>
        <p className="lead">Installs Local Storage and ODF, discovers spare disks on the nodes labelled <span className="mono">cluster.ocs.openshift.io/openshift-storage</span> (a storage pool sets this label; otherwise the workers get it), and creates the StorageCluster. Needs 3 nodes with at least one spare disk each and roughly 16 vCPU / 64 GB per node.</p>
        <div className="grid3">
          <Field label="Minimum disk size GB"><Num value={sto.odf_min_disk_gb} onChange={v => update(s => s.day2.storage.odf_min_disk_gb = v)} /></Field>
          <Field label="Maximum disk size GB"><Num value={sto.odf_max_disk_gb} onChange={v => update(s => s.day2.storage.odf_max_disk_gb = v)} /></Field>
          <Field label="Storage nodes"><Text value={(st?.storage_nodes || []).join(', ') || 'none labelled yet'} onChange={() => {}} disabled /></Field>
        </div>
        <div className="toolbar">{st?.operators?.['odf-operator']?.installed && <span className="help"><Badge s="pass" /> operator installed{st.odf_clusters.length ? ` · StorageCluster ${st.odf_clusters.map(l => l.join(': ')).join(', ')}` : ''}</span>}<span className="spacer" /><button className="primary" onClick={() => run('/storage/odf', {}, 'Install ODF in internal mode? This takes 20-40 minutes and consumes the spare disks on the storage nodes.')}>Install ODF</button></div>
      </div>
      {job && <div className="panel"><JobLog cluster={p.name} jobId={job} onDone={done} /></div>}
      <Footer {...p} next={null} />
    </div>
  )
}
