# Secrets de l'application, dans Secrets Manager.
#
# Terraform crée seulement les « conteneurs », SANS les valeurs : une valeur écrite ici
# finirait en clair dans le fichier d'état (S3). Les valeurs sont saisies à part,
# avec l'AWS CLI (voir DEPLOIEMENT_AWS.md).
#
# La clé d'API du moteur de recherche sera ajoutée ici quand le moteur sera choisi.

locals {
  # nom court => description ; le nom court correspond à la variable d'environnement.
  secrets = {
    "RA_WEB_PASSWORD" = "Mot de passe de l'authentification Basic de l'interface web"
    "SEARXNG_SECRET"  = "Clé secrète de SearXNG (chaîne aléatoire)"
    # Pas un secret, mais une donnée personnelle (contact envoyé aux sites consultés) :
    # elle ne doit pas figurer dans le dépôt public.
    "RA_USER_AGENT" = "Contact public envoyé aux sites consultés (User-Agent)"
  }
}

resource "aws_secretsmanager_secret" "app" {
  for_each = local.secrets

  name        = "${local.name}/${each.key}" # research-agent-prod/RA_WEB_PASSWORD
  description = each.value

  # Après une suppression, le secret reste récupérable 7 jours.
  recovery_window_in_days = 7
}
