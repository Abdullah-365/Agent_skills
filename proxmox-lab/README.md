# proxmox-lab

An agent skill that lets Claude (or any agent that reads `SKILL.md` skills) run your
**Proxmox VE** server or cluster from plain-language requests:

> "Spin up a 3-node Kubernetes lab on an isolated network"
> "Give the build VM 8 GB of RAM and snapshot it first"
> "Add an internal bridge on every node"
> "Tear down the web lab"

It ships `scripts/pve.py`, a single-file, standard-library-only Python CLI that uses the Proxmox
REST API with an API token. Your agent writes the commands; you can also use it by hand.

## What it can do

- **VMs**: from cloud-init templates (built automatically from official Ubuntu, Debian, Rocky and
  Alma images, with qemu-guest-agent baked in) or from installer ISOs, Windows included (EFI, TPM,
  virtio drivers).
- **Containers**: LXC from any Proxmox appliance template (downloaded on demand).
- **Day-to-day**: start/stop/reboot, change any config key, grow disks, snapshots and rollback,
  migration, IP discovery, raw API calls for everything else.
- **Networking**: list/add/edit host bridges and VLANs (staged, with diff and a guard on the
  management interface) and SDN zones, vnets and subnets with NAT.
- **Labs**: describe machines and networks in one JSON blueprint, then plan, build, extend, start,
  stop and destroy the whole thing. Each lab gets its own pool, tag, optional isolated network with
  static IPs and NAT, generated logins and an SSH key. Builds can resume after interruption.
- **Safety**: destructive actions are dry runs until `--yes`. Nothing touches guests you didn't name.

## Requirements

- Proxmox VE 8.x or 9.x (isolated lab networks need SDN, 8.1+). Works with single nodes and clusters,
  local or shared storage.
- Python 3.8+ on a machine that can reach `https://<proxmox>:8006`.
- Optional: `ssh`, `ssh-keygen` and `ping` on that machine (guest-agent baking, IP discovery, logging in).
- An API token (the setup below takes two minutes).

## Install

**Claude Code** - personal or per-project:
```bash
git clone https://github.com/Abdullah-365/Agent_skills
cp -r Agent_skills/proxmox-lab ~/.claude/skills/          # or <project>/.claude/skills/
```

**Claude apps (claude.ai, desktop, Cowork)** - zip the `proxmox-lab` folder (the folder itself, not
just its contents), then **Customize > Skills > + > Create skill > Upload a skill**. In Cowork,
Claude runs the script on your linked computer; set network egress to allow LAN hosts.

**Other agents** that support Agent Skills: copy the folder into their skills directory.

**No agent**: `python3 proxmox-lab/scripts/pve.py -h`

## Setup (once)

1. Create a token on Proxmox - web UI (*Datacenter > Permissions*: add user `claude@pve`, give it
   role `Administrator` on `/`, add API token `agent` with **Privilege Separation unticked**), or on
   any node as root:
   ```bash
   pveum user add claude@pve --comment "AI agent"
   pveum acl modify / --users claude@pve --roles Administrator
   pveum user token add claude@pve agent --privsep 0     # copy the secret, it is shown once
   ```
   `PVEAdmin` instead of `Administrator` works too if you don't want host-network changes.
2. Write the connection file and check it:
   ```bash
   python3 scripts/pve.py init --host 192.0.2.10 --token-id claude@pve!agent
   # paste the secret into ~/.proxmox-lab/pve.env, then:
   python3 scripts/pve.py doctor
   ```
   Or just ask your agent to "set up the Proxmox skill" and it will walk you through it.

Config and data live in `~/.proxmox-lab/` (override with `PVE_HOME`): `pve.env`, `labs/` (your
blueprints and their state, including generated passwords) and `keys/` (the lab SSH key). Keep that
folder private.

## CLI tour

```bash
pve.py status                                   # nodes, guests, storage
pve.py vm create web1 --template ubuntu-24.04 --memory 4G --disk 32G --ssh-key auto
pve.py vm create win1 --iso server_2022 --virtio-iso virtio-win --ostype win11 --memory 8G --disk 80G
pve.py ct create dns --template debian-12 --net bridge=vmbr0,ip=192.0.2.53/24,gw=192.0.2.1
pve.py set web1 cores=4 memory=8G
pve.py snap create web1 before-upgrade
pve.py net add-bridge auto --comment lab-internal --apply
pve.py lab plan  examples/k8s.json
pve.py lab apply examples/k8s.json --yes
pve.py lab status k8s
pve.py lab destroy k8s --yes
```

Blueprint format: [`references/blueprints.md`](references/blueprints.md). Examples:
[`examples/`](examples) (Kubernetes, web stack, air-gapped cyber range with a Windows DC, machines on
your LAN).

## Settings (environment variables or pve.env)

| Variable | Default | |
|---|---|---|
| `PVE_HOST` | - | Any node's address (port 8006 assumed) |
| `PVE_TOKEN_ID` / `PVE_TOKEN_SECRET` | - | `user@realm!name` and its secret |
| `PVE_VERIFY_SSL` | `0` | `1` if your Proxmox has a trusted certificate |
| `PVE_HOME` | `~/.proxmox-lab` | Data directory |
| `PVE_BRIDGE` | auto | Default bridge (auto = the node's bridge with the default gateway) |
| `PVE_LAB_ZONE` | `labs` | SDN zone for isolated lab networks |
| `PVE_LAB_SUBNETS` | `10.66.0.0/16` | Pool that lab /24 networks are taken from |
| `PVE_BUDGET` | none | Seconds before a long command stops cleanly (exit 3) so a re-run resumes |

## Notes

- Building a template boots the cloud image once on the management bridge to install
  qemu-guest-agent; that bridge needs DHCP and internet, and the machine running pve.py must be
  able to reach it. Otherwise the template is still created, just without the agent.
- When no storage on a node allows `import` content, pve.py enables it on the `local` directory
  storage (needed to import cloud images).
- Images are amd64.

## License

MIT - see [LICENSE](../LICENSE).
