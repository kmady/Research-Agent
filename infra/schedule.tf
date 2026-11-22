# Horaires d'ouverture : du lundi au vendredi, 8 h à 20 h (heure de Montréal).
#
#   08:00  démarrage de l'instance ; 08:15 réactivation des alarmes du site
#   20:00  désactivation des alarmes du site ; arrêt PROPRE : on attend la fin des
#          recherches en cours (60 min au plus), puis l'instance s'éteint elle-même
#   21:15  filet de sécurité : arrêt direct si l'instance tourne encore
#
# EventBridge Scheduler appelle directement les API AWS (cibles « universelles »
# arn:aws:scheduler:::aws-sdk:<service>:<action>), avec son propre rôle IAM.

locals {
  schedule_tz = var.schedule_timezone

  # Alarmes qui n'ont de sens que pendant les heures d'ouverture (monitoring.tf).
  business_hours_alarms = [
    aws_cloudwatch_metric_alarm.no_healthy_target.alarm_name,
    aws_cloudwatch_metric_alarm.alb_5xx.alarm_name,
  ]

  # Script exécuté sur l'instance à 20 h, via SSM Run Command.
  graceful_stop_script = <<-EOT
    set -u
    # Identifiants de l'interface (écrits par bootstrap.sh) ; absents = rien à attendre.
    if [ -r /etc/research-agent/.env ]; then set -a; . /etc/research-agent/.env; set +a; fi
    for i in $(seq 1 60); do
      busy=$(curl -fsS --max-time 5 -u "$${RA_WEB_USER:-admin}:$${RA_WEB_PASSWORD:-}" \
        http://127.0.0.1:8000/api/health \
        | python3 -c 'import json,sys; h=json.load(sys.stdin); print(h["running"] + h["queued"])') || break
      [ "$busy" = "0" ] && break
      echo "Recherches en cours : $busy ; nouvelle vérification dans 1 min ($i/60)"
      sleep 60
    done
    echo "Arrêt de l'instance"
    shutdown -h +1
  EOT
}

# ---------- Rôle utilisé par le planificateur ----------

data "aws_iam_policy_document" "scheduler_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
    # Seuls les plannings de CE compte peuvent endosser le rôle.
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "${local.name}-scheduler"
  assume_role_policy = data.aws_iam_policy_document.scheduler_trust.json
}

data "aws_iam_policy_document" "scheduler" {
  statement {
    sid       = "StartStopInstance"
    actions   = ["ec2:StartInstances", "ec2:StopInstances"]
    resources = [aws_instance.gpu.arn]
  }

  statement {
    sid     = "GracefulStop"
    actions = ["ssm:SendCommand"]
    resources = [
      aws_instance.gpu.arn,
      "arn:aws:ssm:${var.region}::document/AWS-RunShellScript",
    ]
  }

  statement {
    sid       = "ToggleAlarms"
    actions   = ["cloudwatch:EnableAlarmActions", "cloudwatch:DisableAlarmActions"]
    resources = [for alarm in [aws_cloudwatch_metric_alarm.no_healthy_target, aws_cloudwatch_metric_alarm.alb_5xx] : alarm.arn]
  }
}

resource "aws_iam_role_policy" "scheduler" {
  name   = "schedule"
  role   = aws_iam_role.scheduler.id
  policy = data.aws_iam_policy_document.scheduler.json
}

resource "aws_scheduler_schedule_group" "main" {
  name = local.name
}

# ---------- Les plannings ----------

resource "aws_scheduler_schedule" "start" {
  name                         = "start"
  group_name                   = aws_scheduler_schedule_group.main.name
  schedule_expression          = "cron(0 8 ? * MON-FRI *)" # minute heure jour mois jour-semaine année
  schedule_expression_timezone = local.schedule_tz         # heure d'été gérée automatiquement
  flexible_time_window { mode = "OFF" }

  target {
    arn      = "arn:aws:scheduler:::aws-sdk:ec2:startInstances"
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({ InstanceIds = [aws_instance.gpu.id] })
  }
}

# Le démarrage et le chargement du modèle prennent quelques minutes : les alarmes
# ne reviennent qu'à 8 h 15.
resource "aws_scheduler_schedule" "alarms_on" {
  name                         = "alarms-on"
  group_name                   = aws_scheduler_schedule_group.main.name
  schedule_expression          = "cron(15 8 ? * MON-FRI *)"
  schedule_expression_timezone = local.schedule_tz
  flexible_time_window { mode = "OFF" }

  target {
    arn      = "arn:aws:scheduler:::aws-sdk:cloudwatch:enableAlarmActions"
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({ AlarmNames = local.business_hours_alarms })
  }
}

resource "aws_scheduler_schedule" "alarms_off" {
  name                         = "alarms-off"
  group_name                   = aws_scheduler_schedule_group.main.name
  schedule_expression          = "cron(0 20 ? * MON-FRI *)"
  schedule_expression_timezone = local.schedule_tz
  flexible_time_window { mode = "OFF" }

  target {
    arn      = "arn:aws:scheduler:::aws-sdk:cloudwatch:disableAlarmActions"
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({ AlarmNames = local.business_hours_alarms })
  }
}

resource "aws_scheduler_schedule" "graceful_stop" {
  name                         = "graceful-stop"
  group_name                   = aws_scheduler_schedule_group.main.name
  schedule_expression          = "cron(0 20 ? * MON-FRI *)"
  schedule_expression_timezone = local.schedule_tz
  flexible_time_window { mode = "OFF" }

  target {
    arn      = "arn:aws:scheduler:::aws-sdk:ssm:sendCommand"
    role_arn = aws_iam_role.scheduler.arn
    input = jsonencode({
      DocumentName = "AWS-RunShellScript"
      InstanceIds  = [aws_instance.gpu.id]
      Comment      = "Arret propre du soir"
      Parameters = {
        commands         = [local.graceful_stop_script]
        executionTimeout = ["4200"] # 70 min : l'attente maximale est de 60 min
      }
    })
  }
}

resource "aws_scheduler_schedule" "hard_stop" {
  name                         = "hard-stop"
  group_name                   = aws_scheduler_schedule_group.main.name
  schedule_expression          = "cron(15 21 ? * MON-FRI *)"
  schedule_expression_timezone = local.schedule_tz
  flexible_time_window { mode = "OFF" }

  target {
    arn      = "arn:aws:scheduler:::aws-sdk:ec2:stopInstances"
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({ InstanceIds = [aws_instance.gpu.id] })
  }
}
