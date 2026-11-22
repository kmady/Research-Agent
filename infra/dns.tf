# Zone DNS du sous-domaine. Le domaine principal reste chez Bluehost : seul ce sous-domaine
# est délégué à Route 53 (4 enregistrements NS à ajouter chez Bluehost, voir l'output).
resource "aws_route53_zone" "main" {
  name    = var.domain_name
  comment = "Sous-domaine de ${var.project}, délégué depuis Bluehost"

  # Recréer la zone changerait ses serveurs de noms et casserait la délégation.
  lifecycle {
    prevent_destroy = true
  }
}

# ---------- Certificat HTTPS (ACM) ----------

# Certificat gratuit, renouvelé automatiquement par AWS tant que l'enregistrement de
# validation ci-dessous reste en place.
resource "aws_acm_certificate" "main" {
  domain_name       = var.domain_name
  validation_method = "DNS" # AWS vérifie qu'on contrôle le domaine par un enregistrement DNS

  lifecycle {
    # Si le certificat doit être remplacé, créer le nouveau AVANT de supprimer l'ancien :
    # le load balancer n'est jamais sans certificat.
    create_before_destroy = true
  }
}

# L'enregistrement de validation demandé par ACM, écrit dans notre zone Route 53.
resource "aws_route53_record" "cert_validation" {
  for_each = {
    for dvo in aws_acm_certificate.main.domain_validation_options : dvo.domain_name => dvo
  }

  zone_id = aws_route53_zone.main.zone_id
  name    = each.value.resource_record_name
  type    = each.value.resource_record_type
  records = [each.value.resource_record_value]
  ttl     = 300
}

# Ressource « d'attente » : rien n'est créé, Terraform attend que ACM déclare le
# certificat émis. L'écouteur HTTPS en dépend, il ne démarre qu'avec un certificat valide.
resource "aws_acm_certificate_validation" "main" {
  certificate_arn         = aws_acm_certificate.main.arn
  validation_record_fqdns = [for record in aws_route53_record.cert_validation : record.fqdn]
}

# ---------- Nom du service ----------

# research.madysystems.ca → load balancer. Un « alias » est propre à Route 53 : il suit
# les adresses du load balancer (qui changent) et ne coûte rien en requêtes.
resource "aws_route53_record" "app" {
  zone_id = aws_route53_zone.main.zone_id
  name    = var.domain_name
  type    = "A"

  alias {
    name                   = aws_lb.main.dns_name
    zone_id                = aws_lb.main.zone_id
    evaluate_target_health = true
  }
}
