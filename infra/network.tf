# Réseau : un VPC avec
#   - 2 sous-réseaux publics (le load balancer en exige deux, dans deux zones différentes) ;
#   - 1 sous-réseau privé pour l'instance GPU (aucune adresse publique) ;
#   - une NAT Gateway pour que l'instance privée sorte vers Internet (moteurs de recherche, pages web).
#
# Plan d'adressage (cidrsubnet découpe le /16 en /24) :
#   10.0.0.0/24  public  zone a        10.0.10.0/24  privé  zone a
#   10.0.1.0/24  public  zone b

locals {
  # Une entrée par zone : { "ca-central-1a" = "10.0.0.0/24", "ca-central-1b" = "10.0.1.0/24" }
  public_subnets = {
    for index, az in var.availability_zones : az => cidrsubnet(var.vpc_cidr, 8, index)
  }

  # L'instance et la NAT Gateway vont dans la première zone.
  main_az = var.availability_zones[0]
}

resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true # nécessaire aux points de terminaison VPC et à SSM

  tags = { Name = local.name }
}

# Groupe de sécurité par défaut du VPC : on lui retire toutes ses règles,
# pour qu'une ressource oubliée sans groupe explicite ne soit jamais ouverte.
resource "aws_default_security_group" "default" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${local.name}-default-ferme" }
}

# ---------- Partie publique ----------

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = local.name }
}

resource "aws_subnet" "public" {
  for_each = local.public_subnets # crée une ressource par entrée de la map

  vpc_id            = aws_vpc.main.id
  availability_zone = each.key   # la clé : la zone
  cidr_block        = each.value # la valeur : le bloc d'adresses

  tags = { Name = "${local.name}-public-${each.key}" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"                  # tout ce qui sort du VPC…
    gateway_id = aws_internet_gateway.main.id # …passe par la passerelle Internet
  }

  tags = { Name = "${local.name}-public" }
}

resource "aws_route_table_association" "public" {
  for_each = aws_subnet.public # on peut aussi itérer sur une ressource créée par for_each

  subnet_id      = each.value.id
  route_table_id = aws_route_table.public.id
}

# ---------- Sortie Internet de la partie privée ----------

resource "aws_eip" "nat" {
  domain = "vpc"
  tags   = { Name = "${local.name}-nat" }
}

resource "aws_nat_gateway" "main" {
  allocation_id = aws_eip.nat.id
  subnet_id     = aws_subnet.public[local.main_az].id # la NAT vit dans un sous-réseau public

  tags = { Name = local.name }

  # La NAT ne fonctionne qu'une fois la passerelle Internet en place.
  depends_on = [aws_internet_gateway.main]
}

# ---------- Partie privée ----------

resource "aws_subnet" "private" {
  vpc_id            = aws_vpc.main.id
  availability_zone = local.main_az
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, 10) # 10.0.10.0/24

  tags = { Name = "${local.name}-private-${local.main_az}" }
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.main.id # sortie seulement : rien n'entre par la NAT
  }

  tags = { Name = "${local.name}-private" }
}

resource "aws_route_table_association" "private" {
  subnet_id      = aws_subnet.private.id
  route_table_id = aws_route_table.private.id
}

# Point de terminaison S3 de type « gateway » : gratuit. Les couches des images ECR
# sont stockées dans S3 ; elles passent par ici au lieu de la NAT (facturée au Go).
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private.id]

  tags = { Name = "${local.name}-s3" }
}
