# Versions exigées : Terraform lui-même et le provider AWS (le plugin qui parle à l'API AWS).
terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.73" # toute version 6.x à partir de 6.73, jamais la 7 (changements cassants)
    }
  }
}

# Configuration du provider : la région, et des étiquettes ajoutées à chaque ressource créée.
provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = "research-agent"
      ManagedBy = "terraform"
      Stack     = "bootstrap"
    }
  }
}
