# Opens the approval page. The approver token is copied to your clipboard (never printed), so
# you can paste it into the page. Leave this running while you use the page; Ctrl+C closes it.
#
#     .\scripts\gateway.ps1
$b64 = kubectl get secret gateway-approver-token -o jsonpath="{.data.token}"
if (-not $b64) { Write-Host "Couldn't read the approver token. Is the cluster running?" -ForegroundColor Red; exit 1 }
[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($b64)) | Set-Clipboard
Write-Host "Approver token copied to your clipboard. Paste it into the page with Ctrl+V." -ForegroundColor Green
Write-Host "Opening http://localhost:8080 ..."
Start-Process "http://localhost:8080"
kubectl port-forward svc/gateway 8080:8080
