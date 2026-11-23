# The idea ledger (see ledger.py): every idea recorded once, then judged on what happened next.
#
#     .\scripts\ledger.ps1            # label new events, record today's ideas, show the review and scoreboard
#     .\scripts\ledger.ps1 record     # record ideas from the labels already stored (no model calls)
#     .\scripts\ledger.ps1 report     # the scoreboard only
param([Parameter(Position = 0)][ValidateSet("daily", "record", "report")][string]$Mode = "daily")
& "$PSScriptRoot\agent-job.ps1" -Prefix "ledger" -Command @("python", "ledger.py", $Mode) -DeadlineSeconds 1800
