# Writes a random sample of real news events to events_sample.txt (id|symbol|articles|headline),
# for building the classifier's labeled test set. Headlines are public news: safe to share.
#
#     .\scripts\export-events.ps1
kubectl exec postgres-0 -- psql -U portfolio -d portfolio -At -F "|" -c "SELECT e.id, e.symbol, e.articles, e.headline FROM events e WHERE e.last_seen > now() - interval '90 days' ORDER BY random() LIMIT 300" | Out-File -Encoding utf8 events_sample.txt
Write-Host "Wrote events_sample.txt"
