# L'état de ce bootstrap est lui-même stocké dans le bucket qu'il a créé.
# Un bloc backend ne peut pas utiliser de variables : les valeurs sont écrites en dur.
terraform {
  backend "s3" {
    bucket       = "research-agent-tfstate-034565845674"
    key          = "bootstrap/terraform.tfstate" # chemin du fichier d'état dans le bucket
    region       = "ca-central-1"
    encrypt      = true
    use_lockfile = true # verrou dans S3 : deux « apply » simultanés sont impossibles
  }
}
