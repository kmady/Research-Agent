# Load balancer (ALB) : seule porte d'entrée depuis Internet.
#
#   Internet ──443 HTTPS──► ALB (sous-réseaux publics) ──8000 HTTP──► instance (privée)
#   Internet ──80 HTTP────► ALB ──► redirection 301 vers HTTPS
#
# Le certificat TLS (dns.tf) est géré et renouvelé par AWS.

# ---------- Groupes de sécurité ----------

resource "aws_security_group" "alb" {
  name        = "${local.name}-alb"
  description = "Load balancer - HTTPS et HTTP depuis Internet" # ASCII seulement (règle AWS)
  vpc_id      = aws_vpc.main.id

  tags = { Name = "${local.name}-alb" }
}

# for_each sur une map de ports : une règle d'entrée par port.
resource "aws_vpc_security_group_ingress_rule" "alb_from_internet" {
  for_each = { https = 443, http = 80 }

  security_group_id = aws_security_group.alb.id
  description       = "${upper(each.key)} depuis Internet"
  ip_protocol       = "tcp"
  from_port         = each.value
  to_port           = each.value
  cidr_ipv4         = "0.0.0.0/0"
}

# Le load balancer ne peut parler qu'à l'instance, et seulement sur le port 8000.
resource "aws_vpc_security_group_egress_rule" "alb_to_instance" {
  security_group_id            = aws_security_group.alb.id
  description                  = "Vers application sur instance" # pas d'apostrophe ni d'accent (règle AWS)
  ip_protocol                  = "tcp"
  from_port                    = 8000
  to_port                      = 8000
  referenced_security_group_id = aws_security_group.instance.id
}

# Côté instance : le port 8000 n'accepte QUE le groupe du load balancer (pas une plage
# d'adresses). C'est l'entrée annoncée dans la couche 5.5.
resource "aws_vpc_security_group_ingress_rule" "instance_from_alb" {
  security_group_id            = aws_security_group.instance.id
  description                  = "Application depuis le load balancer"
  ip_protocol                  = "tcp"
  from_port                    = 8000
  to_port                      = 8000
  referenced_security_group_id = aws_security_group.alb.id
}

# ---------- Load balancer ----------

resource "aws_lb" "main" {
  name               = local.name
  load_balancer_type = "application"
  internal           = false
  subnets            = values(aws_subnet.public)[*].id # les deux sous-réseaux publics
  security_groups    = [aws_security_group.alb.id]

  # Rejette les en-têtes HTTP malformés (protection contre le « request smuggling »).
  drop_invalid_header_fields = true

  # Le flux de progression (SSE) envoie un « keep-alive » toutes les 15 s :
  # le délai d'inactivité par défaut (60 s) suffit.
  idle_timeout = 60
}

# Groupe cible : vers qui le load balancer envoie les requêtes, et comment il vérifie
# qu'elles sont en bonne santé.
resource "aws_lb_target_group" "web" {
  name        = "${local.name}-web"
  port        = 8000
  protocol    = "HTTP"
  target_type = "instance"
  vpc_id      = aws_vpc.main.id

  health_check {
    path                = "/api/health/live" # seule route sans authentification
    matcher             = "200"
    interval            = 15
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  # Lors d'un déploiement, laisser 30 s aux requêtes en cours pour se terminer.
  deregistration_delay = 30
}

resource "aws_lb_target_group_attachment" "web" {
  target_group_arn = aws_lb_target_group.web.arn
  target_id        = aws_instance.gpu.id
  port             = 8000
}

# ---------- Écouteurs ----------

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.main.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-Res-2021-06" # TLS 1.2 et 1.3 seulement
  certificate_arn   = aws_acm_certificate_validation.main.certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.web.arn
  }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "redirect"
    redirect {
      protocol    = "HTTPS"
      port        = "443"
      status_code = "HTTP_301"
    }
  }
}
