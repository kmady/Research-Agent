# Instance GPU : exécute les trois conteneurs (web, ollama, searxng).
#
# - sous-réseau privé, aucune adresse publique, aucun port SSH : l'accès administrateur
#   passe par SSM Session Manager (rôle IAM de l'instance) ;
# - AMI « Deep Learning Base GPU » : pilote NVIDIA, Docker et NVIDIA Container Toolkit
#   déjà installés ;
# - les données vivent sur un volume EBS séparé (storage.tf), pas sur le disque système.

# AWS publie l'identifiant de la dernière AMI dans un paramètre SSM public.
data "aws_ssm_parameter" "gpu_ami" {
  name = "/aws/service/deeplearning/ami/x86_64/base-oss-nvidia-driver-gpu-ubuntu-24.04/latest/ami-id"
}

# Groupe de sécurité de l'instance. Aucune entrée pour l'instant : le port 8000 sera
# ouvert au seul load balancer dans la couche 5.6. Sortie libre (moteurs de recherche,
# pages web, ECR, SSM), via la NAT Gateway.
resource "aws_security_group" "instance" {
  name        = "${local.name}-instance"
  description = "Instance GPU - entree depuis le load balancer seulement" # ASCII seulement (règle AWS)
  vpc_id      = aws_vpc.main.id

  tags = { Name = "${local.name}-instance" }
}

resource "aws_vpc_security_group_egress_rule" "instance_all" {
  security_group_id = aws_security_group.instance.id
  description       = "Sortie vers Internet (via la NAT) et les services AWS"
  ip_protocol       = "-1" # tous les protocoles
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_instance" "gpu" {
  ami                    = data.aws_ssm_parameter.gpu_ami.insecure_value
  instance_type          = var.instance_type
  subnet_id              = aws_subnet.private.id
  vpc_security_group_ids = [aws_security_group.instance.id]
  iam_instance_profile   = aws_iam_instance_profile.instance.name

  # IMDSv2 obligatoire ; limite de saut à 1 : les conteneurs n'atteignent pas
  # les identifiants de l'instance.
  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  # Disque système : l'OS et les images Docker. 75 Go est le minimum de l'AMI.
  root_block_device {
    volume_type = "gp3"
    volume_size = 75
    encrypted   = true
  }

  # Un « shutdown » lancé depuis la machine l'arrête au lieu de la supprimer.
  instance_initiated_shutdown_behavior = "stop"

  tags = { Name = "${local.name}-gpu" }

  lifecycle {
    # Le paramètre « latest » change à chaque nouvelle AMI publiée par AWS. Sans cette
    # ligne, chaque plan voudrait REMPLACER l'instance. On change d'AMI volontairement,
    # avec terraform apply -replace=aws_instance.gpu.
    ignore_changes = [ami]
  }
}
