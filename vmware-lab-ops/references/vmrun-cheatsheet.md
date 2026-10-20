# vmrun cheatsheet

`vmrun` is VMware Workstation/Fusion Pro's headless controller. Resolve the executable path and the
`.vmx` path from `lab.env` (see SKILL.md §0.1). Never inline a literal guest password into a command
you display — read it from an env var or prompt for it.

## Executable path by platform

| Host | Path |
|---|---|
| Windows (Workstation) | `C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe` |
| Linux (Workstation) | `/usr/bin/vmrun` |
| macOS (Fusion) | `/Applications/VMware Fusion.app/Contents/Library/vmrun` |

Host type flag: `-T ws` for Workstation (Windows/Linux), `-T fusion` for Fusion.

## Lifecycle

```
vmrun -T ws list                                   # running VMs
vmrun -T ws start   <vmx> [gui|nogui]              # nogui = headless
vmrun -T ws stop    <vmx> [soft|hard]              # soft needs VMware Tools; hard is destructive
vmrun -T ws reset   <vmx> [soft|hard]
vmrun -T ws suspend <vmx> [soft|hard]
vmrun -T ws pause   <vmx>
vmrun -T ws unpause <vmx>
```

## Snapshots

```
vmrun -T ws listSnapshots      <vmx>
vmrun -T ws snapshot           <vmx> <name>
vmrun -T ws revertToSnapshot   <vmx> <name>        # discards changes since the snapshot — confirm
vmrun -T ws deleteSnapshot     <vmx> <name> [andDeleteChildren]
```

## Clone

```
vmrun -T ws clone <src-vmx> <dest-vmx> full|linked [-snapshot=<name>] [-cloneName=<name>]
```
Linked clones depend on the source VM/snapshot — don't delete the parent while clones exist.

## In-guest execution (VMware Tools channel; works with no guest network)

```
vmrun -T ws -gu <user> -gp <pw> runProgramInGuest <vmx> [-noWait] [-activeWindow] [-interactive] <prog> [args...]
vmrun -T ws -gu <user> -gp <pw> runScriptInGuest  <vmx> <interpreter> <script-text>
vmrun -T ws -gu <user> -gp <pw> fileExistsInGuest <vmx> <path>
vmrun -T ws -gu <user> -gp <pw> listProcessesInGuest <vmx>
vmrun -T ws -gu <user> -gp <pw> copyFileFromHostToGuest <vmx> <host-path> <guest-path>
vmrun -T ws -gu <user> -gp <pw> copyFileFromGuestToHost <vmx> <guest-path> <host-path>
```

Prefer passing the password via an environment variable so it doesn't land in shell history or in
the transcript:

```bash
# user sets GUEST_PW in their own shell/session; Claude never prints it
vmrun -T ws -gu "$GUEST_USER" -gp "$GUEST_PW" listProcessesInGuest "$LAB_ROOT/fw01/fw01.vmx"
```

## IP / network discovery

```
vmrun -T ws getGuestIPAddress <vmx> [-wait]        # needs VMware Tools
```

## Notes

- `-noWait` returns immediately instead of blocking on the guest program.
- If `runProgramInGuest` fails with a Tools error, VMware Tools / open-vm-tools isn't running in the
  guest — fall back to SSH (§3) or the VM console.
