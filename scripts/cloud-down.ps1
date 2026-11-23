# Deletes everything on AWS: the platform, the server, the network and the backup bucket.
# It first copies every backup in the bucket to a "cloud-backups" folder here, so nothing is lost.
#
#     .\scripts\cloud-down.ps1
$ErrorActionPreference = "Stop"
. "$PSScriptRoot\cloud-common.ps1"
Require-Tools
$bucket = Server-Output "backup_bucket"
$region = Server-Output "region"

$target = Join-Path $root "cloud-backups"
Write-Host "Downloading backups from s3://$bucket to $target ..."
aws s3 sync "s3://$bucket/postgres/" $target --region $region --only-show-errors
if ($LASTEXITCODE -ne 0) { Fail "Couldn't download the backups, so nothing was deleted." }
$count = (Get-ChildItem $target -Filter "*.sql.gz" -ErrorAction SilentlyContinue).Count
Write-Host "$count backups saved in $target." -ForegroundColor Green
$answer = Read-Host "Type DELETE to remove everything on AWS"
if ($answer -ne "DELETE") { Write-Host "Nothing was deleted."; exit 0 }

aws s3 rm "s3://$bucket" --recursive --region $region --only-show-errors
Push-Location (Join-Path $root "terraform\platform")
try {
    terraform workspace select aws 2>$null
    if ($LASTEXITCODE -eq 0) {
        # The server is about to disappear, so the platform's state is simply discarded
        terraform workspace select default | Out-Null
        terraform workspace delete -force aws
    }
} finally { Pop-Location }
Export-AwsCredentials
Push-Location $awsDir
try {
    terraform destroy -auto-approve
    if ($LASTEXITCODE -ne 0) { throw "terraform destroy failed" }
} finally { Pop-Location }
kubectl config delete-context $cloudContext 2>$null | Out-Null
Write-Host "Everything on AWS is deleted." -ForegroundColor Green
