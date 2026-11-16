# Labels the news behind today's rising candidates with the model, then prints the review:
# which candidates stay on the shortlist, which are set aside (for example, being acquired),
# and the flags the rules attach (insider selling, strong results, and so on).
#
#     .\scripts\classify.ps1            # label new events, then show the review
#     .\scripts\classify.ps1 review     # show the review only (no model calls)
#     .\scripts\classify.ps1 eval       # score the classifier against the hand-labeled headlines
param([Parameter(Position = 0)][ValidateSet("run", "review", "eval")][string]$Mode = "run")
& "$PSScriptRoot\agent-job.ps1" -Prefix "classify" -Command @("python", "classify.py", $Mode) -DeadlineSeconds 1800
