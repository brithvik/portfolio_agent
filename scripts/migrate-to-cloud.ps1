# Moves the database from the laptop cluster to the AWS cluster, so the ledger, ideas and
# history carry on in the cloud instead of starting over. Run it once, right after cloud-up.ps1.
#
#     .\scripts\migrate-to-cloud.ps1
#
# While it runs, every scheduled job in the cloud is paused, and the cloud's (nearly empty)
# database is replaced by the laptop's. Afterwards, stop the laptop cluster so the two don't
# both run the daily jobs: docker stop portfolio-control-plane   (start it again any time).
$ErrorActionPreference = "Stop"
. "$PSScriptRoot\cloud-common.ps1"
$laptop = kubectl config get-contexts -o name | Where-Object { $_ -like "kind-*" } | Select-Object -First 1
if (-not $laptop) { Fail "No laptop (kind) cluster found in your kubeconfig." }
if (-not (Tunnel-Open)) { Fail "The tunnel to the cloud isn't open. Run: .\scripts\cloud-tunnel.ps1" }
$file = "migrate.sql.gz"

Write-Host "1/5 Dumping the laptop database..."
kubectl --context $laptop exec postgres-0 -- sh -c 'pg_dump -U portfolio -d portfolio --no-owner | gzip > /tmp/migrate.sql.gz'
kubectl --context $laptop cp postgres-0:/tmp/migrate.sql.gz $file
if (-not (Test-Path $file) -or (Get-Item $file).Length -lt 1000) { Fail "The dump looks empty. Nothing in the cloud was changed." }

Write-Host "2/5 Pausing everything in the cloud that writes to the database..."
$cronjobs = kubectl --context $cloudContext get cronjobs -o name
$pause = Write-TempJson @{ spec = @{ suspend = $true } }
$resume = Write-TempJson @{ spec = @{ suspend = $false } }
foreach ($c in $cronjobs) { kubectl --context $cloudContext patch $c --type merge --patch-file $pause | Out-Null }
$writers = "deployment/gateway", "deployment/notifier", "deployment/metrics-exporter"
foreach ($d in $writers) { kubectl --context $cloudContext scale $d --replicas=0 | Out-Null }
kubectl --context $cloudContext delete jobs --all --wait=true | Out-Null

try {
    Write-Host "3/5 Copying the dump to the cloud and replacing the cloud database..."
    kubectl --context $cloudContext cp $file postgres-0:/tmp/migrate.sql.gz
    kubectl --context $cloudContext exec postgres-0 -- sh -c 'psql -U portfolio -d postgres -v ON_ERROR_STOP=1 -c "DROP DATABASE portfolio WITH (FORCE)" -c "CREATE DATABASE portfolio OWNER portfolio" && gunzip -c /tmp/migrate.sql.gz | psql -U portfolio -d portfolio -q -v ON_ERROR_STOP=1 > /dev/null && rm /tmp/migrate.sql.gz'
    if ($LASTEXITCODE -ne 0) { throw "The restore failed. The cloud database may be empty; run this script again." }

    Write-Host "4/5 Checking the copy..."
    $query = "SELECT (SELECT count(*) FROM ideas) || ' ideas, ' || (SELECT count(*) FROM daily_bars) || ' price rows, ' || (SELECT count(*) FROM agent_runs) || ' agent runs'"
    $before = kubectl --context $laptop exec postgres-0 -- psql -U portfolio -d portfolio -Atc $query
    $after = kubectl --context $cloudContext exec postgres-0 -- psql -U portfolio -d portfolio -Atc $query
    Write-Host "   laptop: $before"
    Write-Host "   cloud:  $after"
    if ($before -ne $after) { throw "The counts differ, so the copy is incomplete." }
} finally {
    Write-Host "5/5 Resuming the cloud's jobs..."
    foreach ($c in $cronjobs) { kubectl --context $cloudContext patch $c --type merge --patch-file $resume | Out-Null }
    foreach ($d in $writers) { kubectl --context $cloudContext scale $d --replicas=1 | Out-Null }
    Remove-Item $file -ErrorAction SilentlyContinue
    kubectl --context $laptop exec postgres-0 -- rm -f /tmp/migrate.sql.gz | Out-Null
}
Write-Host "`nMigrated. Now stop the laptop cluster so only the cloud runs the daily jobs:" -ForegroundColor Green
Write-Host "    docker stop portfolio-control-plane"
