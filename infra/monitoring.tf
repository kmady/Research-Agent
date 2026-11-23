# Surveillance : journaux des conteneurs, alarmes, alertes par courriel.

data "aws_caller_identity" "current" {} # numéro du compte AWS courant

# ---------- Journaux ----------

# Les conteneurs y écriront avec le pilote Docker « awslogs » (configuré à l'étape 6).
resource "aws_cloudwatch_log_group" "app" {
  name              = "/${var.project}/${var.environment}"
  retention_in_days = 30
}

# Droit de l'instance d'écrire dans CE groupe de journaux seulement (annoncé en 5.4).
data "aws_iam_policy_document" "instance_logs" {
  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"]
    resources = ["${aws_cloudwatch_log_group.app.arn}:*"]
  }
}

resource "aws_iam_role_policy" "instance_logs" {
  name   = "logs"
  role   = aws_iam_role.instance.id
  policy = data.aws_iam_policy_document.instance_logs.json
}

# ---------- Alertes par courriel ----------

resource "aws_sns_topic" "alerts" {
  name = "${local.name}-alerts"
}

# AWS envoie un courriel de confirmation : l'abonnement reste « en attente »
# tant que le lien n'a pas été cliqué.
resource "aws_sns_topic_subscription" "alerts_email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# ---------- Alarmes ----------

# Le site ne répond plus : aucune cible saine derrière le load balancer.
# Active seulement pendant les heures d'ouverture (schedule.tf).
resource "aws_cloudwatch_metric_alarm" "no_healthy_target" {
  alarm_name          = "${local.name}-site-indisponible"
  alarm_description   = "Aucune instance saine derriere le load balancer depuis 5 minutes"
  namespace           = "AWS/ApplicationELB"
  metric_name         = "HealthyHostCount"
  statistic           = "Minimum"
  period              = 60
  evaluation_periods  = 5
  comparison_operator = "LessThanThreshold"
  threshold           = 1
  treat_missing_data  = "breaching" # pas de mesure = pas de cible = panne

  dimensions = {
    LoadBalancer = aws_lb.main.arn_suffix
    TargetGroup  = aws_lb_target_group.web.arn_suffix
  }

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn] # un courriel aussi au retour à la normale

  # Le planificateur (schedule.tf) active et désactive ces actions chaque jour :
  # Terraform ne doit pas les réactiver à chaque apply (sinon alertes la nuit).
  lifecycle {
    ignore_changes = [actions_enabled]
  }
}

# Erreurs 5xx en série (application en erreur ou injoignable).
resource "aws_cloudwatch_metric_alarm" "alb_5xx" {
  alarm_name          = "${local.name}-erreurs-5xx"
  alarm_description   = "Plus de 10 erreurs 5xx en 5 minutes"
  namespace           = "AWS/ApplicationELB"
  metric_name         = "HTTPCode_ELB_5XX_Count"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = 10
  treat_missing_data  = "notBreaching" # pas de mesure = pas d'erreur

  dimensions = {
    LoadBalancer = aws_lb.main.arn_suffix
  }

  alarm_actions = [aws_sns_topic.alerts.arn]

  # Le planificateur (schedule.tf) active et désactive ces actions chaque jour :
  # Terraform ne doit pas les réactiver à chaque apply (sinon alertes la nuit).
  lifecycle {
    ignore_changes = [actions_enabled]
  }
}

# La machine elle-même est malade (contrôles matériels ou système d'exploitation).
# Pas de mesure la nuit (instance arrêtée) : aucune alerte.
resource "aws_cloudwatch_metric_alarm" "instance_status" {
  alarm_name          = "${local.name}-instance-en-echec"
  alarm_description   = "Echec des controles de sante de l instance GPU"
  namespace           = "AWS/EC2"
  metric_name         = "StatusCheckFailed"
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 5
  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = 1
  treat_missing_data  = "notBreaching"

  dimensions = {
    InstanceId = aws_instance.gpu.id
  }

  alarm_actions = [aws_sns_topic.alerts.arn]
}
