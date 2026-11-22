output "dns_name_servers" {
  description = "Serveurs de noms à déclarer chez Bluehost (enregistrements NS du sous-domaine)"
  value       = aws_route53_zone.main.name_servers
}

output "dns_zone_id" {
  description = "Identifiant de la zone Route 53"
  value       = aws_route53_zone.main.zone_id
}

output "vpc_id" {
  description = "Identifiant du VPC"
  value       = aws_vpc.main.id
}

output "public_subnet_ids" {
  description = "Sous-réseaux publics (load balancer), par zone"
  value       = { for az, subnet in aws_subnet.public : az => subnet.id }
}

output "private_subnet_id" {
  description = "Sous-réseau privé (instance GPU)"
  value       = aws_subnet.private.id
}

output "ecr_repository_urls" {
  description = "Adresses des registres ECR, à utiliser pour docker push et WEB_IMAGE / SEARXNG_IMAGE"
  value       = { for key, repo in aws_ecr_repository.app : key => repo.repository_url }
}

output "secret_names" {
  description = "Noms des secrets dans Secrets Manager (les valeurs se saisissent hors Terraform)"
  value       = { for key, secret in aws_secretsmanager_secret.app : key => secret.name }
}

output "github_deploy_role_arn" {
  description = "Rôle à indiquer à aws-actions/configure-aws-credentials (role-to-assume)"
  value       = aws_iam_role.github_deploy.arn
}

output "instance_profile_name" {
  description = "Profil d'instance attaché à l'EC2 GPU"
  value       = aws_iam_instance_profile.instance.name
}
