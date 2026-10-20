# .vmx reference

The `.vmx` is a plain `key = "value"` dictionary. Edit it only while the VM is powered off and
Workstation/Fusion is not holding the file open, or the app rewrites your change on exit. Back it up
first: `cp fw01.vmx fw01.vmx.bak`.

## Prefer dictTool on 25H2+

Workstation/Fusion **25H2** added `dictTool`, the supported way to read and edit `.vmx` (and the
preferences file). Use it instead of hand-editing when available:

```
dictTool --get '<key>' <vmx>
dictTool --set '<key> = "<value>"' <vmx>
dictTool --unset '<key>' <vmx>
dictTool --list <vmx>
```

Path, like vmrun, lives beside the app (e.g. `C:\Program Files (x86)\VMware\VMware Workstation\dictTool.exe`).
On older builds (17.x and earlier) there is no dictTool — edit the file with a text editor.

## Hardware keys

```ini
memsize = "2048"                 # RAM, MB
numvcpus = "2"                   # vCPUs
cpuid.coresPerSocket = "2"
virtualHW.version = "22"         # HW v22 = Workstation/Fusion 25H2
firmware = "efi"                 # or "bios"
```

## Network adapter

```ini
ethernet0.present = "TRUE"
ethernet0.virtualDev = "vmxnet3" # vmxnet3 (paravirtual, fast) preferred; e1000e for legacy/compat
ethernet0.addressType = "generated"
```

### Connection types

```ini
# Bridged to a physical NIC
ethernet0.connectionType = "bridged"

# NAT (VMnet8 by default)
ethernet0.connectionType = "nat"

# Host-only (VMnet1 by default)
ethernet0.connectionType = "hostonly"

# Custom VMnet
ethernet0.connectionType = "custom"
ethernet0.vnet = "VMnet0"

# LAN segment (private, host-only broadcast domain shared between VMs)
ethernet0.connectionType = "pvn"
ethernet0.pvnID = "<segment-GUID>"   # the GUID the GUI assigns to a LAN Segment
```

The LAN-segment GUID (`pvnID`) is created by the Workstation GUI under **VM → Settings → Network
Adapter → LAN Segments**. To put several VMs on the same isolated segment, copy the same `pvnID`
into each VM's `.vmx` (or just assign the named segment in the GUI, which is less error-prone).

### GUI equivalents (when hand-editing is risky)

| `.vmx` setting | GUI location |
|---|---|
| connectionType / vnet | VM → Settings → Network Adapter |
| LAN segment / pvnID | VM → Settings → Network Adapter → LAN Segments… |
| memsize / numvcpus | VM → Settings → Memory / Processors |
| VMnet subnet, DHCP, NAT | Edit → Virtual Network Editor (Windows) / Fusion → Settings → Network |

## Removed / dead keys — do not use

```ini
# The built-in VMX VNC server was REMOVED on modern VMware. These keys do nothing on current
# Workstation/Fusion and only break consoles on VMs upgraded from old versions. Delete them if an
# upgraded VM's console misbehaves:
#   RemoteDisplay.vnc.enabled
#   RemoteDisplay.vnc.port
#   RemoteDisplay.vnc.password
#   RemoteDisplay.vnc.key
```

For a headless console today, use **VMware Remote Console (VMRC)** or just SSH into the guest.
