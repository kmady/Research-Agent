# Research-Agent : retour d'expérience sur la création d'un agent autonome

## 1. Le projet en bref

**Objectif** : un agent qui reçoit une question, cherche des sources sur le web, les compare, vérifie les affirmations et rédige une synthèse où chaque phrase renvoie à une source réellement lue.

**Ce qui en fait un agent** : après chaque tour de recherche, il évalue lui-même si l'information suffit. Si elle ne suffit pas, il identifie ce qui manque et lance une recherche complémentaire ciblée. Chaque décision est motivée et tracée.

**Contraintes** :
- un modèle **open source** exécuté **en local** ;
- un seul GPU grand public, une RTX 2070 Super avec **8 Go de VRAM**.

**En chiffres** :
- environ 2 800 lignes de Python ;
- 103 tests unitaires, exécutés en 0,5 s, sans réseau ni modèle ;
- 6 à 8 minutes par question.

## 2. Architecture

```
Question
  │
  ▼
Plan ──────────── 2 à 4 sous-questions + requêtes
  │
  ▼
┌─ Tour de recherche (3 au maximum) ─────────────────────────────┐
│  Recherche → filtre de pertinence → sélection → lecture        │
│  → extraction des affirmations (citation exacte exigée)        │
│  → évaluation de chaque sous-question                          │
│      avis du modèle + règles du code                           │
│  → DÉCISION : suffisant ? sinon nouvelles requêtes ciblées     │
└────────────────────────────────────────────────────────────────┘
  │
  ▼
Vérification ──── corroboration, contrôle des affirmations fragiles,
  │               divergences entre sources
  ▼
Synthèse ──────── réponse courte, développement, divergences,
                  limites, sources numérotées
```

### Principe directeur : le code pilote, le modèle exécute

Un modèle de 8 milliards de paramètres ne sait pas mener seul une longue boucle d'appels d'outils : il oublie l'objectif, invente des sources et s'arrête trop tôt. En revanche, il réussit bien les **tâches courtes et cadrées**.

Le travail est donc réparti ainsi :
- **le modèle** traite environ dix tâches précises : planifier, sélectionner, extraire, évaluer, vérifier, rédiger. Chaque réponse est en JSON, validée par un schéma ;
- **le code** décide de ce qui engage la fiabilité du résultat, selon les règles du tableau ci-dessous.

| Règle appliquée par le code | Ce qu'elle empêche |
|---|---|
| Une sous-question n'est « couverte » que si **2 domaines fiables distincts** l'appuient | que l'agent se contente d'une seule source |
| Les sites marchands et les blogs sont classés en fiabilité faible | qu'un site de vente serve de preuve |
| Une affirmation n'est **corroborée** que par 2 origines indépendantes : une dépêche reprise par deux journaux ne compte qu'une fois | une fausse corroboration |
| Chaque citation est comparée au texte de la page | les citations inventées |
| Les références sont renumérotées par le code ; tout identifiant inconnu est supprimé | les références inventées |
| Arrêt après 3 tours, en cas de stagnation, ou si le moteur de recherche est en panne | les boucles sans fin et les arrêts silencieux |

Les fonctions de décision sont **pures**, sans appel au modèle ni au réseau. On peut donc les tester exhaustivement.

## 3. Choix technologiques

| Choix | Pourquoi | Alternatives écartées |
|---|---|---|
| **Qwen3 8B**, quantifié en 4 bits (Q4_K_M) | Open source et bon en français. Il suit bien un schéma JSON. Son mode « réflexion » s'active **appel par appel** : on l'utilise pour évaluer et vérifier, et on le coupe pour extraire, ce qui va plus vite. Ses ~5 Go laissent de la place au contexte dans 8 Go. | Modèles de 14B ou plus : ne tiennent pas en VRAM. Modèles de 3–4B : trop faibles pour juger de la suffisance. |
| **Ollama** | Installation simple et API HTTP locale. Son paramètre `format` **contraint la sortie** au schéma JSON. Il charge le modèle de génération et le modèle d'embeddings côte à côte. | vLLM : pensé pour des serveurs, gourmand en VRAM. llama.cpp en direct : plus de code d'intégration. |
| **Contexte de 8192 tokens** | Mesuré : à 8192 tokens, tout tient en VRAM (46 à 58 tokens/s). À 12 288, une partie passe sur le processeur et la génération devient beaucoup plus lente. Le code estime la taille de chaque requête et la réduit **avant** l'envoi. | Un contexte plus grand imposerait un modèle plus petit. |
| **nomic-embed-text** (embeddings) | Open source, léger, servi par le même Ollama. Il est utilisé pour quatre tâches : classer les passages d'une page, écarter les résultats hors sujet, regrouper les affirmations équivalentes et repérer les articles syndiqués. | Classement par le LLM : trop lent. Recherche par mots-clés : ne gère pas les paraphrases. |
| **SearXNG** auto-hébergé (Docker) | Gratuit, sans clé d'API, interroge plusieurs moteurs à la fois et renvoie du JSON. Il garde la chaîne 100 % locale et open source. Il est placé derrière une interface `SearchProvider`, ce qui permet de le remplacer. | API payantes (Brave, Tavily, SerpAPI) : plus fiables, mais avec un coût et une dépendance externe. |
| **trafilatura** / **pypdf** | trafilatura extrait le texte principal d'une page web (sans menus ni publicités), pypdf le texte d'un PDF. | BeautifulSoup : il faudrait écrire l'extraction à la main. |
| **Pydantic** | Un même schéma sert trois usages : il fournit le JSON Schema envoyé à Ollama, valide la réponse et décrit l'erreur pour un nouvel essai. | Analyse manuelle du JSON : fragile. |
| **Boucle écrite à la main**, sans framework | Elle donne le contrôle total des décisions et rend chaque étape traçable et testable. Avec un petit modèle, il faut un contrôle fin de chaque appel, que les frameworks masquent. | LangChain, LlamaIndex : couches d'abstraction et appels d'outils libres, mal adaptés à un modèle de 8B. |
| **FastAPI** + Server-Sent Events, SQLite, HTML/JS sans framework | FastAPI : API typée avec Pydantic, déjà utilisé pour les schémas, et documentation automatique. SSE : le suivi en direct d'une recherche de 6 minutes est un flux à sens unique, inutile de recourir aux WebSockets. SQLite : historique persistant sans serveur de base de données. Une file d'attente à un seul fil, car un seul GPU. | Streamlit/Gradio : rapides à prototyper, mais pas d'API réutilisable. Celery + Redis : surdimensionnés pour un seul GPU. |
| **httpx**, **uv**, **pytest**, cache disque | Outils légers et standards. Le cache rend les tests reproductibles et ménage les moteurs de recherche. | — |

## 4. Démarche en 9 étapes

1. **Infrastructure** : Ollama, SearXNG, mesure de la VRAM et du contexte.
2. **Recherche et lecture** : extraction du texte, découpage en passages, classement par embeddings.
3. **Client LLM fiable** : JSON contraint, nouvel essai guidé par l'erreur, garde de contexte.
4. **Boucle de recherche** : plan, sélection, extraction avec citations vérifiées.
5. **Décision de suffisance** : le cœur agentique, qui combine l'avis du modèle et des règles dures.
6. **Vérification** : corroboration, verdicts sur les affirmations fragiles, divergences.
7. **Synthèse référencée** : une rédaction par sous-question, puis contrôle des références.
8. **Évaluation** : jeu de 30 questions (6 par catégorie, 5 catégories), mesures automatiques par question et par catégorie.

9. **Mise en production** : interface web FastAPI (file d'attente pour un seul GPU, progression en direct, historique), déploiement Docker.

Chaque étape a été validée sur de vraies questions avant de passer à la suivante. Le détail figure dans [ETAPES_PROJET.md](ETAPES_PROJET.md).

## 5. Difficultés rencontrées et leçons

| Problème observé | Correction | Leçon |
|---|---|---|
| Le modèle déclarait « suffisant » dès le premier tour | « Suffisant » exige 2 domaines fiables. Une sous-question jugée « partielle » déclenche toujours une recherche ciblée au tour 1. | **Ne pas laisser le modèle juger seul de son propre travail.** |
| Les moteurs renvoyaient des pages sans rapport (tests de débit, WhatsApp), et le modèle les sélectionnait en inventant des justifications | Filtre de pertinence par embeddings, avec un seuil calibré sur les données réelles (sources utiles ≥ 0,57, bruit ≤ 0,53). Le modèle a aussi le droit de ne rien sélectionner. | **Un petit modèle rationalise au lieu de refuser** : il faut filtrer en amont. |
| Une panne des moteurs (CAPTCHA) passait pour « aucune information trouvée » | La panne est détectée et signalée. L'agent s'arrête en l'indiquant dans le rapport, et la commande renvoie le code 2. | **Distinguer « rien trouvé » de « rien pu chercher ».** |
| Une critique était prise pour une contradiction, ce qui produisait de faux « contesté » | Consigne plus stricte, et en cas de doute le verdict est « invérifiable ». | Préférer l'incertitude honnête au faux positif. |
| « Probablement cancérogène » et « cancérogène » étaient fusionnés | Deux affirmations ne sont jamais regroupées si leur degré de certitude, une négation ou un chiffre diffèrent. | La similarité sémantique ignore les nuances qui comptent. |
| Une dépêche reprise par deux sites comptait double | Les passages quasi identiques (similarité ≥ 0,95) sont fusionnés et comptent pour une seule origine. | Indépendance des sources ≠ nombre de sources. |
| Références inventées, ou identifiants internes (« C6 ») visibles dans le rapport | Le code ne garde que les références connues et les renumérote. | **Le modèle rédige, le code cite.** |

## 6. Résultats

Évaluation sur une question par catégorie :

| Question | Durée | Tours | Verdict de l'agent |
|---|---|---|---|
| Effets de la caféine | 7 min | 2 | suffisant, 79 % des phrases sourcées |
| Déclin des abeilles | 6,5 min | 2 | suffisant, 87 % des phrases sourcées |
| Glyphosate cancérogène ? | 8 min | 2 | suffisant, controverse CIRC / agences réglementaires détectée |
| Semaine de 4 jours | 5,5 min | 3 | **incomplet, signalé comme tel** |
| Fission / fusion (en anglais) | 6,5 min | 2 | suffisant |

**Aucune référence inventée** n'a subsisté dans les rapports finaux. Quand l'information manque, l'agent le dit au lieu de combler le vide.

## 7. Limites et pistes

- **Moteurs de recherche gratuits** : ils suspendent l'accès après un usage intensif. Un moteur avec clé d'API se brancherait via `SearchProvider`.
- **Évaluation partielle** : la série complète de 30 questions et la comparaison avec un autre modèle (`--model llama3.1:8b`) restent à faire.
- **Wikipédia** : sa politique d'accès demande un user-agent avec contact, réglable via `RA_USER_AGENT`.
- **Contexte limité à 8 000 tokens** : on peut l'agrandir en activant la *flash attention* et en quantifiant le cache KV dans Ollama.

## 8. Ce que j'en retiens

1. **Un agent fiable avec un petit modèle open source est possible**, à condition de réduire le rôle du modèle à des tâches courtes et vérifiables.
2. **Le comportement agentique (« ai-je assez d'information ? ») doit être encadré par des règles explicites.** Le jugement du modèle seul est trop optimiste.
3. **L'évaluation sur de vraies questions a révélé la plupart des défauts.** Les tests unitaires garantissent que les corrections tiennent.
4. **L'honnêteté du résultat** (signaler une panne, une lacune ou une controverse) compte autant que sa qualité.
