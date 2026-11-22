# Valeurs communes, réutilisées par toutes les couches.
locals {
  name = "${var.project}-${var.environment}" # préfixe des noms : research-agent-prod

  # Début du « sub » des jetons OIDC GitHub. GitHub y place les identifiants numériques
  # immuables : repo:kmady@30731043/Research-Agent@1464753526. Un dépôt supprimé puis
  # recréé sous le même nom (par quelqu'un d'autre) aurait d'autres identifiants.
  github_oidc_repo = format(
    "repo:%s@%s/%s@%s",
    split("/", var.github_repository)[0], var.github_owner_id,
    split("/", var.github_repository)[1], var.github_repository_id,
  )
}
