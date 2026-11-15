# Takes a database backup right now and shows the result. Each backup is restore-tested
# inside the job before it is kept (see k8s/backup.yaml). One also runs every evening.
#
#     .\scripts\backup.ps1
$name = "backup-now-" + (Get-Date -Format "HHmmss")
kubectl create job --from=cronjob/backup $name
if ($LASTEXITCODE -ne 0) { Write-Host "Couldn't start the backup. Is the cluster running?" -ForegroundColor Red; exit 1 }
Write-Host "Backing up and restore-testing (usually under a minute)..."
kubectl wait --for=condition=complete --timeout=300s "job/$name" | Out-Null
$ok = $LASTEXITCODE -eq 0
kubectl logs "job/$name"
if ($ok) { Write-Host "`nBackup verified." -ForegroundColor Green }
else { Write-Host "`nThe backup did not finish. See the log above, or: kubectl describe job/$name" -ForegroundColor Red; exit 1 }
