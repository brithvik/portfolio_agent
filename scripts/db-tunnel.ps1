# Opens a tunnel to the cluster's database and sets DATABASE_URL for THIS terminal, so you can
# run the agent on your laptop against real data. Start it with a dot and a space:
#
#     . .\scripts\db-tunnel.ps1
#
# The password comes straight from the Kubernetes secret and is never printed or saved.
# Close the tunnel with:  Stop-Job db-tunnel
$b64 = kubectl get secret postgres-credentials -o jsonpath="{.data.POSTGRES_PASSWORD}"
if (-not $b64) { Write-Host "Couldn't read the database secret. Is the cluster running?" -ForegroundColor Red; return }
$pw = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($b64))

Get-Job -Name db-tunnel -ErrorAction SilentlyContinue | Remove-Job -Force
Start-Job -Name db-tunnel { kubectl port-forward svc/postgres 5433:5432 } | Out-Null
Start-Sleep 3

$env:DATABASE_URL = "postgresql://portfolio:$pw@localhost:5433/portfolio"
Write-Host "Tunnel open on localhost:5433, and DATABASE_URL is set for this terminal." -ForegroundColor Green
