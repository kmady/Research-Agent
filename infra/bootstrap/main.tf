# Bootstrap : le bucket S3 qui stockera l'état Terraform de toute l'infrastructure.

# Source de données (data) : on LIT une information existante, ici le numéro du compte.
data "aws_caller_identity" "current" {}

# Valeurs calculées, réutilisées plus bas.
locals {
  # Un nom de bucket est unique au monde : le numéro de compte évite les collisions.
  bucket_name = "research-agent-tfstate-${data.aws_caller_identity.current.account_id}"
}

# Ressource (resource) : un objet que Terraform CRÉE et gère.
resource "aws_s3_bucket" "tfstate" {
  bucket = local.bucket_name

  # Garde-fou : « terraform destroy » refusera de supprimer ce bucket.
  lifecycle {
    prevent_destroy = true
  }
}

# Versionnage : chaque écriture de l'état garde la version précédente (retour arrière possible).
resource "aws_s3_bucket_versioning" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id # référence à l'autre ressource : Terraform en déduit l'ordre

  versioning_configuration {
    status = "Enabled"
  }
}

# Chiffrement au repos de tous les objets.
resource "aws_s3_bucket_server_side_encryption_configuration" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Aucun accès public, quelle que soit la configuration future.
resource "aws_s3_bucket_public_access_block" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Politique du bucket : refuser toute requête qui n'est pas chiffrée en transit (HTTPS).
data "aws_iam_policy_document" "tfstate" {
  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.tfstate.arn,
      "${aws_s3_bucket.tfstate.arn}/*",
    ]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id
  policy = data.aws_iam_policy_document.tfstate.json

  # La politique ne s'applique qu'une fois le blocage d'accès public en place.
  depends_on = [aws_s3_bucket_public_access_block.tfstate]
}

# Ménage : les anciennes versions de l'état sont supprimées après 90 jours.
resource "aws_s3_bucket_lifecycle_configuration" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id

  rule {
    id     = "expire-old-state-versions"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = 90
    }
  }

  depends_on = [aws_s3_bucket_versioning.tfstate]
}
