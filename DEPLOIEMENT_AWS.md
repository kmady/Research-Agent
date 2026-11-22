# Déploiement sur AWS : plan

Ce document décrit comment mettre Research-Agent en production sur AWS, à partir d'un dépôt GitHub et d'une chaîne CI/CD. **C'est un plan : aucune étape n'a encore été réalisée.** Chaque étape se termine par un critère de validation, et la suivante ne commence que lorsque ce critère est rempli.

## Hypothèses retenues

| Sujet | Choix | Conséquence |
|---|---|---|
| Public | Petit groupe (moins de 10 personnes) | Authentification HTTP Basic sur HTTPS : suffisante, un seul compte partagé |
| Disponibilité | Service arrêté la nuit | Arrêt et démarrage planifiés : on ne paie l'instance GPU qu'en journée |
| Infrastructure | Terraform | Toute l'infrastructure est décrite dans `infra/` et passe par des pull requests |
| Moteur de recherche | **À décider** (voir « Décisions ouvertes ») | Sans moteur à clé d'API, la recherche sera fragile sur AWS |

---

## 1. Architecture cible

### Pourquoi une seule instance EC2 GPU

| Contrainte du projet | Choix AWS |
|---|---|
| Le modèle (qwen3:8b) a besoin d'un GPU | EC2 de la famille G ; Fargate et Lambda n'ont pas de GPU |
| Une recherche à la fois, état dans SQLite | Une seule instance, avec un volume EBS persistant |
| Moins de 10 utilisateurs | Aucun besoin de mise à l'échelle, de Kubernetes ou d'ECS |

### Schéma

```
 Utilisateurs (< 10)
       │ HTTPS (certificat ACM)
       ▼
 Application Load Balancer ── sous-réseaux publics
       │ HTTP :8000
       ▼
 EC2 GPU ── sous-réseau privé, aucun port SSH
  ├─ web      : FastAPI, image ECR taguée par SHA
  ├─ ollama   : conteneur GPU, port 11434 interne
  ├─ searxng  : image figée, port 8080 interne
  └─ EBS /data : SQLite, rapports, cache, modèles Ollama

 GitHub Actions ──OIDC──► ECR (images) ──SSM Run Command──► EC2
 Secrets Manager · CloudWatch Logs et alarmes · snapshots EBS · EventBridge Scheduler
```

### Composants AWS

| Composant | Service | Rôle |
|---|---|---|
| Calcul | EC2 `g4dn.xlarge` (T4, 16 Go) ou `g6.xlarge` (L4, 24 Go) | Exécute les trois conteneurs |
| Système | AMI « Deep Learning Base GPU (Ubuntu) » | Pilote NVIDIA, Docker et NVIDIA Container Toolkit déjà installés |
| Stockage | EBS gp3 séparé de 100 Go, monté sur `/data` | Modèles, base SQLite, rapports, cache ; survit à un remplacement de l'instance |
| Exposition | Application Load Balancer, certificat ACM, Route 53 | HTTPS géré par AWS ; l'instance reste privée |
| Images | Amazon ECR | Images du service web, avec un cycle de vie (20 dernières conservées) |
| Secrets | Secrets Manager | `RA_WEB_PASSWORD`, `SEARXNG_SECRET`, clé d'API de recherche |
| Accès administrateur | SSM Session Manager | Aucun port SSH, aucune clé SSH |
| Planification | EventBridge Scheduler et SSM Run Command | Lundi au vendredi 8 h - 20 h (America/Toronto) : démarrage, arrêt propre après les recherches en cours, arrêt forcé à 21 h 15 |
| Observation | CloudWatch Logs, alarmes, AWS Budgets | Journaux, alertes de santé et de coût |
| Sauvegarde | Data Lifecycle Manager | Snapshot quotidien du volume `/data`, conservé 7 jours |
| Sortie Internet | NAT Gateway | Accès aux moteurs de recherche et aux pages web depuis le sous-réseau privé |

**Le load balancer plutôt que Caddy :** avec le load balancer, le certificat est géré et l'instance n'a pas d'adresse publique. L'alternative la moins chère est une instance en sous-réseau public avec Caddy (Let's Encrypt) et une adresse IP élastique. Elle économise le load balancer et le NAT Gateway, mais expose l'instance directement. Pour un petit public, les deux sont défendables ; ce plan retient la plus sûre.

**Région :** choisis celle qui est la plus proche des utilisateurs **et** propose l'instance GPU retenue (à vérifier dans la console EC2).

### Ordre de grandeur des coûts

Les montants ci-dessous sont indicatifs : vérifie les prix de ta région avant de décider.

| Poste | Hypothèse | Ordre de grandeur |
|---|---|---|
| EC2 GPU | 12 h par jour en semaine, soit environ 260 h par mois | 140 à 220 $ par mois selon l'instance |
| Load balancer | permanent | environ 20 $ par mois |
| NAT Gateway | permanent, plus le trafic | environ 35 $ par mois |
| EBS 100 Go et snapshots | permanent | environ 10 $ par mois |
| ECR, CloudWatch, Secrets Manager | faible volume | moins de 10 $ par mois |

Une instance arrêtée ne coûte rien en calcul, mais le load balancer, le NAT Gateway et l'EBS restent facturés. Pour réduire ces coûts fixes, il faut prendre la variante Caddy.

---

## 2. Organisation du dépôt

```
Research-Agent/
├── .github/
│   ├── workflows/
│   │   ├── ci.yml              # à chaque pull request
│   │   ├── deploy.yml          # à chaque fusion dans main
│   │   └── infra.yml           # plan et apply Terraform
│   └── dependabot.yml
├── deploy/
│   ├── docker-compose.prod.yml # web, ollama, searxng
│   ├── searxng/                # Dockerfile et settings.yml (image dédiée)
│   ├── deploy.sh               # exécuté sur l'instance par SSM
│   └── bootstrap.sh            # premier démarrage : volume, secrets, modèles
├── infra/                      # Terraform
│   ├── backend.tf  versions.tf  variables.tf  outputs.tf
│   ├── network.tf  compute.tf  storage.tf  alb.tf  dns.tf
│   ├── iam.tf      ecr.tf      secrets.tf  schedule.tf  monitoring.tf
│   └── envs/prod.tfvars
├── research_agent/  tests/  scripts/  eval/
├── Dockerfile  pyproject.toml  uv.lock
├── .env.example
└── DEPLOIEMENT_AWS.md
```

---

## 3. Étapes

### Étape 1 : préparer le dépôt GitHub

1. Vérifier que `.gitignore` exclut `.env`, `data/`, `.cache/`, `reports/*`, `eval/runs/` et `.venv/`.
2. Créer `.env.example` avec des valeurs fictives ; `.env` n'est **jamais** committé.
3. Faire le premier commit, puis créer un **dépôt privé** sur GitHub.
4. Protéger `main` : pull request obligatoire, CI verte exigée, historique linéaire, pas de push forcé.
5. Activer Dependabot (pip, Docker, GitHub Actions), le secret scanning et la push protection.

**Validation :** `git log -p | grep -iE "password|secret|token"` ne trouve aucune valeur réelle, et une pull request de test est bloquée tant que la CI n'est pas verte.

### Étape 2 : intégration continue (CI)

Fichier `ci.yml`, déclenché à chaque pull request. Tout tourne **sans GPU ni modèle**, grâce aux exécutants factices des tests.

| Job | Contenu | Bloquant |
|---|---|---|
| `quality` | `ruff check`, `ruff format --check` | oui |
| `tests` | `uv sync --frozen --extra dev`, puis `pytest -q` (120 tests) | oui |
| `deps` | `pip-audit` | vulnérabilités critiques |
| `image` | `docker build` (sans push), puis scan Trivy | vulnérabilités critiques |
| `terraform` | `fmt -check`, `validate`, `tflint` (si `infra/` a changé) | oui |

Ce qui n'est **pas** dans la CI : l'évaluation réelle (`scripts/evaluate.py`). Elle a besoin du GPU, dure plusieurs heures et dépend des moteurs de recherche. Elle se lance à la main sur l'instance (étape 10).

**Validation :** une pull request qui casse un test ne peut pas être fusionnée.

### Étape 3 : adapter le projet à la production

| Changement | Raison |
|---|---|
| Ajouter `ruff` et générer `uv.lock` ; le Dockerfile utilise `uv sync --frozen` | Des builds reproductibles |
| Écrire `docker-compose.prod.yml` : services `web`, `ollama` (`gpus: all`) et `searxng`, sur un réseau interne | Plus de dépendance à l'Ollama installé sur l'hôte |
| Figer les images : `ollama/ollama:<version>`, `searxng/searxng:<version>` | Pas de changement involontaire |
| Construire une image SearXNG qui contient `settings.yml` | Supprime le problème de propriétaire (uid 977) et le `docker cp` |
| Désigner l'image web par une variable (`WEB_IMAGE=<ecr>/research-agent-web:<sha>`) | Déploiement et retour arrière par tag |
| Envoyer les journaux Docker vers CloudWatch (pilote `awslogs`) | Journaux centralisés |
| Prévoir l'arrêt propre : avant d'arrêter l'instance, attendre que `/api/health` indique `running: 0` | Ne pas couper une recherche le soir |

**Validation :** `docker compose -f deploy/docker-compose.prod.yml up` fonctionne sur la machine locale, et `/api/health` répond `ok: true`.

### Étape 4 : préparer le compte AWS

1. Activer MFA sur le compte racine, puis ne plus l'utiliser. Créer un accès administrateur via IAM Identity Center.
2. **Demander le quota GPU** : Service Quotas, « Running On-Demand G and VT instances », au moins 4 vCPU. Un compte récent a souvent un quota de 0, et l'accord peut prendre plusieurs jours.
3. Créer un budget AWS avec des alertes à 50, 80 et 100 % du montant prévu.
4. Créer le bucket S3 de l'état Terraform (versionné, chiffré, accès public bloqué). Le verrouillage se fait par fichier dans S3 (`use_lockfile`) ou par une table DynamoDB.

**Validation :** quota accordé, budget actif, bucket d'état prêt.

### Étape 5 : écrire l'infrastructure avec Terraform

| Fichier | Ressources |
|---|---|
| `network.tf` | VPC ; 2 sous-réseaux publics (load balancer) et 1 privé (instance) ; NAT Gateway ; point de terminaison S3 (gateway, gratuit). Pas de points de terminaison « interface » (SSM, ECR) : la NAT suffit, et ils coûteraient environ 8 $ par mois chacun |
| `compute.tf` | EC2 GPU, IMDSv2 obligatoire, volume racine chiffré, user-data qui lance `bootstrap.sh` |
| `storage.tf` | Volume EBS gp3 `/data` chiffré, avec `prevent_destroy` ; politique de snapshots |
| `alb.tf` | Load balancer, groupe cible :8000, contrôle de santé sur `/api/health/live`, redirection HTTP vers HTTPS |
| `dns.tf` | Certificat ACM validé par DNS, enregistrement Route 53 |
| `iam.tf` | Rôle de l'instance (SSM, lecture ECR, lecture des secrets, CloudWatch) ; rôle GitHub OIDC (section 4) |
| `ecr.tf` | Dépôts `research-agent-web` et `research-agent-searxng`, tags immuables, scan au push, cycle de vie |
| `secrets.tf` | Conteneurs de secrets, **sans les valeurs** : elles sont saisies hors de Terraform |
| `schedule.tf` | Démarrage le matin, arrêt propre le soir (section 5) |
| `monitoring.tf` | Groupes de journaux (rétention 30 jours), alarmes, sujet SNS pour les alertes par e-mail |

Groupes de sécurité :
- le load balancer accepte le port 443 depuis Internet ;
- l'instance n'accepte que le port 8000, et seulement depuis le load balancer ;
- aucune autre entrée.

Fichier `infra.yml` :
- sur une pull request : `terraform plan`, publié en commentaire ;
- après la fusion : `terraform apply`, derrière l'approbation de l'environnement GitHub `production`.

**Validation :**
- `terraform apply` réussit ;
- l'instance est joignable par Session Manager, et `nvidia-smi` y affiche le GPU ;
- le certificat est émis.

### Étape 6 : premier démarrage de l'instance (bootstrap)

`bootstrap.sh`, lancé par user-data au premier démarrage :
1. formate (la première fois seulement) et monte le volume `/data` ;
2. récupère les secrets dans Secrets Manager et écrit `/etc/research-agent/.env`, en mode 600 ;
3. se connecte à ECR, puis lance `docker compose up -d` ;
4. télécharge les modèles une seule fois : `docker compose exec ollama ollama pull qwen3:8b`, puis `nomic-embed-text`. Ils sont stockés sur `/data`.

**Validation :** `curl` sur `/api/health` depuis l'instance renvoie `ok: true`, et le site répond en HTTPS avec authentification.

### Étape 7 : premier déploiement manuel

Avant d'automatiser, faire le chemin complet à la main :
1. construire l'image et la pousser vers ECR avec le tag du commit ;
2. lancer `deploy.sh <sha>` via SSM Run Command ;
3. poser une vraie question dans l'interface.

**Validation :** une recherche aboutit, avec un rapport, depuis le navigateur d'un utilisateur.

### Étape 8 : déploiement continu (CD)

Fichier `deploy.yml`, déclenché à chaque fusion dans `main` :

```
build ─► push ECR (tag = SHA) ─► approbation « production » ─► déploiement SSM ─► test de fumée ─► (retour arrière si échec)
```

1. **Authentification par OIDC.** GitHub obtient un rôle IAM temporaire : **aucune clé AWS n'est stockée dans GitHub**. Le rôle n'accepte que ce dépôt et la branche `main` (condition sur `token.actions.githubusercontent.com:sub`), et ses droits sont limités au push ECR, à `ssm:SendCommand` sur cette instance, à l'écriture du paramètre de version et à `ec2:DescribeInstances`.
2. **Version de référence.** Le SHA déployé est écrit dans le paramètre SSM `/research-agent/web-image-tag`. Il fait foi pour la version en service.
3. **Instance arrêtée (la nuit).** Le workflow met à jour le paramètre et s'arrête là. Au démarrage du matin, `deploy.sh` lit le paramètre et déploie cette version. Aucun déploiement n'est donc perdu.
4. **Instance en marche.** `deploy.sh <sha>` :
   - attend qu'aucune recherche ne soit en cours (`running: 0`), 15 minutes au plus ;
   - lance `docker compose pull web`, puis `docker compose up -d web` ;
   - vérifie `/api/health` (`ok: true`) dans les 2 minutes ;
   - en cas d'échec, redéploie le tag précédent et fait échouer le workflow.
5. **Approbation :** l'environnement GitHub `production` exige ton approbation avant le déploiement.

**Pas de staging permanent** (un second GPU doublerait le coût). Le filet de sécurité repose sur la CI, l'approbation, le test de fumée et le retour arrière automatique. Pour un changement risqué (nouveau modèle, nouvelle version d'Ollama) : le tester d'abord en local avec `docker-compose.prod.yml`.

**Validation :**
- une fusion dans `main` déploie la nouvelle version après approbation ;
- une version volontairement cassée revient automatiquement à la précédente.

### Étape 9 : exploitation

| Sujet | Mise en œuvre |
|---|---|
| Arrêt le soir | EventBridge Scheduler (ex. 21 h, jours ouvrés, fuseau horaire paramétré) lance une automatisation SSM qui attend `running: 0` (30 minutes au plus), puis arrête l'instance |
| Démarrage le matin | EventBridge Scheduler (ex. 8 h) démarre l'instance ; au boot, `deploy.sh` applique la version de référence |
| Hors horaires | Le load balancer répond 503 ; l'interface indique les heures d'ouverture. Un démarrage manuel reste possible depuis la console |
| Journaux | CloudWatch Logs, un groupe par conteneur, rétention de 30 jours |
| Alarmes (e-mail via SNS) | Cible du load balancer en mauvaise santé **pendant les heures d'ouverture** ; disque `/data` à plus de 80 % ; échec de déploiement ; seuils du budget |
| Sauvegardes | Snapshot quotidien de `/data` (7 jours). Restauration testée une fois : nouveau volume à partir d'un snapshot, monté sur l'instance |
| Mises à jour | SSM Patch Manager dans une fenêtre de maintenance ; Dependabot pour les images et les dépendances |
| Secrets | Rotation du mot de passe : modifier le secret, puis redéployer (`deploy.sh` relit les secrets) |

**Validation :**
- l'instance s'arrête et redémarre seule aux heures prévues ;
- une alarme de test arrive par e-mail ;
- une restauration de snapshot a été testée.

### Étape 10 : fiabiliser la recherche et évaluer

1. **Moteur avec clé d'API** derrière l'interface `SearchProvider` (voir « Décisions ouvertes »), avec SearXNG en secours. La clé va dans Secrets Manager.
2. **Évaluation complète** (30 questions, environ 3 h 30), lancée à la main sur l'instance avec `scripts/evaluate.py`. Les résultats vont dans `/data/eval/`.

**Validation :** les 30 questions aboutissent sans panne des moteurs de recherche.

---

## 4. Sécurité : récapitulatif

- Aucune clé AWS dans GitHub : OIDC, avec un rôle limité au dépôt, à la branche et aux actions nécessaires.
- Aucun accès SSH : Session Manager uniquement, et ses sessions sont journalisées.
- Instance en sous-réseau privé : seul le load balancer l'atteint, et seulement sur le port 8000.
- HTTPS obligatoire ; l'authentification Basic ne circule jamais en clair.
- Secrets dans Secrets Manager, jamais dans le dépôt, l'image ou Terraform.
- Volumes EBS chiffrés, IMDSv2 obligatoire, images scannées par Trivy et ECR.
- Ollama et SearXNG ne sont jamais exposés.

---

## 5. Décisions ouvertes

1. **Moteur de recherche avec clé d'API (recommandé).** Les adresses IP d'AWS sont plus souvent bloquées par Google et DuckDuckGo que celles d'une connexion personnelle. Sans moteur à clé d'API, des recherches s'arrêteront sur « moteur de recherche indisponible ». Options : Brave Search API ou un service équivalent (offre et prix à vérifier). Cette décision ne bloque pas les étapes 1 à 9.
2. **Région AWS** : la plus proche des utilisateurs, parmi celles qui proposent l'instance GPU retenue.
3. **Instance** : `g4dn.xlarge` (la moins chère) ou `g6.xlarge` (plus rapide, plus de mémoire). À trancher après un test de vitesse sur l'instance.
4. **Nom de domaine** pour le certificat HTTPS : un domaine existant, ou un nouveau dans Route 53.
5. **Horaires d'ouverture et fuseau horaire** pour l'arrêt et le démarrage planifiés.
6. **Load balancer ou Caddy** : sécurité et simplicité contre une cinquantaine de dollars par mois d'économie (section 1).
