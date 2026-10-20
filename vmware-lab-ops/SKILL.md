---
name: vmware-lab-ops
description: >-
  Operate a VMware Workstation / Fusion Pro virtualization lab from Claude: start, stop, snapshot
  and script VMs with vmrun, inspect and edit .vmx configuration, wire up VMnets / LAN segments and
  802.1Q VLAN trunks, pivot across hosts over SSH, build and recover stateless iptables firewalls,
  and run disaster-recovery triage. Use this whenever the user mentions VMware Workstation, Fusion,
  vmrun, a .vmx file, a virtual-machine lab, VMnet/LAN segments, a pfSense/Linux lab gateway,
  lab VLANs, SSH pivoting between lab hosts, iptables lab firewalls, or recovering a hung lab VM —
  even if they don't name this skill. Works whether Claude has a shell on the VMware host directly
  or reaches the host through a device/remote shell in Cowork or a cloud session; read section 0
  first to pick the execution path, because the sandbox Claude runs in usually cannot reach the lab
  by itself.
license: MIT
compatibility: >-
  Requires VMware Workstation Pro or Fusion Pro on the host (vmrun; dictTool on 25H2+). Guest control
  needs VMware Tools / open-vm-tools in the guest. Cross-host and firewall workflows need SSH and
  iptables. In Cowork/cloud sessions the host is reached through the device/remote shell and the
  network-access policy must permit the lab's addresses.
metadata:
  version: "2.0.0"
  supersedes: "vmware-distributed-lab-operations"
---

# VMware lab operations

A runbook for driving a VMware Workstation / Fusion Pro lab — VM lifecycle, `.vmx` tuning, virtual
networking and VLAN trunks, cross-host SSH pivoting, stateless `iptables` firewalls, and recovery.

Two rules that override convenience everywhere below:

- **Never bake secrets into commands, files, or this skill.** The lab's usernames, passwords, hosts,
  and paths are *not* known in advance — read them from the config in section 0.1, from the user, or
  from an SSH key. Never echo a password back to the user. Treat any credential the user gives as
  single-use for that turn.
- **Confirm before anything destructive or disruptive:** `stop ... hard`, `reset`, deleting a VM or
  snapshot, flushing a firewall, editing a `.vmx` that controls networking, or touching a management
  interface. State exactly what will happen, then act.

---

## 0. Where does this command actually run?

This is the part the old version of the skill got wrong, so settle it **before** running anything.

Claude's own shell (the `bash` tool in chat, or the sandbox in Cowork/cloud) is **not** the VMware
host. In Cowork and cloud sessions that sandbox is network-isolated and by policy cannot reach
private, internal, or link-local addresses at all — so `vmrun`, SSH to `172.16.x.x`, and anything
else that touches the lab must execute **on the user's machine**, not in Claude's sandbox.

Pick the path that matches the session:

| Session | How lab commands run | Notes |
|---|---|---|
| Claude Code / a terminal **on the VMware host itself** | Run directly with the shell tool. | `vmrun`, SSH, etc. all work locally. |
| Claude Code on a machine that can **SSH to** the host | SSH into the host, then run there. | One hop; see section 3. |
| **Cowork** (desktop) | Run through the **device/remote shell** that executes on the user's laptop, not the cloud sandbox. | The sandbox can't see the LAN. Lab IPs need the network-access policy set to allow them. |
| **Cloud** session | Usually cannot reach a local lab at all. | Say so; fall back to a host-side path. |

**If a command that touches the lab fails with a network/connection/`blocked-by-allowlist` error,
stop and diagnose the path — do not just retry.** The usual causes, in order: the command ran in
Claude's sandbox instead of on the host; the Cowork network-access policy blocks private addresses
(user fix: Claude Settings → Capabilities / network access); the connected-folder or device-shell
permission is missing; or the host/guest is actually down.

### 0.1 Lab config — resolve this first, every session

Nothing in this skill hard-codes a path, host, user, or VM. Resolve these before acting, preferring
a committed config file over asking:

1. Look for a `lab.env` (or `vmware-lab.json`) in the project / connected folder and read it.
2. Otherwise ask the user once, and offer to save a `lab.env` for next time.

```ini
# lab.env — example only. Real values come from the user; never commit real passwords.
VMRUN="C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe"   # host path to vmrun
DICTTOOL="C:\Program Files (x86)\VMware\VMware Workstation\dictTool.exe"  # 25H2+ only
LAB_ROOT="E:\Lab"                 # where the .vmx files live
GUEST_USER="labadmin"             # VMware Tools guest account (per-VM may differ)
GW_HOST="172.16.1.1"              # lab gateway / firewall for pivots
SSH_USER="labadmin"
SSH_KEY="~/.ssh/lab_ed25519"      # prefer key auth over passwords, always
```

> **Platform note.** `vmrun`/`dictTool` live under `/Applications/VMware Fusion.app/Contents/Library/`
> on Fusion (macOS) and `/usr/bin/vmrun` on Linux Workstation. On Windows with PowerShell as the
> host shell, set a `$VMRUN` variable; on macOS/Linux call the binary by path. The command *syntax*
> below is identical across platforms — only the executable path changes.

---

## 1. VM lifecycle with `vmrun`

`vmrun` is the headless controller; it needs no GUI. Resolve `$VMRUN` and the `.vmx` path from the
config first (`"$LAB_ROOT/fw01/fw01.vmx"`).

```bash
# List running VMs
"$VMRUN" list

# Start headless (no console window, saves host RAM/GPU)
"$VMRUN" -T ws start "$LAB_ROOT/fw01/fw01.vmx" nogui

# Graceful shutdown (needs VMware Tools in the guest) — prefer this
"$VMRUN" -T ws stop "$LAB_ROOT/fw01/fw01.vmx" soft

# Hard power off — DESTRUCTIVE to in-flight writes; confirm first, use only if the guest is hung
"$VMRUN" -T ws stop "$LAB_ROOT/fw01/fw01.vmx" hard

# Reset / suspend
"$VMRUN" -T ws reset   "$LAB_ROOT/fw01/fw01.vmx" soft
"$VMRUN" -T ws suspend "$LAB_ROOT/fw01/fw01.vmx" soft
```

On a Fusion host, `-T fusion`; on Linux Workstation, `-T ws` still applies.

### Snapshots — take one before risky changes

```bash
"$VMRUN" -T ws listSnapshots      "$LAB_ROOT/fw01/fw01.vmx"
"$VMRUN" -T ws snapshot           "$LAB_ROOT/fw01/fw01.vmx" "pre-firewall"
"$VMRUN" -T ws revertToSnapshot   "$LAB_ROOT/fw01/fw01.vmx" "pre-firewall"   # confirm: discards changes
"$VMRUN" -T ws deleteSnapshot     "$LAB_ROOT/fw01/fw01.vmx" "pre-firewall"   # confirm
```

### In-guest commands & file transfer (when the guest network is down)

Runs over the VMware Tools channel, so it works even with no guest networking. Pass guest creds from
the config — **never** inline a literal password into a command you show the user; read it from an
env var or prompt.

```bash
# Linux guest — write current IPs to a file
"$VMRUN" -T ws -gu "$GUEST_USER" -gp "$GUEST_PW" runProgramInGuest \
  "$LAB_ROOT/fw01/fw01.vmx" -noWait /bin/bash -c "ip addr > /tmp/ip.txt"

# Windows guest
"$VMRUN" -T ws -gu Administrator -gp "$GUEST_PW" runProgramInGuest \
  "$LAB_ROOT/dc01/dc01.vmx" C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe \
  -Command "ipconfig /all > C:\ip.txt"

# Copy files host <-> guest
"$VMRUN" -T ws -gu "$GUEST_USER" -gp "$GUEST_PW" copyFileFromHostToGuest \
  "$LAB_ROOT/fw01/fw01.vmx" "./rules.sh" "/tmp/rules.sh"
"$VMRUN" -T ws -gu "$GUEST_USER" -gp "$GUEST_PW" copyFileFromGuestToHost \
  "$LAB_ROOT/fw01/fw01.vmx" "/tmp/output.log" "./output.log"
```

For a long reference of every `vmrun` verb, see `references/vmrun-cheatsheet.md`.

---

## 2. `.vmx` configuration

Edit `.vmx` only while the VM is **powered off and Workstation isn't holding the file**, or Workstation
overwrites your change on exit. On **Workstation/Fusion 25H2+**, prefer `dictTool` (the supported
config editor) over hand-editing:

```bash
"$DICTTOOL" --get "$LAB_ROOT/fw01/fw01.vmx" ethernet0.connectionType
"$DICTTOOL" --set 'ethernet0.virtualDev = "vmxnet3"' "$LAB_ROOT/fw01/fw01.vmx"
```

On older builds, edit the file directly (back it up first: `cp fw01.vmx fw01.vmx.bak`). Common keys:

```ini
memsize = "2048"                 # RAM in MB
numvcpus = "2"
cpuid.coresPerSocket = "2"

ethernet0.virtualDev = "vmxnet3" # vmxnet3 preferred; e1000e for legacy/compat

# LAN segment (private host-only broadcast domain) — the pvnID is the segment's GUID
ethernet0.connectionType = "pvn"
ethernet0.pvnID = "<LAN-segment-GUID>"

# Custom VMnet (e.g. bridged VMnet0)
ethernet0.connectionType = "custom"
ethernet0.vnet = "VMnet0"
```

> **Dropped feature — don't use it.** `RemoteDisplay.vnc.*` (the built-in VMX VNC server) is removed
> on modern VMware and does nothing on current Workstation/Fusion; it only breaks consoles on
> upgraded VMs. For a headless console use the VMware Remote Console (VMRC) or SSH into the guest.
> See `references/vmx-reference.md` for the full key list and the networking GUI equivalents.

---

## 3. Cross-host SSH pivoting

Backend nodes usually sit behind the lab gateway (`$GW_HOST`). **Prefer SSH keys over passwords.**

```bash
# Direct to the gateway
ssh -i "$SSH_KEY" "$SSH_USER@$GW_HOST" "sudo iptables -L -n -v"

# One hop to an internal node via the gateway as jump host (-J)
ssh -i "$SSH_KEY" -J "$SSH_USER@$GW_HOST" "$SSH_USER@172.16.1.51" "docker ps"
```

`StrictHostKeyChecking=no` disables host-key verification — acceptable only on a throwaway isolated
lab, never for anything exposed. If keys genuinely aren't set up and a password is unavoidable, have
the **user** run the interactive `ssh`/`sshpass` themselves rather than Claude embedding the secret.
Details and multi-hop patterns in `references/pivoting.md`.

---

## 4. Lab networking & VLAN trunks

### Physical managed switch (e.g. TP-Link TL-SG108E)
- **Trunk ports:** 802.1Q-tagged membership for every active lab VLAN on inter-host links.
- **Access/management ports:** untagged, with the port's PVID (e.g. management on VLAN 1).
- **PVID rule:** an untagged ingress frame is tagged with the port's PVID; tagged frames match the
  802.1Q table.

### Linux 802.1Q router subinterfaces (Netplan / `systemd-networkd`)
A trunk NIC carries VLAN subinterfaces, each a gateway for its segment:

```yaml
# /etc/netplan/01-netcfg.yaml on the lab gateway
network:
  version: 2
  renderer: networkd
  ethernets:
    ens33: { dhcp4: false, addresses: [10.1.1.1/24] }   # WAN
    ens160: { dhcp4: false }                             # trunk (tagged)
  vlans:
    ens160.10: { id: 10, link: ens160, addresses: [172.16.1.1/28] }   # Users
    ens160.20: { id: 20, link: ens160, addresses: [172.16.1.33/28] }  # Core
    ens160.30: { id: 30, link: ens160, addresses: [192.168.1.1/26] }  # DMZ
```

Apply with `sudo netplan try` (auto-reverts if you lose the link) then `sudo netplan apply`. Full
segment plan and switch config walkthrough: `references/networking.md`.

---

## 5. Stateless iptables firewalls

When a lab requires **stateless** filtering, connection-tracking matches (`-m conntrack`, `-m state`,
`--state ESTABLISHED,RELATED`) are **forbidden in the `filter` table**. Principles:

1. **Default DROP** on `INPUT`, `FORWARD`, `OUTPUT`.
2. **Symmetrical rule pairs:** every allowed flow gets a forward rule and a reverse rule, with return
   TCP matched by `! --syn` (or explicit `--tcp-flags`) instead of state.
3. **NAT stays in the `nat` table:** the kernel handles NAT translation independently of `filter`.

```bash
# Admin host -> DMZ SSH, stateless pair
iptables -A FORWARD -s 172.16.1.18 -d 192.168.1.0/26 -p tcp --dport 22 -j ACCEPT
iptables -A FORWARD -s 192.168.1.0/26 -d 172.16.1.18 -p tcp --sport 22 ! --syn -j ACCEPT
```

### Always install a kill switch + restore before applying restrictive rules
Losing SSH to a remote lab gateway while writing firewall rules is the classic way to brick a lab.
Install these *first*, and ideally take a VM snapshot (section 1):

```bash
# /usr/local/bin/fw-killswitch — emergency open-all (keeps NAT so routing survives)
cat > /usr/local/bin/fw-killswitch <<'EOF'
#!/bin/bash
iptables -F; iptables -X
iptables -P INPUT ACCEPT; iptables -P FORWARD ACCEPT; iptables -P OUTPUT ACCEPT
iptables -t nat -C POSTROUTING -o "$WAN_IF" -j MASQUERADE 2>/dev/null \
  || iptables -t nat -A POSTROUTING -o "$WAN_IF" -j MASQUERADE
echo ">>> [EMERGENCY] filter rules flushed to ACCEPT."
EOF

# /usr/local/bin/fw-restore — re-apply the saved ruleset
cat > /usr/local/bin/fw-restore <<'EOF'
#!/bin/bash
if [ -f "$RULES_SCRIPT" ]; then bash "$RULES_SCRIPT"
else iptables-restore < /etc/iptables/rules.v4; fi
echo ">>> [RESTORE] rules reapplied."
EOF
chmod +x /usr/local/bin/fw-killswitch /usr/local/bin/fw-restore
```

Set `$WAN_IF` and `$RULES_SCRIPT` from the config. A safe way to apply new rules remotely: schedule
`fw-killswitch` to run in a few minutes (`at now + 3 minutes`), apply your rules, confirm you still
have access, then cancel the scheduled job. Worked examples: `references/stateless-firewall.md`.

---

## 6. Disaster recovery & triage

### Boot stuck at `(initramfs)` (root FS corruption after an unclean stop)
```bash
fsck -y /dev/mapper/ubuntu--vg-ubuntu--lv   # LVM root
fsck -y /dev/sda1                           # plain ext4
exit                                        # resume boot
```

### Service won't start — stale PID lock
```bash
systemctl status <svc>
rm -f /path/to/<svc>.pid      # remove stale lock (confirm it's genuinely stale first)
systemctl restart <svc>
ss -tulnp | grep <port>       # verify it's listening
```

### Clean lab shutdown order (prevents disk corruption, frees host RAM)
Shut down in reverse dependency order: **clients → app servers → core infra / AD → routers/gateways.**
Drive it with `vmrun ... stop ... soft` per VM (section 1). A ready-to-edit loop and more recovery
recipes are in `references/recovery.md`.

---

## Reference files (load as needed)

- `references/vmrun-cheatsheet.md` — every `vmrun` verb, per-platform paths, snapshot/clone recipes.
- `references/vmx-reference.md` — full `.vmx` key reference, `dictTool` usage, GUI equivalents.
- `references/pivoting.md` — SSH jump chains, SOCKS proxies, multi-hop file copy.
- `references/networking.md` — switch config, VLAN/segment plan, Netplan patterns.
- `references/stateless-firewall.md` — stateless ruleset templates and safe remote-apply.
- `references/recovery.md` — initramfs, stale PIDs, shutdown loops, host-service triage.
