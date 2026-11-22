# État de l'infrastructure principale : même bucket que le bootstrap, autre chemin (key).
terraform {
  backend "s3" {
    bucket       = "research-agent-tfstate-034565845674"
    key          = "prod/terraform.tfstate"
    region       = "ca-central-1"
    encrypt      = true
    use_lockfile = true
  }
}
