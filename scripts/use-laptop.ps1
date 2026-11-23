# Points kubectl (and every script) at the kind cluster on this laptop.
#     .\scripts\use-laptop.ps1
$context = kubectl config get-contexts -o name | Where-Object { $_ -like "kind-*" } | Select-Object -First 1
if (-not $context) { Write-Host "No kind cluster in your kubeconfig. Run .\scripts\up.ps1" -ForegroundColor Red; exit 1 }
kubectl config use-context $context
