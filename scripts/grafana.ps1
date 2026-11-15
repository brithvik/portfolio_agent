# Opens the monitoring dashboard. Grafana's admin password is copied to your clipboard (never
# printed). The user name is "admin". Leave this running while you use Grafana; Ctrl+C closes it.
#
#     .\scripts\grafana.ps1
$b64 = kubectl get secret grafana-admin -o jsonpath="{.data.password}"
if (-not $b64) { Write-Host "Couldn't read the Grafana password. Is the cluster running?" -ForegroundColor Red; exit 1 }
[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($b64)) | Set-Clipboard
Write-Host "Grafana password copied to your clipboard. Sign in as 'admin' and paste it with Ctrl+V." -ForegroundColor Green
Write-Host "Opening http://localhost:3000 ..."
Start-Process "http://localhost:3000"
kubectl port-forward svc/grafana 3000:3000
