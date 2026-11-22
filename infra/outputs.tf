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
