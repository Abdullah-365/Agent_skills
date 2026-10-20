# Disaster recovery & triage

## Boot halts at `(initramfs)` — root filesystem corruption

Usually follows an unclean host shutdown or a `stop ... hard`. From the initramfs prompt:

```bash
# LVM root (Ubuntu server default)
fsck -y /dev/mapper/ubuntu--vg-ubuntu--lv
# plain ext4
fsck -y /dev/sda1
exit            # resume boot
```

If `fsck` reports the device is busy/mounted, it isn't mounted rw at initramfs; if it still refuses,
boot a live ISO and fsck from there. For XFS roots use `xfs_repair` instead of `fsck`.

## Service won't start — stale PID lock

When a VM is powered off abruptly, a service's PID file can point at a reused PID, so systemd thinks
it's already running or wedged. Confirm it's genuinely stale (the PID isn't that service) before
deleting:

```bash
systemctl status <svc>
cat /path/to/<svc>.pid ; ps -p "$(cat /path/to/<svc>.pid)" -o comm=   # does the PID belong to <svc>?
rm -f /path/to/<svc>.pid
systemctl restart <svc>
ss -tulnp | grep <port>       # verify listening
journalctl -u <svc> -n 50 --no-pager
```

Common lock locations: `/run/<svc>.pid`, `/var/run/<svc>.pid`, app-specific dirs (e.g. a scanner
under `/opt/<vendor>/var/...`).

## Clean lab shutdown (prevents virtual-disk corruption)

Shut down in **reverse dependency order**: clients → app servers → core infra / AD → routers &
gateways. Edit the VM list and run from the host:

```bash
# bash (Linux/macOS host). On Windows PowerShell use the .ps1 loop in scripts/ if present.
VMRUN="/usr/bin/vmrun"; LAB_ROOT="$HOME/Lab"
for vm in "client-b" "app01" "fs01" "dc01" "dhcp01" "r1" "fw01"; do
  "$VMRUN" -T ws stop "$LAB_ROOT/$vm/$vm.vmx" soft
done
```

`soft` needs VMware Tools in each guest. If a guest ignores soft shutdown, fall back to `hard` for
that one VM only, after confirming with the user.

## Host-side VMware service recovery

If `vmrun list` errors or VMs won't start at all, the host's VMware services may be down:

- **Windows:** restart *VMware Authorization Service* (`VMAuthdService`) and *VMware NAT/DHCP*
  services from `services.msc`, or `net stop/start VMAuthdService`.
- **Linux:** `sudo systemctl restart vmware` (or `vmware-networks` for VMnet issues).
- **Fusion (macOS):** `sudo /Applications/VMware\ Fusion.app/Contents/Library/services/services.sh --restart`.

## When recovery commands "do nothing"

If a recovery command seems to run but changes nothing, re-check the execution path (SKILL.md §0):
in Cowork/cloud the command may have run in Claude's sandbox instead of on the host. Recovery only
works where the lab actually is.
