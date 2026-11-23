# Points kubectl (and every script) at the AWS cluster. The tunnel must be open (cloud-tunnel.ps1).
#     .\scripts\use-cloud.ps1
. "$PSScriptRoot\cloud-common.ps1"
kubectl config use-context $cloudContext
if (-not (Tunnel-Open)) { Write-Host "The tunnel isn't open. In another window run: .\scripts\cloud-tunnel.ps1" -ForegroundColor Yellow }
