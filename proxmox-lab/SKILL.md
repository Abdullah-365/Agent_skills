---
name: proxmox-lab
description: Manage a Proxmox VE server or cluster from plain-language requests - create, configure, start/stop, snapshot, migrate and delete VMs and LXC containers, build cloud-init templates, edit host bridges/VLANs and SDN, and build or tear down whole multi-machine labs (Kubernetes, web stacks, AD/cyber ranges) from one prompt. Use whenever the user mentions Proxmox/PVE or asks to spin up VMs, containers or labs on their hypervisor.
---

# Proxmox lab control

This skill ships **`scripts/pve.py`**: a tested, standard-library-only Python 3.8+ CLI that talks to the
Proxmox VE REST API with an API token. You turn the user's request into `pve.py` commands (or a lab
blueprint), run them, and report results. Never print or echo the token secret.

## 1. Where to run pve.py

The script must run on a machine that can reach `https://<proxmox-host>:8006`.

| Environment | How |
|---|---|
| **Claude Code / any local agent** on a machine in the same network | Run it in place: `python3 "${CLAUDE_SKILL_DIR}/scripts/pve.py" ...` (Claude Code), or the `scripts/pve.py` path next to this file in other agents |
| **Claude Cowork / desktop app with a linked computer** | Your sandbox usually can't reach the user's LAN, but the linked computer can. Copy `scripts/pve.py` into a folder the user connected (e.g. `<folder>/proxmox-lab/pve.py`) and run it with the device shell: `cd "$HOME/mnt/<folder>/proxmox-lab" && PVE_HOME=. PVE_BUDGET=150 python3 pve.py ...`. Network egress must allow LAN hosts (Settings -> Capabilities -> **All domains**). Background processes don't survive between calls - rely on `PVE_BUDGET` + re-runs instead. |
| **Hosted sandbox with no route to the server** | Don't expose Proxmox to the internet to make it work. Give the user the exact commands to run themselves, or suggest a local agent. |

`python` may be `python3`, `python` or `py -3` (Windows). Data (lab blueprints, state with IPs and
logins, the lab SSH key) lives in `$PVE_HOME` (default `~/.proxmox-lab`, or next to the script if a
`labs/` folder or `pve.env` sits there). In ephemeral sandboxes, point `PVE_HOME` at a persistent folder.

## 2. First-time setup (once per user)

1. Run `pve.py doctor`. If it finds a working config, skip to step 4.
2. Ask for the Proxmox address (any cluster node). Then
   `pve.py init --host <addr> --token-id claude@pve!agent` writes `$PVE_HOME/pve.env` with a
   placeholder secret and prints how to create the token (web UI or `pveum` on a node). Ask the user
   to create the token and **paste the secret into the file themselves**. If they insist on giving it
   to you, pipe it in with `--secret-stdin --force` and never repeat it.
   - Already have an admin token in env vars or a file? `pve.py bootstrap-user claude` creates a
     dedicated user + token (role Administrator) and writes `pve.env` for it.
   - Administrator is needed for host-network edits; `PVEAdmin` covers VMs/CTs/pools if they prefer less.
3. `pve.py doctor` again: every `FAIL` must be fixed; read `WARN`s to the user (missing privileges,
   no SDN, missing `ssh`/`ping`, etc.).
4. **Learn the environment** at the start of each task that depends on it: `status` (nodes, RAM,
   guests, storage), `net list` (bridges), `template list`, `sdn list`, `lab list`. Treat existing
   guests as the user's - never change or delete them unless asked by name.

## 3. Running conventions

* Exit codes: `0` ok · `1` error (read it) · `2` dry run (needed `--yes`) · `3` **partial**: the
  `PVE_BUDGET`/`--budget` time limit was hit; nothing is lost - **re-run the identical command**
  until it exits 0. Set the budget ~30 s below your shell's per-call timeout (none needed otherwise).
* `--json` on any command for machine-readable output. Guests are addressed by name or VMID.
* Destructive commands (`delete`, `lab destroy`, `snap rollback`, `net apply/delete/revert`,
  `vm template`) are dry runs until `--yes`.

## 4. Turning a prompt into action

1. **Look first** when state matters (section 2 step 4).
2. **One-off changes** -> direct commands (section 5).
3. **Two or more machines, or anything needing its own network -> a lab blueprint**
   (`references/blueprints.md`, examples in `examples/`): write `$PVE_HOME/labs/<lab>.json`, run
   `lab plan`, summarize it for the user (machines, CPU/RAM/disk, networks, node, total RAM) and get a
   go-ahead unless they already said to just do it. Then `lab apply <file> --yes`, re-run on exit 3,
   finish with `lab status <lab>` and give the user the table and logins.
4. **Destructive or disruptive** actions: say exactly what will happen, get confirmation, then `--yes`.
5. **Capacity**: keep ~25% of each node's RAM free after counting running guests; spread across nodes
   with a per-machine `"node"` (isolated SDN lab networks are node-local). Say so if it won't fit.

**CT or VM?** LXC containers for light Linux services (web, DB, DNS, proxies, jump hosts, simple
targets) - fast and cheap. VMs for Kubernetes, heavy Docker, custom kernels/modules/VPNs, Windows,
BSD/pfSense/OPNsense, desktops and security tooling.

**Images**: cloud-init VM templates are built on demand from official images: `ubuntu-24.04`
(default), `ubuntu-26.04`, `ubuntu-22.04`, `debian-13`, `debian-12`, `rocky-9`, `alma-9` (amd64).
Anything else: installer ISOs (`iso list`, `iso download URL`). Container templates (`debian-12`
default, `ubuntu-24.04`, `alpine`, `rockylinux-9`, ...) download automatically (`template ct-available`).

## 5. Command reference

```
init --host H [--token-id u@realm!name] [--secret-stdin]     doctor     whoami
status | nodes | ls [--node N] [--type vm|ct] [--lab L] [--running] [--templates]
storage [--node N] [--content iso|vztmpl|images|import]   tasks [--errors]
info ID | ip ID... [--scan]           (--scan finds DHCP guests without guest agent via ping+ssh)
start|stop|shutdown|reboot|suspend|resume ID... [--timeout S] [--force]
set ID key=value... [--delete k1,k2]  any Proxmox config key: cores=4 memory=8G onboot=1
                                      net1=virtio,bridge=vmbr1 tags=web  (may need reboot - it says so)
resize ID 64G|+10G [--disk scsi1]     migrate ID NODE      delete ID... --yes
snap list|create|rollback|delete ID [NAME] [--vmstate] [--yes]
vm create NAME --template ubuntu-24.04 [--node N] [--cores C] [--memory 4G] [--disk 32G]
        [--net auto | --net bridge=vmbr1,ip=10.0.0.5/24,gw=10.0.0.1,vlan=20] [--user lab]
        [--password auto|PW] [--ssh-key auto|PUBKEY|path] [--full] [--no-start] [--pool P] [--tags T]
vm create NAME --iso <name-part> [--ostype l26|win11|win10|other] [--virtio-iso virtio-win] [--efi] [--tpm]
        (win11 = Windows 11 / Server 2022/2025, win10 = Windows 10 / Server 2016/2019; EFI+TPM added)
vm clone SRC NAME [--full] [--start]   vm template ID --yes
ct create NAME --template debian-12 [--memory 512M] [--disk 8G] [--net ...] [--password auto]
        [--ssh-key auto] [--privileged] [--no-nesting] [--no-start]
template list | build DISTRO [--node N] [--bridge B] [--no-agent] | ct-available [--filter X]
        | ct-download SPEC [--node N|all]
iso list | download URL [--node N|all] [--filename X.iso] [--checksum H]
net list [--node N] | pending | add-bridge NAME|auto [--node N|all] [--ports nic1] [--cidr C]
        [--vlan-aware] [--apply] | add-vlan --raw-device vmbr0 --tag 30 [--cidr C] [--apply]
        | set IFACE k=v... [--apply] [--force] | delete IFACE --yes [--apply] | apply --yes | revert --yes
sdn list | zone-add ID --type simple|vlan|vxlan [--bridge B] [--apply]
        | vnet-add ID --zone Z [--tag N] [--subnet CIDR --gateway G --snat] [--apply]
        | vnet-del ID --yes [--apply] | zone-del ID --yes | apply
lab plan FILE | apply FILE --yes | status LAB | list | start|stop|shutdown LAB | destroy LAB --yes | example
api GET|POST|PUT|DELETE /path [k=v...] [--wait]      raw API for anything not covered
bootstrap-user NAME [--role R] [--token T] [--out FILE]
```

## 6. Labs in one paragraph

A blueprint names the lab, its networks and machines (`count` expands `web` -> `web1`, `web2`).
Networks are either an existing bridge (`{"bridge": "auto", "dhcp": true}` = the node's LAN bridge,
or a static `cidr` + `range`, optional `vlan`) or **isolated** (`{"isolated": true, "nat": true}`):
a new SDN vnet with its own /24, static IPs from .10, and optional NAT to the internet. Everything gets
pool + tag `lab-<name>`; IPs, VMIDs and passwords are kept in `labs/<name>.state.json`, so re-running
`lab apply` after editing the file only adds what's new. Isolated networks are reachable from the
user's LAN only through a **dual-homed jump container** (LAN DHCP + lab net) - add one whenever the
user will want to log in. Use `"nat": false` for cyber ranges/malware. Full schema:
`references/blueprints.md`.

## 7. Getting into machines

* `lab status LAB` / `vm create` / `ct create` print logins: VMs use the cloud-init user (default
  `lab`) + password, CTs `root` + password. All get the lab SSH key `$PVE_HOME/keys/lab_ed25519`.
* SSH (copy the key to a private 0600 file first if the folder's permissions are loose):
  `ssh -i KEY lab@IP`, through a jump host: `ssh -i KEY -J root@JUMP_IP lab@10.66.0.11`
  (or `-o ProxyCommand="ssh -i KEY -W %h:%p root@JUMP_IP"` when `-J` can't pass the key).
  Use this to install/configure software after provisioning.
* Templates built by `template build` have qemu-guest-agent baked in (one-time boot on the
  management bridge, which needs DHCP + internet and must be reachable from where pve.py runs),
  so DHCP VMs report their IPs ~30-60 s after boot. For other guests use `ip NAME --scan`.

## 8. Host networking rules

* Edits are staged; `net pending` shows the diff; `net apply --yes` activates them (ifreload).
* The interface carrying a node's management IP/gateway (usually `vmbr0` + its NIC) is protected:
  `net set` needs `--force`, `net delete` refuses. Use `--force` only on explicit request after
  warning that a mistake can cut the node off. Prefer adding bridges/VLANs/SDN over editing it.
* `--apply` refuses when a node already has someone else's staged changes - show `net pending`
  and ask before `net apply --yes` / `net revert --yes`.
* Create bridges on every node (`--node all`, the default) so guests can migrate.
* Cluster-wide virtual networks: prefer SDN (`sdn vnet-add` in zone `labs`, or a `vlan` zone on
  the LAN bridge when the switch carries VLANs).

## 9. Troubleshooting

* `Cannot reach Proxmox`: wrong host, firewall, or a sandbox without LAN egress (section 1).
* `HTTP 401`: wrong token id/secret. `HTTP 403 ... Permission check failed`: the token lacks a
  privilege - `doctor` lists which; privilege separation must be off or the token needs its own ACL.
* Isolated networks need SDN (PVE 8.1+). SDN DHCP needs `dnsmasq` on the nodes - labs don't rely on
  it (static IPs). Without SDN, use bridge networks.
* No shared storage: templates are per node and are built on the target node automatically; with
  shared storage (Ceph/NFS) clones go straight to any node.
* Cloud-image templates need the node to download from the internet and use `import` content on a
  directory storage (pve.py enables it on `local` if missing - mention that to the user).
* Brand-new guests can look nameless for a few seconds (cluster cache lag); pve.py works around it.
* Linked clones depend on their template: delete them before deleting/rebuilding the template.
* Don't run two write commands against the same lab at the same time.
