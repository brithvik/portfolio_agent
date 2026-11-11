# Ask the agent a question. It runs inside the cluster with exactly an agent's access
# (see agent-job.ps1): it can read your data and FILE trade proposals, and nothing more.
#
#     .\scripts\ask.ps1 "How concentrated is my portfolio?"
#     .\scripts\ask.ps1 "Propose trimming NVDA by 1500 dollars to reduce concentration."
param([Parameter(Mandatory = $true, Position = 0)][string]$Question)

# Base64 keeps quotes and special characters in the question from breaking the YAML
$b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Question))
& "$PSScriptRoot\agent-job.ps1" -Prefix "ask" -Command "python", "agent.py", "ask-env" -QuestionB64 $b64 -WithGateway
