# Proves the network rules from the inside: starts a pod with an agent's exact network
# position and tries the connections for real (see netcheck.py).
#
#     .\scripts\netcheck.ps1
& "$PSScriptRoot\agent-job.ps1" -Prefix "netcheck" -Command "python", "netcheck.py" -DeadlineSeconds 180
