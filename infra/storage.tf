# Volume de données /data : modèles Ollama, base SQLite, rapports, cache.
# Séparé du disque système : il survit au remplacement de l'instance.

resource "aws_ebs_volume" "data" {
  availability_zone = local.main_az # un volume EBS ne s'attache que dans sa zone
  size              = var.data_volume_size
  type              = "gp3"
  encrypted         = true

  tags = {
    Name   = "${local.name}-data"
    Backup = "daily" # repéré par la politique de snapshots ci-dessous
  }

  # Supprimer ce volume effacerait toutes les données : Terraform refusera.
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_volume_attachment" "data" {
  # Sur ces instances, le nom NVMe (nvme1n1, nvme2n1…) varie selon le démarrage, et la g4dn
  # a aussi un disque local éphémère (effacé à chaque arrêt). bootstrap.sh trouvera ce
  # volume par son identifiant : /dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_vol<id sans tiret>.
  device_name = "/dev/sdf"
  volume_id   = aws_ebs_volume.data.id
  instance_id = aws_instance.gpu.id

  # Pour détacher le volume (remplacement de l'instance), Terraform arrête d'abord
  # l'instance : les écritures en cours sont terminées proprement.
  stop_instance_before_detaching = true
}

# ---------- Sauvegarde : un snapshot par jour, 7 conservés ----------

# Data Lifecycle Manager agit avec son propre rôle.
data "aws_iam_policy_document" "dlm_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["dlm.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "dlm" {
  name               = "${local.name}-dlm"
  assume_role_policy = data.aws_iam_policy_document.dlm_trust.json
}

resource "aws_iam_role_policy_attachment" "dlm" {
  role       = aws_iam_role.dlm.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSDataLifecycleManagerServiceRole"
}

resource "aws_dlm_lifecycle_policy" "data" {
  # Caractères permis par AWS ici : lettres sans accent, chiffres, espace, - et _.
  description        = "${local.name} snapshot quotidien du volume data 7 jours"
  execution_role_arn = aws_iam_role.dlm.arn
  state              = "ENABLED"

  policy_details {
    resource_types = ["VOLUME"]
    target_tags    = { Backup = "daily" }

    schedule {
      name      = "quotidien"
      copy_tags = true

      create_rule {
        interval      = 24
        interval_unit = "HOURS"
        times         = ["07:00"] # UTC, soit 2 h ou 3 h du matin à Montréal
      }

      retain_rule {
        count = 7
      }
    }
  }
}
