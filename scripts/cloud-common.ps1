# Shared by the cloud-*.ps1 scripts. Dot-source it:  . "$PSScriptRoot\cloud-common.ps1"
$root = Split-Path -Parent $PSScriptRoot
$awsDir = Join-Path $root "terraform\aws"
$cloudContext = "portfolio-aws"
$tunnelPort = 16443          # on your laptop; the tunnel carries it to the Kubernetes API on the server

function Fail($message) { Write-Host $message -ForegroundColor Red; exit 1 }

function Require-Tools {
    if (-not (Get-Command aws -ErrorAction SilentlyContinue)) {
        Fail "The AWS command line isn't installed. Install it: winget install Amazon.AWSCLI  (then open a new terminal)"
    }
    if (-not (Get-Command session-manager-plugin -ErrorAction SilentlyContinue)) {
        Fail "The Session Manager plugin isn't installed. Install it: winget install Amazon.SessionManagerPlugin  (then open a new terminal)"
    }
    aws sts get-caller-identity --query Account --output text 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail "The AWS command line isn't signed in. Run: aws login" }
}

function Export-AwsCredentials {
    # Terraform's AWS provider can't read 'aws login' sessions itself, so the AWS CLI hands it
    # the current short-lived credentials as environment variables, for this script run only.
    $lines = aws configure export-credentials --format powershell 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $lines) { Fail "Couldn't get credentials from the AWS CLI. Sign in again: aws login" }
    $lines | Invoke-Expression
}

function Server-Output($name) {
    Push-Location $awsDir
    try { $value = terraform output -raw $name 2>$null } finally { Pop-Location }
    if ($LASTEXITCODE -ne 0 -or -not $value) { Fail "The AWS server doesn't exist yet. Run: .\scripts\cloud-up.ps1" }
    return $value
}

function Tunnel-Open {
    $client = New-Object Net.Sockets.TcpClient
    try { $client.Connect("127.0.0.1", $tunnelPort); return $true } catch { return $false } finally { $client.Close() }
}

function Write-TempJson($object) {
    # A JSON file for kubectl's --patch-file, which avoids PowerShell's quoting rules
    $path = Join-Path ([IO.Path]::GetTempPath()) ("patch-" + [Guid]::NewGuid() + ".json")
    $object | ConvertTo-Json -Compress -Depth 5 | Set-Content -Encoding ascii $path
    return $path
}

function Write-Json($object) {
    # The AWS command line reads parameters from a file, which avoids PowerShell's quoting rules
    $path = Join-Path ([IO.Path]::GetTempPath()) ("aws-params-" + [Guid]::NewGuid() + ".json")
    $object | ConvertTo-Json -Compress | Set-Content -Encoding ascii $path
    return "file://$path"
}
