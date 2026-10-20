# Lab blueprint reference

A blueprint is a JSON file (YAML also works if PyYAML is installed). `lab plan FILE` shows what it
would do; `lab apply FILE --yes` builds it; re-applying after edits only adds what's missing.
Files are looked up as given, then in `$PVE_HOME/labs/`, then in the skill's `examples/`.
Unknown keys are ignored, so `"_comment"` fields are a fine way to document a blueprint.

## Top level

| Key | Default | Meaning |
|---|---|---|
| `lab` | required | Name `[a-z0-9-]`, max 24 chars. Creates pool + tag `lab-<lab>` and `labs/<lab>.state.json`. |
| `node` | `auto` | Node for the lab. `auto` = the node holding the first needed VM template, else the node with most free RAM. Kept stable across re-applies. |
| `vmid_start` | next free | First VMID to try. |
| `defaults` | see below | Credentials and per-type defaults. |
| `networks` | LAN bridge, DHCP | Named networks; the **first** one is the default for machines without `nets`. |
| `network` | - | Shorthand for a single network named `default`. |
| `machines` | required | List of machines (below). |

### `defaults`

```json
{
  "user": "lab",            // cloud-init user for VMs (CTs always use root)
  "password": "auto",       // "auto" = generated once, then kept in the state file
  "ssh_key": "auto",        // "auto" = $PVE_HOME/keys/lab_ed25519(.pub), generated on first use;
                            // or a public key string / path to a .pub file / null
  "nameserver": "1.1.1.1",  // optional DNS for every guest
  "start": true,            // start machines after creation
  "onboot": null,           // true = start with the host
  "vm": {"template": "ubuntu-24.04", "cores": 2, "memory": "2G", "disk": "20G"},
  "ct": {"template": "debian-12", "cores": 1, "memory": "512M", "swap": "512M", "disk": "8G",
         "nesting": true, "unprivileged": true}
}
```

Sizes accept `512M`, `4G`, `1T`, or bare numbers (memory: MB, or GB if < 64; disk: GB).

## Networks

**Existing bridge, DHCP** (the user's LAN):
```json
"home": {"bridge": "auto", "dhcp": true}
```
`auto` = the node's management bridge (the one with the default gateway), or `$PVE_BRIDGE`.

**Existing bridge or VLAN, static addressing** (`range` = addresses pve.py may hand out):
```json
"servers": {"bridge": "vmbr0", "vlan": 30, "cidr": "10.30.0.0/24", "gateway": "10.30.0.1",
            "range": "10.30.0.100-10.30.0.150"}
```

**Isolated lab network** (new SDN vnet in zone `labs` / `$PVE_LAB_ZONE`; needs PVE 8.1+):
```json
"lan": {"isolated": true, "cidr": "auto", "nat": true, "start": 10}
```
* `cidr: auto` takes the next free /24 from `$PVE_LAB_SUBNETS` (default `10.66.0.0/16`).
* The host gets `.1` as gateway; machines get static IPs from `.start` (default 10) upward.
* `nat: true` = outbound internet via the node (SNAT). `nat: false` = fully air-gapped.
* Simple SDN zones are node-local: all machines on an isolated network must be on the lab's node.
* Reach it from the LAN through a dual-homed jump host (see examples), e.g.
  `ssh -i KEY -o ProxyCommand="ssh -i KEY -W %h:%p root@JUMP" lab@10.66.0.11`.

## Machines

| Key | Applies to | Meaning |
|---|---|---|
| `name` | all | Hostname. With `count` > 1: `web` -> `web1`, `web2`; or use `{n}`: `"node-{n}"`. |
| `type` | all | `vm` (default) or `ct`. |
| `count` | all | Number of copies. Explicit IPs are incremented per copy. |
| `template` | vm, ct | VM: template name/VMID or buildable distro (`ubuntu-24.04`, `ubuntu-26.04`, `ubuntu-22.04`, `debian-13`, `debian-12`, `rocky-9`, `alma-9`). CT: template name prefix (`debian-12`, `ubuntu-24.04`, `alpine`, ...) or a full volid. |
| `iso` | vm | Installer ISO instead of a template (part of the filename or a volid). No cloud-init: the planned IP is only a suggestion for the installer. |
| `virtio_iso`, `ostype`, `efi`, `tpm` | vm + iso | Windows: `ostype` `win11` (Win11, Server 2022/2025) or `win10` (Win10, Server 2016/2019) adds EFI + TPM; `virtio_iso` attaches drivers so disks/NICs use virtio. Others: `l26`, `other`. |
| `cores`, `memory`, `disk` | all | Override defaults. Template disks are grown to `disk`. |
| `swap`, `nesting`, `unprivileged` | ct | Container options (`nesting` lets Docker run inside). |
| `nets` | all | List of network names or NIC objects (below). Default: first network. |
| `ip` | all | Shortcut for the first NIC's `ip`. |
| `node` | all | Put this machine on another node (not allowed with isolated networks). |
| `vmid` | all | Fixed VMID. |
| `tags` | all | Extra tags (string `"a;b"` or list). |
| `start`, `onboot` | all | Per-machine overrides. |

NIC object: `{"net": "lan", "ip": "auto" | "dhcp" | "10.66.0.50" | "10.66.0.50/24", "gw": "...",
"vlan": 20, "firewall": true, "model": "e1000e"}`. The first NIC with a static IP on a network that
has a gateway (bridge network with `gateway`, or isolated with `nat`) gets the default route; a DHCP
NIC brings its own.

## State and lifecycle

* `labs/<lab>.state.json` records VMIDs, nodes, NICs/IPs, the network vnets and the credentials.
  Keep it private (it contains the lab password).
* `lab status LAB` - live table with IPs; `lab start|stop|shutdown LAB`.
* Add machines: edit the blueprint, `lab apply` again. Remove one: `delete NAME --yes`.
* `lab destroy LAB --yes` deletes every guest in the pool, the lab's vnets and the pool, and renames
  the state file to `*.destroyed-<timestamp>.json`.
* Interrupted builds (exit 3, crash, closed laptop) resume where they stopped on the next apply.
