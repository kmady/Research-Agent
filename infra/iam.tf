# Identités et droits (IAM).
#
# Deux rôles, chacun avec le strict nécessaire :
#   1. le rôle de l'instance GPU : ce que la machine a le droit de faire ;
#   2. le rôle de GitHub Actions : ce que la CI a le droit de faire, sans aucune clé AWS
#      stockée dans GitHub (connexion par OIDC).
#
# Les droits sont ajoutés couche par couche : l'écriture des journaux CloudWatch (5.7)
# viendra avec les groupes de journaux.

# ===================== 1. Rôle de l'instance GPU =====================

# Politique de confiance : QUI peut endosser le rôle. Ici, le service EC2 seulement.
data "aws_iam_policy_document" "instance_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "instance" {
  name               = "${local.name}-instance"
  assume_role_policy = data.aws_iam_policy_document.instance_trust.json
}

# Politique gérée par AWS : permet Session Manager (shell sans SSH) et Run Command (déploiement).
resource "aws_iam_role_policy_attachment" "instance_ssm" {
  role       = aws_iam_role.instance.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

# Politique de droits : CE QUE le rôle peut faire, et SUR QUOI.
data "aws_iam_policy_document" "instance" {
  # Obtenir un jeton de connexion à ECR : AWS n'accepte pas de restriction de ressource ici.
  statement {
    sid       = "EcrLogin"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  # Télécharger les images, uniquement depuis nos deux dépôts.
  statement {
    sid = "EcrPull"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
    ]
    resources = [for repo in aws_ecr_repository.app : repo.arn]
  }

  # Lire les valeurs des secrets, uniquement les nôtres.
  statement {
    sid       = "ReadAppSecrets"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [for secret in aws_secretsmanager_secret.app : secret.arn]
  }
}

resource "aws_iam_role_policy" "instance" {
  name   = "app"
  role   = aws_iam_role.instance.id
  policy = data.aws_iam_policy_document.instance.json
}

# Le « profil d'instance » est l'enveloppe qui permet d'attacher le rôle à une EC2.
resource "aws_iam_instance_profile" "instance" {
  name = "${local.name}-instance"
  role = aws_iam_role.instance.name
}

# ===================== 2. Rôle de GitHub Actions (OIDC) =====================

# Déclare GitHub comme fournisseur d'identité de confiance pour le compte AWS.
# À chaque exécution, GitHub délivre un jeton signé qui décrit le dépôt, la branche
# et l'environnement ; AWS l'échange contre des identifiants temporaires (1 h).
resource "aws_iam_openid_connect_provider" "github" {
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
}

data "aws_iam_policy_document" "github_trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }

    # Le jeton doit être destiné à AWS…
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    # …et venir de NOTRE dépôt, depuis la branche main ou l'environnement « production ».
    # Un fork, une autre branche ou une pull request sont refusés.
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values = [
        "${local.github_oidc_repo}:ref:refs/heads/main",
        "${local.github_oidc_repo}:environment:production",
      ]
    }
  }
}

resource "aws_iam_role" "github_deploy" {
  name                 = "${local.name}-github-deploy"
  assume_role_policy   = data.aws_iam_policy_document.github_trust.json
  max_session_duration = 3600
}

data "aws_iam_policy_document" "github_deploy" {
  statement {
    sid       = "EcrLogin"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  # Pousser des images, uniquement dans nos deux dépôts.
  statement {
    sid = "EcrPush"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
      "ecr:InitiateLayerUpload",
      "ecr:UploadLayerPart",
      "ecr:CompleteLayerUpload",
      "ecr:PutImage",
    ]
    resources = [for repo in aws_ecr_repository.app : repo.arn]
  }

  # Déployer : lancer un script sur NOTRE instance seulement, via SSM Run Command,
  # avec le document standard AWS-RunShellScript.
  statement {
    sid     = "DeployRunCommand"
    actions = ["ssm:SendCommand"]
    resources = [
      aws_instance.gpu.arn,
      "arn:aws:ssm:${var.region}::document/AWS-RunShellScript",
    ]
  }

  # Suivre le résultat de la commande et retrouver l'instance. Ces actions de lecture
  # n'acceptent pas de restriction de ressource.
  statement {
    sid = "DeployRead"
    actions = [
      "ssm:GetCommandInvocation",
      "ssm:ListCommandInvocations",
      "ec2:DescribeInstances",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "github_deploy" {
  name   = "deploy"
  role   = aws_iam_role.github_deploy.id
  policy = data.aws_iam_policy_document.github_deploy.json
}

# ===================== 3. Rôles de Terraform dans GitHub Actions =====================
#
# Deux rôles, pour deux niveaux de confiance (workflow .github/workflows/infra.yml) :
#   - terraform-plan  : lecture seule, pour le plan affiché sur chaque pull request ;
#   - terraform-apply : administrateur (Terraform crée des rôles IAM, des réseaux…),
#     réservé à l'environnement GitHub « infra », qui exige ton approbation et
#     n'accepte que la branche main.
# Les PR venant de forks n'obtiennent jamais de jeton OIDC : elles ne peuvent endosser
# ni l'un ni l'autre.

data "aws_iam_policy_document" "terraform_plan_trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["${local.github_oidc_repo}:pull_request"]
    }
  }
}

resource "aws_iam_role" "terraform_plan" {
  name                 = "${local.name}-terraform-plan"
  assume_role_policy   = data.aws_iam_policy_document.terraform_plan_trust.json
  max_session_duration = 3600
}

# Lecture seule sur tout le compte (y compris le fichier d'état dans S3), sans pouvoir
# lire la valeur des secrets. Le plan sur PR tourne sans verrou (-lock=false) : ce rôle
# n'écrit rien, nulle part.
resource "aws_iam_role_policy_attachment" "terraform_plan" {
  role       = aws_iam_role.terraform_plan.name
  policy_arn = "arn:aws:iam::aws:policy/ReadOnlyAccess"
}

data "aws_iam_policy_document" "terraform_apply_trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["${local.github_oidc_repo}:environment:infra"]
    }
  }
}

resource "aws_iam_role" "terraform_apply" {
  name                 = "${local.name}-terraform-apply"
  assume_role_policy   = data.aws_iam_policy_document.terraform_apply_trust.json
  max_session_duration = 3600
}

resource "aws_iam_role_policy_attachment" "terraform_apply" {
  role       = aws_iam_role.terraform_apply.name
  policy_arn = "arn:aws:iam::aws:policy/AdministratorAccess"
}
