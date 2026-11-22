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
