# Les sorties : affichées après « apply », et lisibles avec « terraform output ».
output "state_bucket" {
  description = "Bucket S3 de l'état Terraform"
  value       = aws_s3_bucket.tfstate.bucket
}
