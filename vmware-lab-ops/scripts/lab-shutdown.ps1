#requires -Version 5
<#
.SYNOPSIS
  Gracefully shut down a VMware Workstation lab in reverse dependency order.

.DESCRIPTION
  Reads VM names (in the order to shut them down) and the vmrun/lab paths, then calls
  `vmrun ... stop <vmx> soft` for each. Soft shutdown needs VMware Tools in the guest.
  Nothing here contains credentials. Run this ON THE VMWARE HOST (or via the device/remote
  shell that executes on the host) — not in a cloud sandbox.

.EXAMPLE
  .\lab-shutdown.ps1 -Vmrun "C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe" `
                     -LabRoot "E:\Lab" -Vms client-b,app01,fs01,dc01,dhcp01,r1,fw01

.EXAMPLE
  # Force (hard) shutdown for any guest that ignores soft — prompts per VM
  .\lab-shutdown.ps1 -LabRoot "E:\Lab" -Vms app01,fw01 -Force
#>
[CmdletBinding(SupportsShouldProcess)]
param(
  [string]$Vmrun = "C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe",
  [Parameter(Mandatory)][string]$LabRoot,
  [Parameter(Mandatory)][string[]]$Vms,   # in shutdown order: clients -> apps -> infra -> gateways
  [ValidateSet("ws","fusion")][string]$HostType = "ws",
  [switch]$Force                           # use 'hard' instead of 'soft'
)

if (-not (Test-Path $Vmrun)) { throw "vmrun not found at '$Vmrun' — pass -Vmrun with the correct path." }

$mode = if ($Force) { "hard" } else { "soft" }

foreach ($vm in $Vms) {
  $vmx = Join-Path $LabRoot (Join-Path $vm "$vm.vmx")
  if (-not (Test-Path $vmx)) { Write-Warning "skip '$vm': $vmx not found"; continue }
  if ($PSCmdlet.ShouldProcess($vm, "vmrun stop ($mode)")) {
    Write-Host ">>> stopping $vm ($mode)"
    & $Vmrun -T $HostType stop "$vmx" $mode
    if ($LASTEXITCODE -ne 0) { Write-Warning "  vmrun returned $LASTEXITCODE for $vm" }
  }
}
Write-Host ">>> done."
