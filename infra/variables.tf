# Entrées de l'infrastructure. Les valeurs propres à un environnement sont dans envs/<env>.tfvars.

variable "region" {
  description = "Région AWS"
  type        = string
  default     = "ca-central-1"
}

variable "project" {
  description = "Nom du projet, utilisé comme préfixe des ressources"
  type        = string
  default     = "research-agent"
}

variable "environment" {
  description = "Nom de l'environnement"
  type        = string
  # Pas de valeur par défaut : on doit la donner explicitement (envs/prod.tfvars).

  validation {
    condition     = contains(["prod"], var.environment)
    error_message = "Seul l'environnement « prod » existe pour l'instant."
  }
}

variable "domain_name" {
  description = "Sous-domaine du service, délégué à Route 53 (ex. research.madysystems.ca)"
  type        = string
}

variable "vpc_cidr" {
  description = "Plage d'adresses du VPC"
  type        = string
  default     = "10.0.0.0/16"
}

variable "availability_zones" {
  description = "Zones utilisées ; la première accueille l'instance GPU et la NAT Gateway"
  type        = list(string)

  validation {
    condition     = length(var.availability_zones) >= 2
    error_message = "Le load balancer exige au moins deux zones."
  }
}

variable "github_repository" {
  description = "Dépôt GitHub autorisé à déployer (propriétaire/nom)"
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", var.github_repository))
    error_message = "Format attendu : propriétaire/nom."
  }
}

variable "instance_type" {
  description = "Type d'instance GPU"
  type        = string
  default     = "g4dn.xlarge" # T4 16 Go ; g6.xlarge (L4 24 Go) est plus rapide et plus chère

  validation {
    condition     = contains(["g4dn.xlarge", "g6.xlarge"], var.instance_type)
    error_message = "Types prévus : g4dn.xlarge ou g6.xlarge (quota GPU : 4 vCPU)."
  }
}

variable "data_volume_size" {
  description = "Taille du volume /data, en Go"
  type        = number
  default     = 100
}

variable "schedule_timezone" {
  description = "Fuseau horaire des démarrages et arrêts planifiés"
  type        = string
  default     = "America/Toronto"
}

variable "alert_email" {
  description = "Adresse qui reçoit les alertes. Donnée dans envs/prod.local.tfvars (non versionné)."
  type        = string
  sensitive   = true # masquée dans les sorties de plan et d'apply

  validation {
    condition     = can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", var.alert_email))
    error_message = "Adresse courriel invalide."
  }
}

variable "github_owner_id" {
  description = "Identifiant numérique du propriétaire du dépôt GitHub (immuable)"
  type        = string
}

variable "github_repository_id" {
  description = "Identifiant numérique du dépôt GitHub (immuable)"
  type        = string
}
