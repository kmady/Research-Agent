# Agent autonome de recherche documentaire — Étapes de réalisation

> **Objectif :** un agent qui reçoit une question, recherche des sources, les compare, vérifie les affirmations et rédige une synthèse référencée.
> **Aspect agentique :** après chaque tour de recherche, l'agent décide lui-même si les informations recueillies sont suffisantes ou s'il faut une recherche complémentaire.
> **Contrainte :** modèle open source exécuté en local.

---

## Sommaire

1. [Choix techniques retenus](#1-choix-techniques-retenus)
2. [Principe de fonctionnement](#2-principe-de-fonctionnement)
3. [Structure du projet](#3-structure-du-projet)
4. [Étape 0 — Préparer l'environnement](#étape-0--préparer-lenvironnement)
5. [Étape 1 — Infrastructure : modèle, recherche, embeddings](#étape-1--infrastructure--modèle-recherche-embeddings)
6. [Étape 2 — Outils déterministes : recherche et lecture](#étape-2--outils-déterministes--recherche-et-lecture)
7. [Étape 3 — Client LLM et sorties structurées](#étape-3--client-llm-et-sorties-structurées)
8. [Étape 4 — Un tour complet sans boucle](#étape-4--un-tour-complet-sans-boucle)
9. [Étape 5 — La boucle agentique : juger la suffisance](#étape-5--la-boucle-agentique--juger-la-suffisance)
10. [Étape 6 — Vérification croisée des affirmations](#étape-6--vérification-croisée-des-affirmations)
11. [Étape 7 — Synthèse référencée](#étape-7--synthèse-référencée)
12. [Étape 8 — Évaluation et réglages](#étape-8--évaluation-et-réglages)
13. [Questions ouvertes](#questions-ouvertes)

---

## 1. Choix techniques retenus

| Élément | Choix | Raison |
|---|---|---|
| Matériel | RTX 2070 Super (8 Go VRAM), 31 Go RAM, WSL2 | Machine existante |
| Serveur de modèle | **Ollama** (déjà installé, v0.40.2) | Simple, compatible avec Turing, quantification, schéma JSON natif |
| Modèle principal | **`qwen3:8b`** (Q4_K_M, ~5 Go) | Bon en français, JSON fiable, mode « réflexion » activable |
| Modèle de comparaison | `llama3.1:8b` ou Ministral 8B | Vérifier le choix sur nos propres questions |
| Embeddings | **`nomic-embed-text`** (déjà installé) | Classer les passages avant de les envoyer au LLM |
| Recherche web | **SearXNG** auto-hébergé (Docker) | Open source, gratuit, sans clé d'API *(à confirmer)* |
| Lecture des pages | `httpx` + `trafilatura` (+ `pypdf`) | Extraction propre du texte principal |
| Validation | `pydantic` | Valider chaque sortie JSON du modèle |
| Tests | `pytest` | Tester la logique sans appeler le modèle |

**Pourquoi pas vLLM ?** La 2070 Super (architecture Turing) est mal prise en charge par vLLM, et 8 Go de VRAM c'est trop peu pour lui.

**Contraintes à garder en tête (liées aux 8 Go) :**
- contexte utilisable d'environ **8k à 16k tokens**, à mesurer à l'étape 1 ;
- on n'envoie **jamais une page entière** au modèle, seulement des passages sélectionnés ;
- un modèle 8B juge moins finement : **les règles en code ont le dernier mot**.

---

## 2. Principe de fonctionnement

C'est **le code qui pilote la boucle**. Le modèle est appelé pour des tâches courtes et précises : planifier, sélectionner, extraire, juger, rédiger. La recherche et la lecture des pages sont du code déterministe.

```
Question
   │
   ▼
[1] PLANIFIER (LLM, JSON) ─────────► sous-questions Q1..Qn + requêtes initiales
   │
   ▼
┌─► [2a] RECHERCHER (code : SearXNG) ► URLs + extraits courts
│   [2b] SÉLECTIONNER (LLM, JSON) ───► URLs qui méritent d'être lues
│   [2c] LIRE (code) ────────────────► texte découpé en passages, classés par embeddings
│   [3]  EXTRAIRE (LLM, JSON) ───────► affirmations C1..Cn liées aux passages [S3:p2]
│   [4]  SUFFISANCE (LLM + règles) ──► continuer ? lacunes ? nouvelles requêtes ?
│          │
└──────────┘ non (et budget restant)
   │ oui (ou budget épuisé)
   ▼
[5] VÉRIFIER (recherche ciblée + LLM) ► confirmée / contestée / invérifiable
   │
   ▼
[6] RÉDIGER (LLM) ───────────────────► rapport Markdown + références [1], [2]… + « Limites »
```

**L'état vit dans le code, pas dans une conversation :**
- un **registre de sources** dédupliqué (S1, S2…), avec domaine, type et fiabilité ;
- des **passages** numérotés par source (S3:p1, S3:p2…) ;
- des **affirmations** liées à leurs passages, avec un statut ;
- un **journal des décisions** (trace JSON) qui explique pourquoi l'agent a continué ou s'est arrêté.

---

## 3. Structure du projet

```
Research-Agent/
├── ETAPES_PROJET.md        # ce document
├── README.md               # installation et utilisation
├── pyproject.toml          # dépendances
├── docker-compose.yml      # SearXNG
├── searxng/
│   └── settings.yml        # active le format JSON de SearXNG
├── research_agent/
│   ├── __init__.py
│   ├── config.py           # modèles, URLs des serveurs, seuils, budgets
│   ├── schemas.py          # modèles Pydantic des sorties JSON
│   ├── state.py            # registre de sources, passages, affirmations, trace
│   ├── llm.py              # client Ollama : chat, JSON contraint, nouvel essai
│   ├── embeddings.py       # embeddings Ollama (nomic-embed-text, préfixes de tâche)
│   ├── cache.py            # cache disque clé → JSON
│   ├── search.py           # SearchProvider (SearXNG) + cache
│   ├── fetch.py            # téléchargement, extraction de texte, découpage, classement
│   ├── prompts.py          # consignes de chaque étape
│   ├── sufficiency.py      # règles dures + décision continuer / arrêter
│   ├── agent.py            # orchestrateur
│   ├── report.py           # rendu Markdown, contrôle des références
│   └── __main__.py         # ligne de commande
├── scripts/
│   └── check_infra.py      # vérification de l'étape 1
├── tests/
│   ├── test_state.py
│   ├── test_fetch.py
│   ├── test_sufficiency.py
│   └── test_report.py
├── eval/
│   └── questions.yaml      # questions de test (étape 8)
└── reports/                # rapports .md + traces .json générés
```

---

## Étape 0 — Préparer l'environnement

**But :** un environnement Python propre avec les bonnes dépendances.

1. Supprimer l'ancien `.venv/` (il contient le SDK `anthropic`, qui n'est plus utile) puis le recréer :
   ```bash
   cd ~/workspace/Research-Agent
   rm -rf .venv
   uv venv .venv
   source .venv/bin/activate
   ```
2. Créer `pyproject.toml` avec les dépendances :
   `httpx`, `trafilatura`, `pypdf`, `pydantic`, `pyyaml`, `numpy`, et `pytest` en développement.
3. Installer :
   ```bash
   uv pip install -e ".[dev]"
   ```
4. Initialiser git (recommandé) :
   ```bash
   git init && echo -e ".venv/\nreports/\n.cache/\n__pycache__/" > .gitignore
   ```

**✅ Terminé quand :** `python -c "import httpx, trafilatura, pydantic"` fonctionne.

---

## Étape 1 — Infrastructure : modèle, recherche, embeddings

**But :** vérifier que chaque brique fonctionne seule avant d'écrire l'agent.

### 1.1 Télécharger le modèle

```bash
ollama pull qwen3:8b
ollama list            # vérifier qu'il apparaît (~5 Go)
```

> Regarder s'il existe une version plus récente de Qwen adaptée à 8 Go avant de télécharger.

### 1.2 Mesurer la vitesse et le contexte

- Lancer une requête simple et noter la vitesse (tokens/seconde) affichée par `ollama run qwen3:8b --verbose`.
- Tester plusieurs valeurs de contexte (`num_ctx` : 8192, 12288, 16384) et surveiller la VRAM avec `nvidia-smi`.
- **Retenir la plus grande valeur qui tient entièrement dans le GPU.** Si le modèle déborde sur le CPU, la vitesse chute fortement.
- Optionnel : la quantification du cache KV (`q8_0`) permet d'agrandir le contexte. Elle se règle dans la configuration du service Ollama.

### 1.3 Lancer SearXNG

1. Créer `searxng/settings.yml` en activant la sortie JSON (indispensable pour l'agent) :
   ```yaml
   use_default_settings: true
   server:
     secret_key: "à-remplacer-par-une-valeur-aléatoire"
     limiter: false
   search:
     formats:
       - html
       - json
   ```
2. Créer `docker-compose.yml` avec le service `searxng/searxng`, exposé sur le port `8080`, et le dossier `searxng/` monté dans `/etc/searxng`.
3. Lancer (sur cette machine, c'est `docker-compose` v1 qui est installé) :
   ```bash
   docker-compose up -d
   curl "http://localhost:8080/search?q=test&format=json" | head -c 500
   ```

### 1.4 Script de vérification `scripts/check_infra.py`

Ce script doit, l'un après l'autre :
1. faire une recherche SearXNG et afficher 3 résultats ;
2. télécharger une page et afficher les 500 premiers caractères du texte extrait ;
3. demander à `qwen3:8b` une réponse JSON conforme à un petit schéma (paramètre `format` de l'API `/api/chat`) et la valider avec Pydantic ;
4. calculer l'embedding d'une phrase avec `nomic-embed-text` (API `/api/embed`) et afficher sa dimension.

**✅ Terminé quand :** les 4 vérifications passent, et la vitesse et le `num_ctx` retenus sont notés dans `config.py`.

> **Résultats mesurés (17/11/2026) :**
> - `qwen3:8b` : environ **46–49 tokens/s** en génération ; ~36 s au premier appel (chargement), ~6 s ensuite.
> - Contexte : **8192 tokens tiennent à 100 % en VRAM** (5,76 Go) ; à 12288, seuls 87 % tiennent → `num_ctx = 8192` retenu.
> - SearXNG : OK (40+ résultats par requête). Wikipédia renvoie 403 avec l'user-agent générique : la politique Wikimedia exige un user-agent avec un contact (`RA_USER_AGENT`).
> - `nomic-embed-text` : vecteurs de dimension 768.

---

## Étape 2 — Outils déterministes : recherche et lecture

**But :** du code fiable et testé, sans LLM.

### 2.1 `state.py` — registre de sources

- **Normalisation des URLs** pour dédupliquer : hôte en minuscules, sans fragment `#`, sans paramètres de suivi (`utm_*`, `fbclid`…), sans `/` final.
- **Identifiants stables** S1, S2… dans l'ordre de découverte.
- Pour chaque source : URL, titre, domaine (sans `www.`), tour de découverte, type, fiabilité, date de publication.
- Méthode `catalog()` qui produit une liste lisible pour le modèle : `[S3] Titre — url (domaine, type, fiabilité)`.

### 2.2 `search.py` — recherche

- Interface `SearchProvider` avec une méthode `search(query, n) -> list[SearchHit]`, pour pouvoir changer de moteur plus tard.
- Implémentation `SearxngProvider` (appel `GET /search?format=json`).
- Cache disque (`.cache/search/`) indexé par la requête, pour ne pas relancer les mêmes recherches pendant le développement.

### 2.3 `fetch.py` — lecture des pages

- Téléchargement avec `httpx` : délai maximal (~15 s), redirections suivies, taille maximale, user-agent générique, respect de `robots.txt`.
- Extraction du texte principal avec `trafilatura`, et `pypdf` pour les PDF.
- **Découpage en passages** d'environ 400 à 500 tokens, en coupant sur les paragraphes.
- **Classement des passages** par similarité (embedding `nomic-embed-text`) avec la sous-question ; on garde les 4 à 6 meilleurs.
- Cache disque (`.cache/pages/`).

### 2.4 Tests

- `test_state.py` : deux URLs équivalentes donnent la même source ; les identifiants sont stables.
- `test_fetch.py` : le découpage ne coupe pas au milieu d'un mot et respecte la taille ; le classement renvoie le bon nombre de passages (avec de faux embeddings).

**✅ Terminé quand :** `pytest` passe, et un petit script cherche une requête, lit 3 pages et affiche les passages les mieux classés.

> **Résultat (17/11/2026) :** 26 tests passent. `scripts/demo_step2.py` sur « effets de la caféine sur le sommeil » : 8 sources, 3 pages lues, 19 passages, meilleurs passages pertinents (score cosinus 0,75–0,83), **~10 s** au total.
> Choix faits en cours de route : les embeddings sont dans `embeddings.py` (et non `llm.py`) ; un fragment trop court est **fusionné** au passage précédent plutôt que supprimé, pour ne jamais perdre de contenu ; un `robots.txt` indisponible (4xx/5xx) vaut « pas de restriction », comme le prévoit la RFC 9309.

---

## Étape 3 — Client LLM et sorties structurées

**But :** une couche unique pour parler au modèle, avec du JSON toujours valide.

### 3.1 `llm.py`

- `chat(messages, think: bool) -> str` : appel à `/api/chat` avec `num_ctx` réglé et la réflexion activée ou non.
- `structured(messages, schema: type[BaseModel], think: bool) -> BaseModel` :
  1. envoie `format = schema.model_json_schema()` à Ollama (décodage contraint) ;
  2. valide la réponse avec Pydantic ;
  3. en cas d'échec, **un seul nouvel essai** avec l'erreur de validation dans le message ;
  4. sinon, lève une exception claire.
- `embed(texts) -> list[list[float]]` avec `nomic-embed-text`.
- Compteurs : nombre d'appels, tokens, durée (pour l'évaluation).

### 3.2 `schemas.py` — des schémas petits et plats

| Schéma | Champs principaux |
|---|---|
| `ResearchPlan` | `sub_questions[{id, question}]`, `initial_queries[]` |
| `UrlSelection` | `selected[{url, reason}]` |
| `Extraction` | `claims[{statement, sub_question_id, passage_ids[], quote}]`, `source_kind`, `reliability`, `publication_date` |
| `SubQuestionAssessment` | `status` (covered / partial / missing), `missing_aspects[]`, `follow_up_queries[]` |
| `ClaimVerdict` | `verdict` (confirmed / contested / unverifiable), `supporting[]`, `contradicting[]`, `note` |

> Avec un modèle 8B, **éviter les structures imbriquées sur plusieurs niveaux** et les listes de listes.

### 3.3 Réflexion activée ou coupée

| Étape | Réflexion | Raison |
|---|---|---|
| Plan | coupée | tâche simple |
| Sélection d'URLs | coupée | nombreuse, doit être rapide |
| Extraction | coupée | nombreuse, tâche cadrée |
| Suffisance | **activée** | décision clé de l'agent |
| Vérification | **activée** | jugement fin |
| Synthèse | **activée** | qualité de rédaction |

**✅ Terminé quand :** chaque schéma est testé une fois sur un exemple réel et la validation passe.

> **Résultat (18/11/2026) :** 39 tests passent. `scripts/demo_step3.py` (caféine et sommeil) : **les 5 schémas sont valides du premier coup**, 0 nouvel essai, **41 s** pour 5 appels (4–11 s par appel ; ~5 900 tokens en entrée, ~1 900 en sortie).
> - Ollama accepte le JSON contraint **avec ou sans réflexion**, et les schémas Pydantic tels quels (`$defs`).
> - Le schéma est aussi inclus dans la consigne : le paramètre `format` contraint la sortie, mais le modèle ne le « voit » pas.
> - Ollama tronque silencieusement un message trop long : `llm.py` refuse l'appel avant (`ContextOverflowError`). Budget du message : 6 144 tokens sans réflexion, 4 096 avec. L'estimation (3 caractères/token) est prudente.
> - **À reprendre dans les consignes (étapes 4–5) :** les requêtes complémentaires sont formulées comme des questions et non comme des mots-clés ; la sélection a placé en tête un site marchand (complément alimentaire) ; le modèle a tendance à juger « partielle » une sous-question en inventant des aspects manquants (les règles en code devront trancher) ; les citations (`quote`) doivent être vérifiées par le code contre le passage.

---

## Étape 4 — Un tour complet sans boucle

**But :** produire un premier rapport brut de bout en bout, sans décision agentique.

Dans `agent.py`, enchaîner une seule fois :
1. **Plan :** la question donne 2 à 4 sous-questions et 3 à 5 requêtes.
2. **Recherche :** chaque requête passe par SearXNG (5 à 8 résultats), puis on fusionne et on déduplique.
3. **Sélection :** le LLM choisit 5 à 8 URLs à lire, à partir des titres et des extraits.
4. **Lecture :** téléchargement, découpage, classement des passages par sous-question.
5. **Extraction, source par source :** un appel LLM par source, avec seulement ses meilleurs passages. Le code ajoute les affirmations à l'état et **rejette les `passage_ids` qui n'existent pas**.
6. **Rapport brut :** liste des affirmations par sous-question, avec leurs sources.

Ligne de commande (`__main__.py`) :
```bash
python -m research_agent "Ma question" --max-rounds 1 --out reports/
```

**✅ Terminé quand :** 2 ou 3 questions variées produisent un rapport brut cohérent, et chaque affirmation pointe vers un vrai passage.

> **Résultat (18/11/2026) :** 49 tests passent. Trois questions réelles, un tour chacune :
>
> | Question | Durée | Sources lues | Affirmations | Citations vérifiées |
> |---|---|---|---|---|
> | Effets de la caféine sur le sommeil | 82 s | 4 | 27 | 22 |
> | Causes du déclin des abeilles | 95 s | 5 | 36 | 32 |
> | Fission vs fusion (en anglais) | 94 s | 6 | 44 | 43 |
>
> Aucun nouvel essai JSON ; chaque affirmation pointe vers un passage existant (les identifiants inventés sont retirés par le code, et les citations non retrouvées dans le passage sont effacées).
> Corrections faites après les essais réels :
> - SearXNG renvoyait parfois des liens sans hôte (`http:///go/v1/...`) : filtrés dans `search.py` ;
> - réseaux sociaux et sites vidéo exclus par le code (`blocked_domains`), le modèle ayant sélectionné Facebook ;
> - sources `company` et `blog` forcées à la fiabilité `low` (le modèle l'oubliait) ;
> - limite de taille relevée à 15 Mo (une thèse en PDF était refusée) ;
> - requêtes nettoyées (« ? » final, mot « english » ajouté par le modèle).
>
> **Points à traiter plus tard :** le plan peut présupposer la réponse (abeilles : sous-questions sur des causes supposées) ; certaines affirmations sont rattachées à la mauvaise sous-question ; plusieurs pages d'un même domaine peuvent être lues dans le même tour.

---

## Étape 5 — La boucle agentique : juger la suffisance

**But :** l'agent décide seul s'il continue à chercher. **C'est le cœur du projet.**

### 5.1 Mesures calculées par le code (faits objectifs)

Pour chaque sous-question :
- nombre d'affirmations ;
- nombre de **domaines différents** qui les appuient ;
- nombre de sources de fiabilité « haute » ;
- nouvelles sources trouvées pendant ce tour.

### 5.2 Jugement du LLM (un appel par sous-question)

On lui donne la sous-question, ses affirmations et les mesures ci-dessus. Il renvoie : `covered` / `partial` / `missing`, les aspects manquants et des requêtes complémentaires.

### 5.3 Décision dans `sufficiency.py` (fonction pure, testée)

```
SUFFISANT si, pour chaque sous-question :
    - le LLM la juge « covered »
    - ET elle est appuyée par au moins 2 domaines différents
  ET le nombre total de sources ≥ seuil minimum

SINON on continue avec les requêtes proposées (en retirant celles déjà lancées)
       pour les sous-questions non couvertes,

SAUF si une condition d'arrêt est atteinte :
    - nombre maximal de tours atteint (par défaut 3)
    - stagnation : aucune nouvelle source au dernier tour
    - plus aucune requête nouvelle à lancer
  → on s'arrête et on marque le rapport « couverture incomplète »
```

Les seuils (domaines minimum, sources minimum, tours maximum) se règlent dans `config.py`.

### 5.4 Journal des décisions

À chaque tour, on enregistre dans la trace : les mesures, le jugement du LLM, la décision, sa raison et les requêtes lancées. La trace est exportée en `reports/<question>.trace.json`.

### 5.5 Tests (`test_sufficiency.py`)

- Tout est couvert avec 2 domaines → arrêt, suffisant.
- Le LLM dit « covered » mais un seul domaine → on continue.
- Le dernier tour n'a apporté aucune nouvelle source → arrêt, insuffisant.
- Le nombre maximal de tours est atteint → arrêt, insuffisant.
- Les requêtes déjà lancées ne sont pas relancées.

**✅ Terminé quand :** les tests passent ; une question facile s'arrête au tour 1 ; une question pointue lance au moins un tour complémentaire, et la trace explique pourquoi.

> **Résultat (18/11/2026) :** 67 tests passent (dont 15 pour `sufficiency.py` et 4 pour la boucle).
>
> | Question | Tours | Décision | Durée |
> |---|---|---|---|
> | Effets de la caféine sur le sommeil | 1 | suffisant (4 sous-questions, 4–5 domaines fiables chacune) | ~110 s |
> | Loi Climat et Résilience en Bretagne | 1 | suffisant (3 sous-questions, 4 domaines fiables) | ~150 s |
> | Semaine de 4 jours dans les collectivités | **2** | tour 1 : Q4 « partielle » → 2 requêtes ciblées → tour 2 : Q4 couverte → suffisant | 242 s |
>
> Règles ajoutées après les essais réels :
> - **seuls les domaines fiables comptent** (fiabilité medium/high) : un site marchand ou un blog ne corrobore rien ;
> - **la « preuve forte » ne joue qu'à partir du tour 2** : au tour 1, un avis « partial » déclenche toujours une recherche ciblée (elle avait accepté trop tôt une sous-question au premier essai) ;
> - **au plus 2 pages par domaine** sur toute la recherche (un même site avait été lu 3 fois dans un tour) ;
> - une sous-question couverte le reste : elle n'est plus réévaluée (gain de temps) ;
> - au tour suivant, la sélection et le choix des passages se concentrent sur les sous-questions non couvertes.
>
> **Points à traiter plus tard (étape 8) :** le type de source est souvent mal classé (un média comme Euronews ou un site de notaire classés « academic »/« official, high ») ; les requêtes complémentaires restent longues ; une source déjà trouvée au tour 1 et retrouvée au tour 2 n'est pas reconnue comme « récente » par le tri de la sélection.

---

## Étape 6 — Vérification croisée des affirmations

**But :** comparer les sources et repérer ce qui est solide ou fragile.

1. **Statut automatique (code) :** une affirmation appuyée par au moins 2 domaines, sans contradiction signalée, est **corroborée**.
2. **Sélection des affirmations fragiles :** une seule source, une source peu fiable, ou une contradiction détectée. Limite d'environ 8 par question.
3. **Recherche ciblée :** pour chacune, une requête de confirmation et une requête de contradiction (par exemple « X faux », « X contesté »), puis lecture des meilleurs passages.
4. **Verdict du LLM :** `confirmed` / `contested` / `unverifiable`, avec les passages pour et contre.
5. **Comparaison des sources :** on garde, pour chaque affirmation contestée, quelles sources disent quoi. Cela alimente la section « Points de divergence » du rapport.

**✅ Terminé quand :** sur une question à sujet controversé, au moins une affirmation est marquée « contestée » avec les sources des deux côtés.

> **Résultat (18/11/2026) :** 83 tests passent (dont 17 dans `test_verify.py`). Essais sur « Le glyphosate est-il cancérogène pour l'humain ? ».
>
> **Ce qui a été construit (`verify.py`) :**
> 1. *Regroupement* des affirmations équivalentes (embeddings ≥ 0,91, calibré) + **garde-fou** : jamais de regroupement si l'une est nuancée (« probable ») et pas l'autre, si la polarité ou les chiffres diffèrent — les embeddings ne voient pas ces nuances (0,94 entre « probablement cancérogène » et « cancérogène »).
> 2. *Origines indépendantes* : une dépêche reprise par deux journaux ne compte qu'une fois (passages similaires à ≥ 0,95 ; cas réel lapresse.ca / canadafrancais.com).
> 3. ≥ 2 origines fiables → **corroborée**, sans appel au modèle.
> 4. Affirmations fragiles (budget de 8, sources fiables d'abord) : preuves dans le corpus déjà lu, puis sur le web ; verdict du modèle **contrôlé par le code** (« confirmée » sans passage à l'appui → invérifiable ; appuis et contradictions → contestée).
> 5. **Divergences entre sources**, par sous-question : la controverse réelle n'est pas un fait contredit, ce sont des organismes qui concluent différemment. Le modèle compare un échantillon diversifié de 12 affirmations ; le code exige deux côtés existants, venant de domaines différents.
>
> **Ce que les essais ont appris :**
> - 1er essai : une « contestation » erronée (une procédure judiciaire prise pour une contradiction) et 5 vérifications sur 8 gaspillées sur un blog peu fiable → consigne resserrée (critique ≠ contradiction, doute → invérifiable) et priorité aux sources fiables.
> - 2e essai : plus aucune fausse contestation (3 confirmées, 5 invérifiables), mais aucune divergence visible → ajout de l'étape 5.
> - Divergences trouvées : **CIRC « cancérogène probable » vs BfR « peu probable »** ; méta-analyse (+50 % de lymphome non hodgkinien) vs BfR ; lors d'un autre passage, **OMS/CIRC (4 sources) vs EFSA « improbable »**. Le résultat varie d'un passage à l'autre (échantillon et part d'aléatoire du modèle).
>
> **Limites connues :** la durée — la vérification et la comparaison ajoutent ~5 à 8 min à la recherche (davantage quand la machine est chargée) ; beaucoup d'affirmations restent « non vérifiées » faute de budget (8 par question) ; à 20 affirmations, la comparaison dépassait le délai de 5 min (d'où la limite à 12). À régler à l'étape 8.

---

## Étape 7 — Synthèse référencée

**But :** un rapport lisible où chaque phrase importante est sourcée.

1. **Entrée du LLM :** la question, les sous-questions, et uniquement les affirmations corroborées, confirmées ou contestées, avec leurs identifiants de sources.
2. **Consigne :** citer avec `[S3]` après chaque affirmation, ne rien affirmer sans source, signaler les divergences, répondre dans la langue de la question.
3. **Contrôle par le code (`report.py`) :**
   - supprimer les références qui n'existent pas dans le registre ;
   - renuméroter dans l'ordre d'apparition : `[S7]` devient `[1]`, `[S2]` devient `[2]`… ;
   - générer la bibliographie : titre, domaine, URL, date, type.
4. **Structure du rapport :**
   ```
   # <Question>
   ## Réponse courte
   ## Développement (par sous-question)
   ## Points de divergence
   ## Limites (lacunes, couverture incomplète, affirmations invérifiables)
   ## Sources
   ```
5. **Sorties :** `reports/<slug>.md` et `reports/<slug>.trace.json`.

**Tests (`test_report.py`) :** renumérotation correcte, références inventées supprimées, bibliographie complète.

**✅ Terminé quand :** le rapport final ne contient aucune référence invalide, et chaque section « Limites » reflète bien la trace.

> **Résultat (18/11/2026) :** 96 tests passent (dont 10 dans `test_synthesis.py`). Essai de bout en bout sur la caféine : **6 min 20** (2 tours, 11 sources lues, 67 affirmations, 2 divergences, 4 sections rédigées), 10 références, toutes valides.
>
> **Conception (`synthesis.py`) :** le contexte de 8k ne permet pas de tout rédiger d'un coup → **un appel par sous-question** (≤ 15 affirmations, les plus solides d'abord, une par groupe équivalent, sources peu fiables exclues sauf si corroborées), puis une **réponse courte** rédigée à partir des sections. Le modèle cite `[S3]` ; le code ne garde que les sources fournies pour ce texte (accepte aussi `[S3:p2]` et `[C12]`), puis renumérote `[1]`, `[2]`… dans l'ordre du rapport. Les sections *Points de divergence*, *Limites* et *Sources* sont produites **par le code** à partir de la trace. Le rapport brut devient `*.details.md`.
>
> **Corrigé après relecture du premier rapport réel :** identifiants internes (« C6, C21 ») dans l'analyse des divergences → références numérotées ; un site d'entreprise cité parce qu'il appartenait à un groupe corroboré → seules les sources fiables d'un groupe sont citées ; une fausse divergence (limite de sécurité de 400 mg/j contre seuil d'effet de 600 mg/j) → consigne précisée ; noovo.info classé « académique » → liste de médias complétée.
>
> **Incidents rencontrés pendant cette étape (corrigés) :**
> - **Panne des moteurs de recherche** (DuckDuckGo en CAPTCHA, Brave et Google en délai) : SearXNG renvoyait 0 résultat sans erreur, et l'agent l'aurait pris pour un manque d'information. Désormais `search.py` lève une erreur dans ce cas, l'agent s'arrête avec le motif « moteur de recherche indisponible », et la requête en échec n'est pas comptée comme faite. SearXNG : moteurs Bing, Mojeek, Qwant, Startpage ajoutés, délai porté à 6 s.
> - **Machine hôte très ralentie** pendant plusieurs heures (charge ~9, imports Python à 20 s) : la cause est côté Windows/WSL, pas l'agent. L'hypothèse d'un débordement de VRAM (qwen3 + embeddings) a été **mesurée et écartée** : 58 tokens/s avec les deux modèles chargés.
> - Le conteneur SearXNG s'attribue le dossier `searxng/` : la configuration se met à jour par `docker cp` (voir README).

---

## Étape 8 — Évaluation et réglages

**But :** mesurer la qualité et régler les paramètres sur des faits.

1. **Jeu de test** (`eval/questions.yaml`), 10 à 20 questions :
   - faciles (une source officielle suffit) ;
   - multi-aspects (plusieurs sous-questions) ;
   - controversées (sources en désaccord) ;
   - pointues ou récentes (peu de sources) ;
   - en français et en anglais.
2. **Mesures par question :** nombre de tours, sources lues, affirmations, taux de références valides, durée, part d'affirmations corroborées.
3. **Contrôle manuel** d'un échantillon : les citations disent-elles vraiment ce qu'on leur fait dire ?
4. **Réglages :** seuils de suffisance, nombre de passages envoyés, taille du contexte, nombre de tours.
5. **Comparaison de modèles :** `qwen3:8b` contre un autre 8B, et éventuellement `qwen3:14b` (avec débordement en RAM) pour la synthèse seule.

**✅ Terminé quand :** les réglages par défaut donnent des rapports satisfaisants sur la majorité du jeu de test, en un temps acceptable.

> **Résultat (18/11/2026) :** 103 tests passent. Jeu de test : 11 questions dans `eval/questions.yaml` (5 catégories), porté ensuite à **30 (6 par catégorie)** pour que les moyennes par catégorie aient un sens ; `scripts/evaluate.py` produit `eval/runs/<date>-<modèle>/RESULTATS.md`.
>
> **Évaluation « smoke » (une question par catégorie), `qwen3:8b` :**
>
> | Question | Durée | Tours | Suffisant | Sources fiables | Affirm. | Citations vérif. | Divergences | Phrases citées | Réf. inventées |
> |---|---|---|---|---|---|---|---|---|---|
> | cafeine (facile) | 7 min | 2 | oui | 9 | 61 | 80 % | 1 | 79 % | 0 |
> | abeilles (multi) | 6,5 min | 2 | oui | 12 | 65 | 82 % | 0 | 87 % | 0 |
> | glyphosate (controverse) | 8 min | 2 | oui | 13 | 65 | 68 % | 7 → dédoublonnées | 85 % | 0 |
> | semaine-4-jours (pointue) — 2e passage | 5,5 min | 3 | non (honnête) | 6 | 31 | 100 % | 0 | 69 % | 0 |
> | fission-fusion (anglais) — 2e passage | 6,5 min | 2 | oui | 6 | 61 | 89 % | 1 | 69 % | 0 |
>
> Bilan : **~6,5 min par question**, **aucune référence inventée** dans les rapports finaux, 70 à 87 % des phrases de la synthèse sourcées, la controverse détectée sur la question « controverse ».
>
> **Problèmes trouvés par l'évaluation et corrigés :**
> - **Bruit des moteurs de recherche** : des résultats sans rapport (tests de débit internet pour « semaine de quatre jours », Gmail, WhatsApp, dictionnaires) étaient sélectionnés par le modèle avec des justifications absurdes. → **filtre de pertinence** par embeddings (titre + extrait contre la question, seuil 0,55 calibré : sources utiles ≥ 0,57, bruit 0,32–0,53) ; une **sélection vide** est désormais respectée ; consigne de sélection précisée.
> - **Bing via SearXNG** renvoie du bruit pur quand les autres moteurs bloquent, ce qui masquait les pannes → **désactivé**.
> - **Requêtes de repli tronquées** (« Quels sont les résultats des expérimentations de la semaine de ») → réduites aux **mots-clés** (« résultats expérimentations semaine quatre jours »).
> - **Divergences répétées** d'une sous-question à l'autre (CIRC contre EPA ×3) → une même opposition de sources n'est signalée qu'une fois ; références répétées (« [4], [4] ») fusionnées.
> - **Panne totale des moteurs** : arrêt immédiat avec le motif affiché dans le rapport, code de sortie 2.
>
> **Limite principale restante : les moteurs de recherche gratuits.** Après quelques heures d'usage intensif (~20 exécutions, plusieurs centaines de requêtes), Brave, Google, DuckDuckGo et Qwant suspendent SearXNG (trop de requêtes, CAPTCHA ; suspension d'une heure à un jour). L'agent le détecte et le signale, et espace désormais ses requêtes d'1 s, mais l'évaluation complète (30 questions) n'a pas pu être lancée dans la foulée. Pour un usage soutenu, il faudrait un moteur avec clé d'API (par exemple Brave Search API) derrière `SearchProvider`.
>
> **Non fait :** la comparaison avec un autre modèle (`--model llama3.1:8b`) et l'évaluation complète, faute de moteurs disponibles ; à relancer avec `python scripts/evaluate.py` quand les moteurs sont débloqués.

---

## Questions ouvertes

- [ ] **Recherche web :** SearXNG auto-hébergé, ou une API externe (Brave, Tavily…) ?
- [ ] **100 % local :** l'exigence porte-t-elle seulement sur le modèle, ou aussi sur tous les services ?
- [ ] **Langue du rapport :** celle de la question, ou toujours en français ?
- [ ] **Budget par question :** nombre de tours maximum (proposé : 3) et sources lues par tour (proposé : 5 à 8).
- [ ] **Restrictions de domaines :** privilégier ou exclure certains types de sources ?
- [ ] **Interface :** ligne de commande seulement, ou interface web plus tard ?

---

## Étape 9 — Mise en production : interface web (FastAPI)

**Objectif :** utiliser l'agent depuis un navigateur, suivre son raisonnement en direct et conserver l'historique.

- **Point d'accroche dans l'agent :** `ResearchState.log()` appelle un observateur facultatif (`agent.run(question, listener=...)`). L'agent n'a pas d'autre dépendance au web. L'annulation passe par une exception `Cancelled`, qui hérite de `BaseException` pour ne pas être interceptée par les `except Exception` de l'agent.
- **File d'attente** (`web/jobs.py`) : SQLite et un unique fil d'exécution, car il n'y a qu'un GPU. Une recherche interrompue par un redémarrage est marquée en échec ; celles en attente sont reprises.
- **API** (`web/app.py`) : création, liste, détail, annulation et suppression des recherches ; progression en SSE ; rapport en HTML ; téléchargements ; état d'Ollama et de SearXNG.
- **Interface** (`web/static/`) : historique, formulaire, déroulement en direct (étapes, sources lues, décisions de l'agent), rapport avec références cliquables, thème sombre, version mobile.
- **Déploiement** : `Dockerfile` et service `web` dans `docker-compose.yml`, en réseau hôte pour joindre Ollama et SearXNG.

> **Résultat (18/11/2026) :** 116 tests passent (13 pour le service web, avec un exécutant factice). Essais réels via l'API :
> - « marées » : arrêt honnête après 0,9 min, les moteurs étant bloqués ;
> - « caféine » : 5,6 min, 4 sources lues, 29 affirmations, rapport affiché avec ses références cliquables.
>
> Image Docker construite ; conteneur `healthy`, avec 401 sans mot de passe et 200 avec. Le service refuse de démarrer sur `0.0.0.0` sans mot de passe.
