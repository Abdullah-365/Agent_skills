# vmware-lab-ops

An agent skill that lets Claude (or any agent that reads `SKILL.md` skills) operate a
**VMware Workstation / Fusion Pro** virtualization lab from plain-language requests:

> "Start the firewall and DC headless, snapshot the firewall first"
> "Set fw01 to 4 GB of RAM and put ens160 on a tagged trunk"
> "SSH through the gateway and restart the docker container on the DMZ box"
> "Rebuild the stateless ruleset but don't lock me out of the gateway"
> "The lab VM is stuck at the initramfs prompt — get it booting"

It works whether the agent has a shell **on the VMware host** (Claude Code on the host), can **SSH
to** the host, or reaches it through the **device/remote shell** in Cowork — the first thing the
skill does is pick the right execution path, because the sandbox an agent runs in usually can't
reach a local lab by itself.

## What it can do

- **VM lifecycle** (`vmrun`): headless start/stop/reset/suspend, snapshots and clones, and in-guest
  command execution + file copy over the VMware Tools channel when guest networking is down.
- **`.vmx` configuration**: RAM/CPU, adapter type, VMnets and private LAN segments — using `dictTool`
  (the supported editor added in Workstation/Fusion 25H2) or safe hand-editing, with GUI equivalents.
- **Networking**: 802.1Q VLAN trunks on a managed switch, Linux router subinterfaces (Netplan /
  systemd-networkd), and a segment plan for a multi-VLAN lab gateway.
- **Cross-host SSH pivoting**: key-first auth, `-J` jump chains, SOCKS proxies, cross-hop file copy.
- **Stateless `iptables` firewalls**: default-DROP, symmetrical forward/reverse rule pairs with
  `! --syn` instead of conntrack, NAT confined to the `nat` table, plus a kill-switch + dead-man's
  switch so you don't lock yourself out of a remote gateway while applying rules.
- **Disaster recovery**: initramfs `fsck`, stale-PID service recovery, ordered clean shutdown, and
  host-side VMware service restarts.

## Safety

- **No baked-in secrets or paths.** Hosts, users, VM names and the `vmrun` path come from a `lab.env`
  config or from you — never hard-coded, and passwords are never printed back.
- **Confirms before destructive or disruptive actions** (hard power-off, snapshot revert, VM/snapshot
  delete, firewall flush, networking `.vmx` edits, management-interface changes).

## Requirements

- VMware Workstation Pro or Fusion Pro on the host (`vmrun`; `dictTool` on **25H2+**).
- Guest control needs VMware Tools / open-vm-tools in the guest.
- Cross-host and firewall workflows need `ssh` and `iptables`.
- In Cowork/cloud sessions the host is reached through the device/remote shell, and the network-access
  policy must allow the lab's addresses.

## Install

**Claude Code** — personal or per-project:
```bash
git clone https://github.com/Abdullah-365/Agent_skills
cp -r Agent_skills/vmware-lab-ops ~/.claude/skills/          # or <project>/.claude/skills/
```

**Claude apps (claude.ai, desktop, Cowork)** — zip the `vmware-lab-ops` folder (the folder itself,
not just its contents), then **Customize > Skills > + > Create skill > Upload a skill**. In Cowork,
Claude runs commands on your linked computer; set network egress to allow the lab's hosts.

**Other agents** that support Agent Skills: copy the folder into their skills directory.

## Configure (once)

Create a `lab.env` in your project / connected folder — see `SKILL.md` §0.1 for the full template:

```ini
VMRUN="C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe"
DICTTOOL="C:\Program Files (x86)\VMware\VMware Workstation\dictTool.exe"   # 25H2+
LAB_ROOT="E:\Lab"
GUEST_USER="labadmin"
GW_HOST="172.16.1.1"
SSH_USER="labadmin"
SSH_KEY="~/.ssh/lab_ed25519"
```

Never commit a real `lab.env` — the repo's root `.gitignore` already excludes `*.env`.

## Layout

```
vmware-lab-ops/
├── SKILL.md                     # entry point: execution path, config, workflows
├── references/                  # loaded on demand
│   ├── vmrun-cheatsheet.md      # every vmrun verb, per-platform paths
│   ├── vmx-reference.md         # .vmx keys, dictTool, GUI equivalents, dead VNC keys
│   ├── pivoting.md              # SSH jump chains, SOCKS, cross-hop copy
│   ├── networking.md            # switch VLANs, Netplan/networkd, segment plan
│   ├── stateless-firewall.md    # rule templates + safe remote-apply
│   └── recovery.md              # initramfs, stale PIDs, shutdown order, host services
└── scripts/
    └── lab-shutdown.ps1         # ordered graceful shutdown (Windows host)
```

## License

MIT — see [LICENSE](../LICENSE).
