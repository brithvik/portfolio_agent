# Sends a test alert through the real path: Alertmanager -> notify proxy -> Discord.
# The message arrives in about 30 seconds, and a "Resolved" message follows about 5 minutes
# later. Needs DISCORD_WEBHOOK_URL in .env (then run up.ps1).
#
#     .\scripts\notify-test.ps1
kubectl exec deploy/alertmanager -- amtool alert add TestNotification severity=info `
    '--annotation=summary=Test alert from the portfolio agent. If you can read this, notifications work.' `
    --alertmanager.url=http://localhost:9093
if ($LASTEXITCODE -ne 0) { Write-Host "Couldn't reach Alertmanager. Check: kubectl get pods -l app=alertmanager" -ForegroundColor Red; exit 1 }
Write-Host "Test alert sent. Check your Discord channel in about 30 seconds." -ForegroundColor Green
Write-Host "Nothing arrived? Look at: kubectl logs deploy/alertmanager --tail=20   and   kubectl logs deploy/notify-proxy --tail=20"
