# Runs the daily market-data job right now and follows its output: refreshes the universe of
# tradable stocks, updates prices, and prints today's rising candidates.
# The very first run loads about a year of prices for a few thousand stocks: allow ~10 minutes.
#
#     .\scripts\market.ps1
$name = "market-now-" + (Get-Date -Format "MMdd-HHmmss")
kubectl create job --from=cronjob/market-data $name
if ($LASTEXITCODE -ne 0) { Write-Host "Couldn't start the job. Is the cluster running?" -ForegroundColor Red; exit 1 }
Write-Host "Waiting for the job to start..."
kubectl wait --for=condition=ready pod -l "job-name=$name" --timeout=120s | Out-Null
kubectl logs -f "job/$name"
