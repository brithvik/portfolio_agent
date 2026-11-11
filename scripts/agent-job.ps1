# Runs one command as a Job with exactly an agent's access, then follows its output.
# Used by ask.ps1, redteam.ps1, and netcheck.ps1, so all three run under the same rules:
#   - label app=agent, so the agent's network rules apply (no direct internet)
#   - the LLM key and the database address; internet only through the allowlisting proxy
#   - never the brokerage keys, never the approver token, no Kubernetes API access
#   - with -WithGateway: a token that can only FILE trade proposals
param(
    [Parameter(Mandatory = $true)][string]$Prefix,
    [Parameter(Mandatory = $true)][string[]]$Command,
    [string]$QuestionB64 = "",
    [switch]$WithGateway,
    [int]$DeadlineSeconds = 600
)

$image = kubectl get cronjob briefing -o jsonpath="{.spec.jobTemplate.spec.template.spec.containers[0].image}"
if (-not $image) { Write-Host "Couldn't find the cluster. Is it running? Try .\scripts\up.ps1" -ForegroundColor Red; exit 1 }

$name = "$Prefix-" + (Get-Date -Format "MMdd-HHmmss")
$commandJson = '["' + ($Command -join '", "') + '"]'
$gatewayEnv = ""
if ($WithGateway) {
    $gatewayEnv = @"

            - name: GATEWAY_URL
              value: "http://gateway:8080"
            - name: GATEWAY_AGENT_TOKEN
              valueFrom:
                secretKeyRef: { name: gateway-agent-token, key: token }
"@
}

$yaml = @"
apiVersion: batch/v1
kind: Job
metadata:
  name: $name
spec:
  backoffLimit: 0
  activeDeadlineSeconds: $DeadlineSeconds
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
          command: $commandJson
          envFrom:
            - secretRef:
                name: gemini-credentials
          env:
            - name: QUESTION_B64
              value: "$QuestionB64"
            - name: DATABASE_URL
              valueFrom:
                secretKeyRef: { name: postgres-credentials, key: DATABASE_URL }
            - name: HTTPS_PROXY
              value: "http://egress-proxy:3128"
            - name: NO_PROXY
              value: "postgres,gateway,localhost,127.0.0.1,.svc,.cluster.local"$gatewayEnv
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
if ($LASTEXITCODE -ne 0) { Write-Host "Couldn't start the job." -ForegroundColor Red; exit 1 }

# Wait for the pod to start, then follow its output
for ($i = 0; $i -lt 90; $i++) {
    $phase = kubectl get pods -l job-name=$name -o jsonpath="{.items[0].status.phase}" 2>$null
    if ($phase -in "Running", "Succeeded", "Failed") { break }
    Start-Sleep 1
}
kubectl logs -f "job/$name"
