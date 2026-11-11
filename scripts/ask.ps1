# Ask the agent a question. It runs as a one-off Job inside the cluster, with exactly the
# access an agent should have: the LLM key, the database, and a token that can only FILE trade
# proposals. It never gets the brokerage keys or the approver token.
#
#     .\scripts\ask.ps1 "How concentrated is my portfolio?"
#     .\scripts\ask.ps1 "Propose trimming NVDA by 1500 dollars to reduce concentration."
param([Parameter(Mandatory = $true, Position = 0)][string]$Question)

$image = kubectl get cronjob briefing -o jsonpath="{.spec.jobTemplate.spec.template.spec.containers[0].image}"
if (-not $image) { Write-Host "Couldn't find the cluster. Is it running? Try .\scripts\up.ps1" -ForegroundColor Red; exit 1 }

# Base64 keeps quotes and special characters in the question from breaking the YAML
$b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Question))
$name = "ask-" + (Get-Date -Format "MMdd-HHmmss")

$yaml = @"
apiVersion: batch/v1
kind: Job
metadata:
  name: $name
spec:
  backoffLimit: 0
  activeDeadlineSeconds: 600
  ttlSecondsAfterFinished: 3600
  template:
    metadata:
      labels:
        app: agent
    spec:
      restartPolicy: Never
      automountServiceAccountToken: false
      securityContext:
        runAsNonRoot: true
        runAsUser: 1000
      containers:
        - name: agent
          image: $image
          imagePullPolicy: IfNotPresent
          command: ["python", "agent.py", "ask-env"]
          envFrom:
            - secretRef:
                name: gemini-credentials
          env:
            - name: QUESTION_B64
              value: "$b64"
            - name: DATABASE_URL
              valueFrom:
                secretKeyRef: { name: postgres-credentials, key: DATABASE_URL }
            - name: GATEWAY_URL
              value: "http://gateway:8080"
            - name: GATEWAY_AGENT_TOKEN
              valueFrom:
                secretKeyRef: { name: gateway-agent-token, key: token }
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities:
              drop: ["ALL"]
          resources:
            requests: { memory: 128Mi, cpu: 50m }
            limits: { memory: 384Mi }
"@

$yaml | kubectl apply -f - | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Host "Couldn't start the agent job." -ForegroundColor Red; exit 1 }

# Wait for the pod to start, then follow its output
for ($i = 0; $i -lt 90; $i++) {
    $phase = kubectl get pods -l job-name=$name -o jsonpath="{.items[0].status.phase}" 2>$null
    if ($phase -in "Running", "Succeeded", "Failed") { break }
    Start-Sleep 1
}
kubectl logs -f "job/$name"
