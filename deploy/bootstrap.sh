#!/usr/bin/env bash
# Premier démarrage (et remise en état) de Research-Agent sur l'instance EC2.
#
#   sudo deploy/bootstrap.sh <tag>      tag = SHA court des images poussées dans ECR
#
# Le script peut être relancé sans risque : chaque étape vérifie d'abord ce qui existe.
#   1. trouve le volume EBS de données, le formate la PREMIÈRE fois seulement, le monte sur /data ;
#   2. lit les secrets dans Secrets Manager et écrit /etc/research-agent/.env (mode 600) ;
#   3. se connecte à ECR et lance les conteneurs (journaux vers CloudWatch) ;
#   4. télécharge les modèles Ollama sur /data (une seule fois) ;
#   5. attend que l'application réponde.
set -euo pipefail

TAG="${1:?usage : sudo $0 <tag des images>}"

REGION="ca-central-1"
ACCOUNT_ID="034565845674"
PREFIX="research-agent-prod"
REGISTRY="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"
SECRETS=(RA_WEB_PASSWORD SEARXNG_SECRET RA_USER_AGENT)
MODELS=(qwen3:8b nomic-embed-text)

DATA=/data
ENV_DIR=/etc/research-agent
ENV_FILE="${ENV_DIR}/.env"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
COMPOSE=(docker compose --env-file "$ENV_FILE" -p research-agent
         -f "$REPO_DIR/deploy/docker-compose.prod.yml"
         -f "$REPO_DIR/deploy/docker-compose.aws.yml")

log() { printf '\n==> %s\n' "$*"; }
die() { printf 'ERREUR : %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "lancer avec sudo"

# ---------- 1. Volume de données ----------
log "Volume de données"
# Les disques EBS apparaissent dans /dev/disk/by-id sous le nom « Amazon_Elastic_Block_Store_vol… ».
# Le disque local de la g4dn (éphémère) n'y figure pas. On écarte le disque système.
root_disk="/dev/$(lsblk -no PKNAME "$(findmnt -no SOURCE /)")"
candidates=()
for link in /dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_vol*; do
  [[ "$link" == *-part* ]] && continue
  dev="$(readlink -f "$link")"
  [ "$dev" = "$root_disk" ] && continue
  candidates+=("$dev")
done
[ "${#candidates[@]}" -eq 1 ] || die "attendu 1 volume EBS de données, trouvé ${#candidates[@]} : ${candidates[*]:-aucun}"
data_dev="${candidates[0]}"
echo "Volume : $data_dev"

if [ -z "$(blkid -o value -s TYPE "$data_dev" || true)" ]; then
  echo "Aucun système de fichiers : formatage (première utilisation)"
  mkfs.ext4 -q -L ra-data "$data_dev"
fi

uuid="$(blkid -o value -s UUID "$data_dev")"
mkdir -p "$DATA"
if ! grep -q "$uuid" /etc/fstab; then
  # nofail : l'instance démarre même si le volume manque (on peut alors réparer par SSM).
  echo "UUID=$uuid $DATA ext4 defaults,nofail 0 2" >> /etc/fstab
fi
mountpoint -q "$DATA" || mount "$DATA"
mkdir -p "$DATA/ollama" "$DATA/web"
chown 1000:1000 "$DATA/web"   # le conteneur web tourne sous l'utilisateur « agent » (uid 1000)
df -h "$DATA" | tail -1

# ---------- 2. Secrets ----------
log "Secrets"
install -d -m 700 "$ENV_DIR"
tmp="$(mktemp "$ENV_DIR/.env.XXXXXX")"   # créé en mode 600
trap 'rm -f "$tmp"' EXIT
{
  echo "# Écrit par bootstrap.sh le $(date -Is). Ne pas modifier à la main."
  echo "WEB_IMAGE=${REGISTRY}/research-agent-web:${TAG}"
  echo "SEARXNG_IMAGE=${REGISTRY}/research-agent-searxng:${TAG}"
  echo "DATA_ROOT=${DATA}"
  echo "WEB_BIND=0.0.0.0"   # le load balancer atteint le port 8000 ; le groupe de sécurité filtre
  echo "RA_WEB_USER=admin"
  for name in "${SECRETS[@]}"; do
    value="$(aws secretsmanager get-secret-value --region "$REGION" \
      --secret-id "${PREFIX}/${name}" --query SecretString --output text)" \
      || die "secret ${PREFIX}/${name} illisible (existe-t-il une valeur ?)"
    [[ "$value" == *"'"* || "$value" == *$'\n'* ]] && die "le secret $name contient une apostrophe ou un saut de ligne"
    # Apostrophes simples : la valeur est prise telle quelle par Docker Compose et par bash.
    echo "${name}='${value}'"
  done
} > "$tmp"
mv "$tmp" "$ENV_FILE"
trap - EXIT
chmod 600 "$ENV_FILE"
echo "Écrit : $ENV_FILE ($(grep -c '=' "$ENV_FILE") variables)"

# ---------- 3. Conteneurs ----------
log "Connexion à ECR"
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$REGISTRY"

log "Téléchargement des images et démarrage"
"${COMPOSE[@]}" pull
"${COMPOSE[@]}" up -d

# ---------- 4. Modèles ----------
log "Modèles Ollama"
for _ in $(seq 1 30); do
  "${COMPOSE[@]}" exec -T ollama ollama list >/dev/null 2>&1 && break
  sleep 2
done
for model in "${MODELS[@]}"; do
  if "${COMPOSE[@]}" exec -T ollama ollama list | awk 'NR>1 {print $1}' | grep -qx "${model}\(:latest\)\?"; then
    echo "$model : déjà présent"
  else
    echo "$model : téléchargement (plusieurs minutes la première fois)"
    "${COMPOSE[@]}" exec -T ollama ollama pull "$model"
  fi
done

# ---------- 5. Vérification ----------
log "Attente de l'application"
for _ in $(seq 1 60); do
  if curl -fsS --max-time 3 http://127.0.0.1:8000/api/health/live >/dev/null; then
    "${COMPOSE[@]}" ps
    echo
    echo "Research-Agent répond. Test complet : curl -u admin:… http://127.0.0.1:8000/api/health"
    exit 0
  fi
  sleep 2
done
"${COMPOSE[@]}" ps
die "l'application ne répond pas : voir « ${COMPOSE[*]} logs web »"
