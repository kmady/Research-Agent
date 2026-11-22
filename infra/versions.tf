# Versions exigées et configuration du provider AWS.
terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.73"
    }
  }
}

provider "aws" {
  region = var.region

  # Étiquettes ajoutées automatiquement à chaque ressource : on retrouve tout le projet
  # dans la console et dans la facture (Cost Explorer, filtre par étiquette).
  default_tags {
    tags = {
      Project     = var.project
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}
