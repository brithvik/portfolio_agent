# Build everything: the cluster first, then the platform on top of it.
# Run from the project folder:  .\scripts\up.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

foreach ($layer in "cluster", "platform") {
    Write-Host "`n=== $layer ===" -ForegroundColor Cyan
    Push-Location (Join-Path $root "terraform\$layer")
    try {
        terraform init -input=false
        if ($layer -eq "platform") { terraform workspace select default | Out-Null }   # the laptop, never the cloud
        if ($LASTEXITCODE -ne 0) { throw "terraform init failed in $layer" }
        terraform apply -auto-approve
        if ($LASTEXITCODE -ne 0) { throw "terraform apply failed in $layer" }
    } finally {
        Pop-Location
    }
}
& "$PSScriptRoot\use-laptop.ps1" | Out-Null
Write-Host "`nAll up, and kubectl points at the laptop cluster. Run: kubectl get pods" -ForegroundColor Green
