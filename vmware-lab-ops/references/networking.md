# Lab networking & VLAN trunks

## Managed switch (example: TP-Link TL-SG108E / Easy Smart series)

The web GUI is reached on the switch's management IP (default often `192.168.0.1`) from a port in the
management VLAN. Configure, in order:

1. **802.1Q VLAN table** — create each lab VLAN (e.g. 10, 20, 30, 40).
2. **Tagged membership (trunks)** — on every inter-host/uplink port, tag all active lab VLANs so
   frames cross the link carrying their tag.
3. **Untagged membership (access ports)** — a VM host port that should see one untagged VLAN is an
   untagged member of just that VLAN.
4. **PVID** — set each access port's PVID to its VLAN. Rule: an untagged ingress frame is tagged with
   the port's PVID; an already-tagged frame is matched against the 802.1Q table and passed if the
   port is a member.
5. **Management port** — keep the port you use for the GUI untagged on the management VLAN with the
   matching PVID so you don't lock yourself out.

> Lock-out guard: change the port you're connected through **last**, and keep a second known-good
> port on the management VLAN while you work.

## Linux 802.1Q router (gateway VM) — Netplan

A single trunk NIC carries one subinterface per VLAN; each subinterface is that segment's gateway.

```yaml
# /etc/netplan/01-netcfg.yaml
network:
  version: 2
  renderer: networkd
  ethernets:
    ens33:  { dhcp4: false, addresses: [10.1.1.1/24] }   # WAN / uplink
    ens160: { dhcp4: false }                             # trunk (tagged), no IP of its own
  vlans:
    ens160.10: { id: 10, link: ens160, addresses: [172.16.1.1/28] }   # Users
    ens160.15: { id: 15, link: ens160, addresses: [172.16.1.17/28] }  # Admins
    ens160.20: { id: 20, link: ens160, addresses: [172.16.1.33/28] }  # Core infra
    ens160.30: { id: 30, link: ens160, addresses: [192.168.1.1/26] }  # DMZ
    ens160.40: { id: 40, link: ens160, addresses: [172.16.1.49/28] }  # Apps & storage
```

Apply safely over SSH (auto-reverts if you lose the link):

```bash
sudo netplan try          # applies, waits for Enter, reverts on timeout
sudo netplan apply        # commit once 'try' proved you kept access
```

Enable routing between segments:

```bash
echo 'net.ipv4.ip_forward=1' | sudo tee /etc/sysctl.d/99-router.conf
sudo sysctl --system
```

## systemd-networkd equivalent (no Netplan)

```ini
# /etc/systemd/network/10-trunk.netdev
[NetDev]
Name=ens160.10
Kind=vlan
[VLAN]
Id=10
```
```ini
# /etc/systemd/network/10-trunk.network   (bind the VLAN to the trunk)
[Match]
Name=ens160
[Network]
VLAN=ens160.10
```

## VMware side

Bridge the gateway VM's trunk NIC to the physical NIC wired to the switch trunk port (custom VMnet
bridged to that adapter — see vmx-reference.md). VMware passes 802.1Q tags through a bridged adapter,
so the guest does the VLAN tagging.
