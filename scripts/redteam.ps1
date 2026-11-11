# Attack the agent with poisoned news articles and score how it holds up (see redteam.py).
# It runs the real model against a throwaway copy of your portfolio. Trade proposals go to a
# recorder, not the real gateway, so nothing here can touch your account.
#
#     .\scripts\redteam.ps1                       # every case (takes several minutes)
#     .\scripts\redteam.ps1 fake_user_message     # one case
#     .\scripts\redteam.ps1 -ShowAnswers          # also print the agent's full replies
param([Parameter(Position = 0)][string]$Case = "", [switch]$ShowAnswers)

$command = @("python", "redteam.py")
if ($Case) { $command += @("--case", $Case) }
if ($ShowAnswers) { $command += "--show-answers" }
& "$PSScriptRoot\agent-job.ps1" -Prefix "redteam" -Command $command -DeadlineSeconds 1800
