# Les entrées du code. On peut les changer sans toucher au reste (-var, fichier .tfvars).
variable "region" {
  description = "Région AWS du projet"
  type        = string
  default     = "ca-central-1"
}
