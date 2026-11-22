output "dns_name_servers" {
  description = "Serveurs de noms à déclarer chez Bluehost (enregistrements NS du sous-domaine)"
  value       = aws_route53_zone.main.name_servers
}

output "dns_zone_id" {
  description = "Identifiant de la zone Route 53"
  value       = aws_route53_zone.main.zone_id
}
