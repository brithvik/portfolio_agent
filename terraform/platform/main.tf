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
  default     = "portfolio-agent:v6"
}

# ---------- Connect to the cluster built by layer 1 ----------
data "terraform_remote_state" "cluster" {
  backend = "local"
  config = {
    path = "${path.module}/../cluster/terraform.tfstate"
  }
}

locals {
  cluster = data.terraform_remote_state.cluster.outputs

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

provider "kubernetes" {
  host                   = local.cluster.endpoint
  client_certificate     = local.cluster.client_certificate
  client_key             = local.cluster.client_key
  cluster_ca_certificate = local.cluster.cluster_ca_certificate
}

provider "kubectl" {
  host                   = local.cluster.endpoint
  client_certificate     = local.cluster.client_certificate
  client_key             = local.cluster.client_key
  cluster_ca_certificate = local.cluster.cluster_ca_certificate
  load_config_file       = false
}

# ---------- The app image ----------
# kind can't pull images from your laptop on its own, so copy it in.
resource "terraform_data" "load_image" {
  triggers_replace = [var.app_image, local.cluster.endpoint]

  provisioner "local-exec" {
    command = "kind load docker-image ${var.app_image} --name ${local.cluster.cluster_name}"
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

# ---------- The app: every YAML file in the k8s folder ----------
data "kubectl_path_documents" "app" {
  pattern = "${path.module}/../../k8s/*.yaml"
  vars = {
    app_image = var.app_image # fills in ${app_image} inside the YAML files
  }
}

resource "kubectl_manifest" "app" {
  for_each         = data.kubectl_path_documents.app.manifests
  yaml_body        = each.value
  wait_for_rollout = false # check readiness with kubectl get pods

  depends_on = [
    terraform_data.load_image,
    kubernetes_secret_v1.postgres,
    kubernetes_secret_v1.alpaca,
    kubernetes_secret_v1.gemini,
    kubernetes_secret_v1.gateway_agent_token,
    kubernetes_secret_v1.gateway_approver_token,
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
  EOT
}
