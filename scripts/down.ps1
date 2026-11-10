# Tear everything down: the platform first, then the cluster.
# WARNING: this deletes the database too. Back it up first if you want to keep your plays.
# Run from the project folder:  .\scripts\down.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

foreach ($layer in "platform", "cluster") {
    Write-Host "`n=== $layer ===" -ForegroundColor Cyan
    Push-Location (Join-Path $root "terraform\$layer")
    try {
        terraform init -input=false
        terraform destroy -auto-approve
        if ($LASTEXITCODE -ne 0) { throw "terraform destroy failed in $layer" }
    } finally {
        Pop-Location
    }
}
Write-Host "`nEverything removed." -ForegroundColor Green
