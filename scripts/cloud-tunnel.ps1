# Opens an encrypted tunnel from this laptop to the AWS server's Kubernetes API, through AWS
# Systems Manager. The server has no open ports; this tunnel is the only way in, and it needs
# your AWS login. Leave this window open while you work with the cloud cluster.
#
#     .\scripts\cloud-tunnel.ps1
. "$PSScriptRoot\cloud-common.ps1"
Require-Tools
$instance = Server-Output "instance_id"
$region = Server-Output "region"
if (Tunnel-Open) { Fail "Something is already listening on port $tunnelPort (probably the tunnel is already open)." }
Write-Host "Tunnel open: localhost:$tunnelPort -> the server's Kubernetes API. Leave this window open; Ctrl+C closes it." -ForegroundColor Green
$params = Write-Json @{ portNumber = @("6443"); localPortNumber = @("$tunnelPort") }
aws ssm start-session --region $region --target $instance --document-name AWS-StartPortForwardingSession --parameters $params
