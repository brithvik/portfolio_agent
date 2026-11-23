# Builds the AWS server (terraform/aws) and deploys the platform onto it (terraform/platform,
# in its own "aws" workspace, so the laptop's state is untouched).
#
#     .\scripts\cloud-up.ps1                  # deploys the image CI published for your current commit
#     .\scripts\cloud-up.ps1 -Image ghcr.io/you/portfolio-agent:sha-1234567
#
# Before the first run: install the AWS CLI and Session Manager plugin, sign in (aws login),
# and copy terraform\aws\terraform.tfvars.example to terraform.tfvars with your email in it.
param([string]$Image = "")
$ErrorActionPreference = "Stop"
. "$PSScriptRoot\cloud-common.ps1"
Require-Tools
if (-not (Test-Path (Join-Path $awsDir "terraform.tfvars"))) {
    Fail "Copy terraform\aws\terraform.tfvars.example to terraform\aws\terraform.tfvars and put your email in it."
}

# ---------- Which image: the one CI built and published for this commit ----------
if (-not $Image) {
    if (git status --porcelain) { Write-Host "Note: you have uncommitted changes. The cloud runs the last pushed commit, without them." -ForegroundColor Yellow }
    $remote = git remote get-url origin
    if ($remote -notmatch "github\.com[:/]([^/]+)/") { Fail "Couldn't read the GitHub owner from: $remote. Pass -Image instead." }
    $owner = $Matches[1].ToLower()
    $commit = git rev-parse --short=7 HEAD
    $Image = "ghcr.io/$owner/portfolio-agent:sha-$commit"
    $pushed = git branch -r --contains HEAD 2>$null
    if (-not $pushed) { Fail "This commit hasn't been pushed, so CI hasn't published an image for it. Run: git push, wait for CI, then try again." }
}
Write-Host "Image: $Image" -ForegroundColor Cyan
docker manifest inspect $Image *> $null
if ($LASTEXITCODE -ne 0) {
    Fail ("Can't find $Image. Either CI hasn't finished publishing it (check the Actions tab), or the package is private: " +
          "make it public on GitHub (Packages > portfolio-agent > Package settings), or add GHCR_USER and GHCR_PULL_TOKEN to .env.")
}

# ---------- 1. The server ----------
Write-Host "`n=== AWS server ===" -ForegroundColor Cyan
Export-AwsCredentials
Push-Location $awsDir
try {
    terraform init -input=false
    if ($LASTEXITCODE -ne 0) { throw "terraform init failed" }
    terraform apply -auto-approve
    if ($LASTEXITCODE -ne 0) { throw "terraform apply failed" }
} finally { Pop-Location }
$instance = Server-Output "instance_id"
$region = Server-Output "region"

# ---------- 2. Wait for it to boot and for k3s to come up ----------
Write-Host "`nWaiting for the server to check in with Systems Manager (a few minutes on first boot)..."
$deadline = (Get-Date).AddMinutes(15)
do {
    Start-Sleep 10
    $ping = aws ssm describe-instance-information --region $region --filters "Key=InstanceIds,Values=$instance" --query "InstanceInformationList[0].PingStatus" --output text 2>$null
} until ($ping -eq "Online" -or (Get-Date) -gt $deadline)
if ($ping -ne "Online") { Fail "The server didn't check in within 15 minutes. Look at its console log in the EC2 console (Actions > Monitor > Get system log)." }

Write-Host "Fetching the cluster's credentials (waits for k3s to finish installing)..."
$params = Write-Json @{ commands = @("for i in `$(seq 1 60); do test -s /etc/rancher/k3s/k3s.yaml && break; sleep 5; done; cat /etc/rancher/k3s/k3s.yaml") }
$commandId = aws ssm send-command --region $region --instance-ids $instance --document-name AWS-RunShellScript --parameters $params --query "Command.CommandId" --output text
do {
    Start-Sleep 5
    $status = aws ssm get-command-invocation --region $region --command-id $commandId --instance-id $instance --query Status --output text 2>$null
} while ($status -in @("Pending", "InProgress", "Delayed", $null, ""))
$k3s = aws ssm get-command-invocation --region $region --command-id $commandId --instance-id $instance --query StandardOutputContent --output text
$fields = @{}
foreach ($key in "certificate-authority-data", "client-certificate-data", "client-key-data") {
    if (($k3s -join "`n") -notmatch "$key`:\s*(\S+)") { Fail "k3s isn't ready yet ($status). Wait a minute and run cloud-up.ps1 again." }
    $fields[$key] = $Matches[1]
}

# ---------- 3. Add the cloud cluster to your kubeconfig as "portfolio-aws" ----------
$kubeDir = Join-Path $HOME ".kube"
New-Item -ItemType Directory -Force $kubeDir | Out-Null
$cloudConfig = Join-Path $kubeDir "portfolio-aws.yaml"
@"
apiVersion: v1
kind: Config
clusters:
  - name: $cloudContext
    cluster:
      server: https://127.0.0.1:$tunnelPort
      certificate-authority-data: $($fields["certificate-authority-data"])
users:
  - name: $cloudContext
    user:
      client-certificate-data: $($fields["client-certificate-data"])
      client-key-data: $($fields["client-key-data"])
contexts:
  - name: $cloudContext
    context: { cluster: $cloudContext, user: $cloudContext }
current-context: $cloudContext
"@ | Set-Content -Encoding ascii $cloudConfig
$mainConfig = Join-Path $kubeDir "config"
if (Test-Path $mainConfig) {
    $env:KUBECONFIG = "$cloudConfig;$mainConfig"      # the new credentials win over any old copy
    kubectl config view --flatten --raw | Set-Content -Encoding ascii "$mainConfig.new"
    Remove-Item Env:KUBECONFIG
    Move-Item -Force "$mainConfig.new" $mainConfig
} else {
    Copy-Item $cloudConfig $mainConfig
}

# ---------- 4. Open the tunnel (in its own window) ----------
if (-not (Tunnel-Open)) {
    Start-Process powershell -ArgumentList "-NoExit", "-File", (Join-Path $PSScriptRoot "cloud-tunnel.ps1")
    $deadline = (Get-Date).AddMinutes(2)
    while (-not (Tunnel-Open) -and (Get-Date) -lt $deadline) { Start-Sleep 2 }
    if (-not (Tunnel-Open)) { Fail "The tunnel didn't open. Look at the tunnel window for the error." }
}
kubectl config use-context $cloudContext | Out-Null
kubectl get nodes

# ---------- 5. The platform, in its own workspace ----------
Write-Host "`n=== platform (aws workspace) ===" -ForegroundColor Cyan
Push-Location (Join-Path $root "terraform\platform")
try {
    terraform init -input=false
    terraform workspace select -or-create aws
    if ($LASTEXITCODE -ne 0) { throw "couldn't select the aws workspace" }
    terraform apply -auto-approve -var "kube_context=$cloudContext" -var "app_image=$Image" -var "grafana_replicas=0"
    if ($LASTEXITCODE -ne 0) { throw "terraform apply failed" }
} finally {
    terraform workspace select default | Out-Null      # so up.ps1 keeps deploying to the laptop
    Pop-Location
}
Write-Host "`nCloud is up, and kubectl now points at it (context $cloudContext)." -ForegroundColor Green
Write-Host "Keep the tunnel window open while you work. Every script (ask, netcheck, market, ledger...) now runs against the cloud."
Write-Host "Switch back to the laptop cluster with: .\scripts\use-laptop.ps1"
