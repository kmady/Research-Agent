# Registres d'images Docker (ECR). Deux images sont construites par nous :
#   - web     : l'application FastAPI ;
#   - searxng : SearXNG avec notre settings.yml intégré (deploy/searxng/).
# Ollama vient directement de Docker Hub (image officielle figée par digest).

locals {
  ecr_repositories = toset(["web", "searxng"])
}

resource "aws_ecr_repository" "app" {
  for_each = local.ecr_repositories # un ensemble : each.key et each.value valent le nom

  name = "${var.project}-${each.key}" # research-agent-web, research-agent-searxng

  # Un tag (le SHA du commit) désigne toujours la même image : impossible de l'écraser.
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true # analyse de vulnérabilités à chaque push
  }

  encryption_configuration {
    encryption_type = "AES256"
  }
}

# Cycle de vie : on garde les 20 images les plus récentes, les plus anciennes sont supprimées.
# Le contenu est un document JSON ; jsonencode() l'écrit à partir d'une structure HCL.
resource "aws_ecr_lifecycle_policy" "app" {
  for_each = aws_ecr_repository.app

  repository = each.value.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Garder les 20 dernières images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 20
      }
      action = { type = "expire" }
    }]
  })
}
