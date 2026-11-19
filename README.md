# Research-Agent

Agent autonome de recherche documentaire : il reçoit une question, recherche des sources, les compare, vérifie les affirmations et rédige une synthèse référencée. Après chaque tour de recherche, **il décide lui-même** si les informations sont suffisantes ou s'il faut chercher davantage, et il explique pourquoi.

Tout fonctionne en local : un **modèle open source** (`qwen3:8b` via Ollama) et un moteur de recherche auto-hébergé (**SearXNG**). Le plan de réalisation, étape par étape avec les mesures, est dans [ETAPES_PROJET.md](ETAPES_PROJET.md).

## Fonctionnement

```
Question
  → plan : 2 à 4 sous-questions + requêtes
  → [ tour de recherche : SearXNG → sélection des sources → lecture → extraction des affirmations
      → évaluation par sous-question (avis du modèle + règles du code) → continuer ou s'arrêter ] × 3 au plus
  → vérification : corroboration, vérification des affirmations fragiles, divergences entre sources
  → synthèse : réponse courte, développement, divergences, limites, sources numérotées
```

C'est **le code qui pilote** ; le modèle exécute des tâches courtes au format JSON validé. Le code a le dernier mot sur ce qui compte :

- une sous-question n'est couverte que si **au moins 2 domaines fiables distincts** l'appuient (sites marchands et blogs exclus) ;
- l'agent s'arrête si tout est couvert, après 3 tours, en cas de stagnation, ou si le moteur de recherche est en panne (l'arrêt est alors signalé comme tel) ;
- une affirmation est **corroborée** si au moins 2 origines indépendantes la rapportent (une dépêche reprise par deux journaux compte une fois) ;
- chaque référence du rapport désigne une source réellement lue : les identifiants inventés par le modèle sont retirés.

## Prérequis

- Python ≥ 3.11 et [uv](https://docs.astral.sh/uv/)
- [Ollama](https://ollama.com) avec les modèles :
  ```bash
  ollama pull qwen3:8b
  ollama pull nomic-embed-text
  ```
- Docker et `docker-compose` (pour SearXNG)
- Un GPU avec ~8 Go de VRAM (testé sur RTX 2070 Super) ; contexte réglé à 8192 tokens.

## Installation

```bash
uv venv .venv && source .venv/bin/activate
uv pip install -e ".[dev]"

# Clé secrète de SearXNG (fichier .env, exclu de git)
echo "SEARXNG_SECRET=$(python -c 'import secrets; print(secrets.token_hex(32))')" > .env
docker-compose up -d

python scripts/check_infra.py      # vérifie recherche, lecture, modèle et embeddings
```

> Au démarrage, le conteneur SearXNG s'attribue le dossier `searxng/`. Pour modifier `searxng/settings.yml` ensuite :
> `docker cp settings.yml research_agent_searxng:/etc/searxng/settings.yml && docker-compose restart searxng`.

## Utilisation

```bash
python -m research_agent "Le glyphosate est-il cancérogène pour l'humain ?"
```

Options : `--max-rounds N`, `--no-verify` (plus rapide), `--no-synthesis` (rapport brut seul), `--model NOM`, `--no-cache`, `-v`.

Trois fichiers sont produits dans `reports/` :

| Fichier | Contenu |
|---|---|
| `<date>-<question>.md` | **Rapport** : réponse courte, développement par sous-question, points de divergence, limites, sources numérotées |
| `<date>-<question>.details.md` | Détail : toutes les affirmations avec leur statut, les décisions de l'agent tour par tour, le tableau des sources |
| `<date>-<question>.trace.json` | Trace complète (plan, sources, affirmations, chaque décision), pour analyse |

Durée mesurée : **environ 6 à 8 minutes par question** (un à trois tours, vérification et synthèse comprises).

### Limite : les moteurs de recherche gratuits

SearXNG interroge des moteurs publics. Google, Brave, DuckDuckGo et Qwant **bloquent l'adresse IP** après un usage intensif (CAPTCHA, « unusual traffic »), parfois pendant des heures. Trois parades sont en place :

- **d'autres moteurs généralistes**, moins sollicités : Yahoo et Mojeek, activés dans [searxng/settings.yml](searxng/settings.yml) ;
- **un secours scientifique** : si aucun moteur généraliste ne répond, l'agent interroge la catégorie `science` (arXiv, Crossref, OpenAlex, Semantic Scholar, PubMed), dont les API ne bloquent pas de cette façon. Réglage : `RA_SEARCH_CATEGORIES` et `RA_SEARCH_FALLBACK_CATEGORIES` ;
- **des requêtes espacées de 2 s** et mises en cache dans `.cache/`.

Si tout est bloqué malgré tout, l'agent s'arrête avec le motif « moteur de recherche indisponible », le rapport l'indique et la commande renvoie le code 2. Pour diagnostiquer :

```bash
curl -s "localhost:8080/search?q=test&format=json" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d['results']), d['unresponsive_engines'])"
```

Pour un usage soutenu, branchez un moteur avec clé d'API derrière l'interface `SearchProvider` de [search.py](research_agent/search.py).

- **Bing** est désactivé : via SearXNG, il renvoie des résultats sans rapport quand les autres moteurs bloquent.
- **Wikipédia et Wikidata** exigent une adresse de contact (politique Wikimedia). Pour les activer, ajoutez `SEARXNG_CONTACT_URL=mailto:…` dans l'`environment` du service `searxng` de `docker-compose.yml`.

## Interface web (FastAPI)

```bash
uv pip install -e ".[web]"
python -m research_agent.web                 # http://127.0.0.1:8000
```

L'interface permet de poser une question, de suivre la recherche en direct (plan, sources lues, décisions de l'agent), puis de lire le rapport, dont les références `[n]` renvoient à la liste des sources. Elle permet aussi de télécharger le rapport, le détail et la trace, et de retrouver les recherches passées dans l'historique. L'API est documentée sur `/api/docs`.

**Déploiement avec Docker** (le modèle reste servi par Ollama sur l'hôte, avec le GPU) :

```bash
echo "RA_WEB_PASSWORD=$(python -c 'import secrets; print(secrets.token_urlsafe(18))')" >> .env
docker-compose up -d --build                 # SearXNG + service web
```

Choix de conception :

| Point | Choix | Raison |
|---|---|---|
| Exécution | une file d'attente, **une recherche à la fois** | un seul GPU de 8 Go : deux recherches simultanées le satureraient |
| Persistance | SQLite dans `data/` (recherches, progression), rapports dans `data/reports/<id>/` | survit aux redémarrages ; une recherche coupée par un arrêt est marquée en échec, celles en attente reprennent |
| Progression | Server-Sent Events (`/api/jobs/{id}/events`) | flux unidirectionnel simple ; reprise automatique après une coupure réseau |
| Sécurité | authentification HTTP Basic dès que `RA_WEB_PASSWORD` est défini ; **refus de démarrer hors de 127.0.0.1 sans mot de passe** | le service lance des requêtes web et occupe le GPU |
| | Markdown rendu côté serveur sans HTML brut, CSP stricte, aucun script externe | les rapports contiennent du texte venu de pages web |
| | file d'attente bornée (`RA_MAX_QUEUE`, réponse 429 au-delà), question de 8 à 500 caractères | éviter l'engorgement |
| Interface | HTML, CSS et JS sans framework ni CDN, servis par FastAPI | rien à compiler, fonctionne hors ligne |

Pour un accès depuis le réseau, placez de préférence le service derrière un proxy inverse avec HTTPS (Caddy, nginx). Pour un préfixe d'URL, utilisez `--root-path /agent`. Le proxy ne doit pas mettre en tampon les réponses `text/event-stream`.

## Configuration

Tous les réglages sont dans [research_agent/config.py](research_agent/config.py) ; les principaux se surchargent par variable d'environnement :

| Variable | Défaut | Rôle |
|---|---|---|
| `RA_MODEL` | `qwen3:8b` | modèle Ollama |
| `RA_NUM_CTX` | `8192` | contexte (doit tenir entièrement dans la VRAM) |
| `RA_USER_AGENT` | générique | ajoutez un contact pour lire Wikipédia (politique Wikimedia) |
| `RA_SEARXNG_URL` | `http://localhost:8080` | instance SearXNG |
| `RA_WEB_PASSWORD` | vide | mot de passe de l'interface web (utilisateur `RA_WEB_USER`, défaut `admin`) |
| `RA_WEB_HOST` | `127.0.0.1` | adresse d'écoute du conteneur web (`0.0.0.0` exige un mot de passe) |
| `RA_DATA_DIR` | `data` | base SQLite et rapports du service web |
| `RA_MAX_QUEUE` | `10` | recherches en attente au maximum |

## Tests et évaluation

```bash
pytest                                   # tests unitaires, sans réseau ni modèle
python scripts/evaluate.py --smoke       # une question par catégorie, avec mesures
python scripts/evaluate.py --model llama3.1:8b --smoke   # comparer un autre modèle
python scripts/evaluate.py                       # les 30 questions (~3 h 30)
python scripts/evaluate.py --resume eval/runs/<dossier>  # reprendre après une panne des moteurs
```

Les questions interrompues par une panne des moteurs de recherche sont exclues des moyennes. Après 3 pannes consécutives, l'évaluation s'arrête ; `--resume` relance ensuite les seules questions manquantes. Le récapitulatif donne les moyennes globales et par catégorie.

Le jeu de questions est dans [eval/questions.yaml](eval/questions.yaml) ; les résultats vont dans `eval/runs/`.

## Structure

| Module | Rôle |
|---|---|
| `agent.py` | orchestrateur : plan, tours de recherche, boucle de décision |
| `sufficiency.py` | décision « suffisant ou recherche complémentaire » (fonctions pures) |
| `verify.py` | corroboration, vérification des affirmations fragiles, divergences entre sources |
| `synthesis.py` | rédaction par sous-question, contrôle et renumérotation des références |
| `search.py`, `fetch.py`, `embeddings.py` | recherche SearXNG, lecture des pages, embeddings (déterministes) |
| `llm.py`, `schemas.py`, `prompts.py` | client Ollama (JSON contraint, nouvel essai, garde de contexte), schémas, consignes |
| `state.py`, `report.py` | état de la recherche, rapports et trace |
| `web/` | service FastAPI : file d'attente persistée (`jobs.py`), progression (`progress.py`), API et interface (`app.py`, `static/`) |
