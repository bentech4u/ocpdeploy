import { Field, Text, Num } from '../components/Field.jsx'
import Footer from '../components/Footer.jsx'

const list = (v) => (v || []).join(', ')
const parse = (s) => s.split(/[,\s]+/).map(x => x.trim()).filter(Boolean)

export default function Network(p) {
  const { spec, update } = p
  const n = spec.network
  const set = (k, v) => update(s => s.network[k] = v)
  return (
    <div>
      <div className="panel">
        <h2>Network</h2>
        <p className="lead">Addressing for the node network. Nodes get static IPs from the Nodes step; DHCP is not required.</p>
        <div className="grid3">
          <Field label="Machine network CIDR" help="Subnet the nodes and load balancers live in"><Text value={n.machine_cidr} onChange={v => set('machine_cidr', v)} placeholder="192.168.68.0/24" /></Field>
          <Field label="Gateway"><Text value={n.gateway} onChange={v => set('gateway', v)} placeholder="192.168.68.1" /></Field>
          <Field label="DNS servers" help="Comma separated; the first must know the cluster records"><Text value={list(n.dns_servers)} onChange={v => set('dns_servers', parse(v))} placeholder="192.168.68.122" /></Field>
          <Field label="NTP servers (optional)" help="Added as additional NTP sources for agent installs"><Text value={list(n.ntp_servers)} onChange={v => set('ntp_servers', parse(v))} /></Field>
          <Field label="Interface name inside RHCOS" help="vmxnet3 on a fresh VM is ens192. Used by the agent method for static IP config."><Text value={n.interface_name} onChange={v => set('interface_name', v)} /></Field>
        </div>
        <h3>Cluster internal networks</h3>
        <p className="help">Defaults are fine unless they overlap with your LAN. Pods use the cluster network, services the service network.</p>
        <div className="grid3">
          <Field label="Cluster network (pods)"><Text value={n.cluster_network} onChange={v => set('cluster_network', v)} /></Field>
          <Field label="Host prefix"><Num value={n.host_prefix} onChange={v => set('host_prefix', v)} /></Field>
          <Field label="Service network"><Text value={n.service_network} onChange={v => set('service_network', v)} /></Field>
        </div>
      </div>
      <Footer {...p} />
    </div>
  )
}
