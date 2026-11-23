# Layer 2: everything that runs on the cluster: secrets, Postgres, and the collectors.
terraform {
  required_version = ">= 1.6"
  required_providers {
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.35"
    }
    kubectl = {
      source  = "alekc/kubectl"
      version = "~> 2.1"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

variable "app_image" {
  description = "The app image. The only place the version is set."
  type        = string
  default     = "portfolio-agent:v20"
}

variable "kube_context" {
  description = <<-EOT
    Empty: deploy to the kind cluster on this laptop (layer 1 in terraform/cluster).
    A context name (cloud-up.ps1 passes "portfolio-aws"): deploy to that cluster from ~/.kube/config.
  EOT
  type        = string
  default     = ""
}

variable "grafana_replicas" {
  description = "1 runs Grafana all the time. The 2 GB cloud server runs it only on demand (grafana.ps1 -Cloud)."
  type        = number
  default     = 1
}

# ---------- Connect to the cluster built by layer 1 ----------
data "terraform_remote_state" "cluster" {
  count   = var.kube_context == "" ? 1 : 0
  backend = "local"
  config = {
    path = "${path.module}/../cluster/terraform.tfstate"
  }
}

locals {
  on_kind = var.kube_context == ""
  # try(): in the cloud there is no kind state to read, and these are simply unused
  cluster = {
    for key in ["endpoint", "client_certificate", "client_key", "cluster_ca_certificate", "cluster_name"] :
    key => try(data.terraform_remote_state.cluster[0].outputs[key], null)
  }
  kubeconfig = pathexpand("~/.kube/config")

  # Read the Alpaca keys from your .env file, so secrets live in one place.
  env_lines = [
    for line in split("\n", replace(file("${path.module}/../../.env"), "\r", "")) :
    trimspace(line) if trimspace(line) != "" && !startswith(trimspace(line), "#")
  ]
  env = {
    for line in local.env_lines :
    split("=", line)[0] => join("=", slice(split("=", line), 1, length(split("=", line))))
  }
}

# On kind, connect with the certificates layer 1 produced; in the cloud, with a kubeconfig
# context (the API is reached through the Systems Manager tunnel, see cloud-tunnel.ps1).
provider "kubernetes" {
  host                   = local.on_kind ? local.cluster.endpoint : null
  client_certificate     = local.on_kind ? local.cluster.client_certificate : null
  client_key             = local.on_kind ? local.cluster.client_key : null
  cluster_ca_certificate = local.on_kind ? local.cluster.cluster_ca_certificate : null
  config_path            = local.on_kind ? null : local.kubeconfig
  config_context         = local.on_kind ? null : var.kube_context
}

provider "kubectl" {
  host                   = local.on_kind ? local.cluster.endpoint : null
  client_certificate     = local.on_kind ? local.cluster.client_certificate : null
  client_key             = local.on_kind ? local.cluster.client_key : null
  cluster_ca_certificate = local.on_kind ? local.cluster.cluster_ca_certificate : null
  load_config_file       = !local.on_kind
  config_path            = local.on_kind ? null : local.kubeconfig
  config_context         = local.on_kind ? null : var.kube_context
}

# ---------- The app image ----------
# kind can't pull images from your laptop on its own, so copy it in. (In the cloud the image
# comes from GitHub's container registry, published by CI for both Intel and ARM.)
resource "terraform_data" "load_image" {
  count            = local.on_kind ? 1 : 0
  triggers_replace = [var.app_image, local.cluster.endpoint]

  provisioner "local-exec" {
    command = "kind load docker-image ${var.app_image} --name ${local.cluster.cluster_name}"
  }
}

# ---------- Pulling a private image (optional) ----------
# If the image on GitHub's registry is private, put a token that can read packages in .env as
# GHCR_PULL_TOKEN (plus GHCR_USER). Every pod then pulls with it, through the default service
# account. A public image needs neither.
locals {
  ghcr_token = trimspace(lookup(local.env, "GHCR_PULL_TOKEN", ""))
}

resource "kubernetes_secret_v1" "ghcr" {
  count = local.ghcr_token == "" ? 0 : 1
  metadata {
    name = "ghcr-pull"
  }
  type = "kubernetes.io/dockerconfigjson"
  data = {
    ".dockerconfigjson" = jsonencode({
      auths = {
        "ghcr.io" = {
          auth = base64encode("${lookup(local.env, "GHCR_USER", "token")}:${local.ghcr_token}")
        }
      }
    })
  }
}

resource "kubernetes_default_service_account_v1" "default" {
  count = local.ghcr_token == "" ? 0 : 1
  metadata {
    namespace = "default"
  }
  image_pull_secret {
    name = kubernetes_secret_v1.ghcr[0].metadata[0].name
  }
}

# ---------- Secrets ----------
resource "random_password" "postgres" {
  length  = 24
  special = false # keeps it safe to put inside a connection URL
}

resource "kubernetes_secret_v1" "postgres" {
  metadata {
    name = "postgres-credentials"
  }
  data = {
    POSTGRES_USER     = "portfolio"
    POSTGRES_PASSWORD = random_password.postgres.result
    POSTGRES_DB       = "portfolio"
    # Full address, so components in other namespaces (like KEDA, later) can reach it too
    DATABASE_URL = "postgresql://portfolio:${random_password.postgres.result}@postgres.default.svc.cluster.local:5432/portfolio?sslmode=disable"
  }
}

# Read-only access is all the collectors need. Later, only the broker gateway will be
# allowed to trade, and the agent will never see these keys at all.
resource "kubernetes_secret_v1" "alpaca" {
  metadata {
    name = "alpaca-credentials"
  }
  data = {
    ALPACA_API_KEY    = local.env["ALPACA_API_KEY"]
    ALPACA_SECRET_KEY = local.env["ALPACA_SECRET_KEY"]
    ALPACA_PAPER      = "true"
  }
}

# The agent's LLM key. Only the agent's pods get this secret, and the agent never gets
# alpaca-credentials, so neither secret is ever in the same place.
resource "kubernetes_secret_v1" "gemini" {
  metadata {
    name = "gemini-credentials"
  }
  data = {
    GEMINI_API_KEY        = local.env["GEMINI_API_KEY"]
    GEMINI_MODEL          = local.env["GEMINI_MODEL"]
    GEMINI_FALLBACK_MODEL = lookup(local.env, "GEMINI_FALLBACK_MODEL", "")
  }
}

# ---------- The broker gateway's two tokens ----------
# The agent token can only file proposals. The approver token can approve, reject, and use the
# kill switch. They are separate secrets on purpose: agent pods are given the first and never
# the second, so the agent cannot approve its own proposals.
resource "random_password" "gateway_agent" {
  length  = 40
  special = false
}

resource "random_password" "gateway_approver" {
  length  = 40
  special = false
}

resource "kubernetes_secret_v1" "gateway_agent_token" {
  metadata {
    name = "gateway-agent-token"
  }
  data = {
    token = random_password.gateway_agent.result
  }
}

resource "kubernetes_secret_v1" "gateway_approver_token" {
  metadata {
    name = "gateway-approver-token"
  }
  data = {
    token = random_password.gateway_approver.result
  }
}

# ---------- Monitoring ----------
# Grafana's admin login, generated here like every other password in the project
resource "random_password" "grafana_admin" {
  length  = 24
  special = false
}

resource "kubernetes_secret_v1" "grafana_admin" {
  metadata {
    name = "grafana-admin"
  }
  data = {
    user     = "admin"
    password = random_password.grafana_admin.result
  }
}

# Dashboards as code: the JSON in monitoring/dashboards (built by monitoring/build_dashboard.py)
# is deployed from Git. Changing the file and running up.ps1 updates the dashboard.
resource "kubernetes_config_map_v1" "grafana_dashboards" {
  metadata {
    name = "grafana-dashboards"
  }
  data = {
    for f in fileset("${path.module}/../../monitoring/dashboards", "*.json") :
    f => file("${path.module}/../../monitoring/dashboards/${f}")
  }
}

# ---------- Notifications (optional) ----------
# Add DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/... to .env to get alerts, daily
# briefings and trade proposals in a Discord channel. Without it everything still deploys and
# alerts simply stay on the dashboard.
locals {
  discord_webhook = trimspace(lookup(local.env, "DISCORD_WEBHOOK_URL", ""))
  discord_on      = local.discord_webhook != ""
}

resource "kubernetes_secret_v1" "discord_webhook" {
  metadata {
    name = "discord-webhook"
  }
  data = {
    webhook_url = local.discord_webhook
  }
}

# Alertmanager's config. The webhook itself is not in here: Alertmanager reads it from the
# secret above, mounted as a file, so the URL never appears in a ConfigMap.
resource "kubernetes_config_map_v1" "alertmanager" {
  metadata {
    name = "alertmanager-config"
  }
  data = {
    "alertmanager.yml" = local.discord_on ? local.alertmanager_discord : local.alertmanager_quiet
  }
}

locals {
  alertmanager_route = {
    group_by        = ["alertname"]
    group_wait      = "30s" # collect related alerts into one message
    group_interval  = "5m"
    repeat_interval = "12h" # remind twice a day while something stays broken
  }

  alertmanager_discord = yamlencode({
    route = merge(local.alertmanager_route, { receiver = "discord" })
    receivers = [{
      name = "discord"
      discord_configs = [{
        webhook_url_file = "/etc/alertmanager-secrets/webhook_url"
        send_resolved    = true
        title            = "{{ if eq .Status \"firing\" }}ALERT{{ else }}Resolved{{ end }}: {{ .CommonLabels.alertname }} ({{ .CommonLabels.severity }})"
        message          = "{{ range .Alerts }}{{ .Annotations.summary }}\n{{ end }}"
        http_config = {
          proxy_url = "http://notify-proxy:3128" # the only way out, and it only reaches discord.com
        }
      }]
    }]
  })

  # No webhook configured: alerts are still evaluated and shown on the dashboard, just not sent.
  alertmanager_quiet = yamlencode({
    route     = merge(local.alertmanager_route, { receiver = "dashboard-only" })
    receivers = [{ name = "dashboard-only" }]
  })
}

# ---------- The app: every YAML file in the k8s folder ----------
data "kubectl_path_documents" "app" {
  pattern = "${path.module}/../../k8s/*.yaml"
  vars = {
    app_image        = var.app_image # fills in ${app_image} inside the YAML files
    grafana_replicas = var.grafana_replicas
  }
}

resource "kubectl_manifest" "app" {
  for_each         = data.kubectl_path_documents.app.manifests
  yaml_body        = each.value
  wait_for_rollout = false # check readiness with kubectl get pods

  depends_on = [
    terraform_data.load_image,
    kubernetes_default_service_account_v1.default,
    kubernetes_secret_v1.postgres,
    kubernetes_secret_v1.alpaca,
    kubernetes_secret_v1.gemini,
    kubernetes_secret_v1.gateway_agent_token,
    kubernetes_secret_v1.gateway_approver_token,
    kubernetes_secret_v1.grafana_admin,
    kubernetes_config_map_v1.grafana_dashboards,
    kubernetes_secret_v1.discord_webhook,
    kubernetes_config_map_v1.alertmanager,
  ]
}

output "next_steps" {
  value = <<-EOT
    Platform is up. Check it with:   kubectl get pods
    Take a snapshot now with:        kubectl create job --from=cronjob/snapshot snapshot-test
    Get a briefing now with:         kubectl create job --from=cronjob/briefing briefing-now
    Ask the agent a question:        .\scripts\ask.ps1 "How concentrated am I?"
    Open the approval page:          .\scripts\gateway.ps1
    Prove the network rules:         .\scripts\netcheck.ps1
    Run the red-team suite:          .\scripts\redteam.ps1
    Score the agent's accuracy:      .\scripts\evaluate.ps1
    Open the monitoring dashboard:   .\scripts\grafana.ps1
    Back up the database now:        .\scripts\backup.ps1
    Send a test notification:        .\scripts\notify-test.ps1
    Update market data and screens:  .\scripts\market.ps1
    Label news and review candidates: .\scripts\classify.ps1
    Record ideas, show the scoreboard: .\scripts\ledger.ps1
  EOT
}
