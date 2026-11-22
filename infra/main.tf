# Valeurs communes, réutilisées par toutes les couches.
locals {
  name = "${var.project}-${var.environment}" # préfixe des noms : research-agent-prod
}
