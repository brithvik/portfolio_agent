# Score the agent's accuracy (see evaluate.py). It asks the real model a fixed set of questions
# and checks each answer with plain code: are the stated facts right, and is every number
# backed by the data the agent saw? Read-only: the agent cannot propose trades here.
#
#     .\scripts\evaluate.ps1                      # every question (takes several minutes)
#     .\scripts\evaluate.ps1 sector_exposure      # one question
#     .\scripts\evaluate.ps1 -ShowAnswers         # also print the agent's full replies
#     .\scripts\evaluate.ps1 -Run 2               # re-check the numbers in a past conversation
param([Parameter(Position = 0)][string]$Question = "", [int]$Run = 0, [switch]$ShowAnswers)

$command = @("python", "evaluate.py")
if ($Run -gt 0) { $command += @("--run", "$Run") }
if ($Question) { $command += @("--question", $Question) }
if ($ShowAnswers) { $command += "--show-answers" }
& "$PSScriptRoot\agent-job.ps1" -Prefix "evaluate" -Command $command -DeadlineSeconds 1800
