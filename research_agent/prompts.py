"""Consignes de chaque étape.

Elles corrigent les défauts observés à l'étape 3 : requêtes formulées comme des
questions, sites marchands sélectionnés en premier, citations approximatives.
Le schéma JSON attendu est ajouté automatiquement par `OllamaLLM.structured()`.
"""

from __future__ import annotations

SYSTEM = (
    "Tu es un assistant de recherche documentaire rigoureux. "
    "Tu t'appuies uniquement sur les informations fournies dans le message, jamais sur tes connaissances. "
    "Tu cites uniquement les identifiants fournis (Q1, S3, S3:p2...). "
    "Tu rédiges dans la langue de la question."
)


def plan_prompt(question: str) -> str:
    return f"""Question de recherche : {question}

Tâche : prépare la recherche documentaire.

1. Décompose la question en 2 à 4 sous-questions (identifiants Q1, Q2...).
   - Chaque sous-question porte sur un aspect distinct et peut être vérifiée dans des sources.
   - Ensemble, elles couvrent toute la question, sans chevauchement.

2. Propose 3 à 5 requêtes pour un moteur de recherche web.
   - Des mots-clés, pas des questions : 3 à 8 mots, sans point d'interrogation.
     Exemple : « caféine sommeil profond étude » plutôt que « Quel est l'effet de la caféine ? ».
   - Variées : au moins une requête par sous-question.
   - Dans la langue de la question. Si le sujet est scientifique ou international, ajoute une requête
     traduite en anglais (n'écris pas le mot « anglais » ou « english » dans la requête)."""


def selection_prompt(question: str, sub_questions: str, candidates: str, n: int, focus: str = "") -> str:
    focus_block = (
        f"\nRecherche complémentaire : la priorité va aux sources utiles pour ces sous-questions, "
        f"encore insuffisamment couvertes :\n{focus}\n"
        if focus
        else ""
    )
    return f"""Question de recherche : {question}
Sous-questions :
{sub_questions}
{focus_block}
Résultats de recherche (identifiant, titre, domaine, extrait) :
{candidates}

Tâche : choisis au plus {n} sources à lire, par ordre de priorité.
Ne retiens QUE des sources qui traitent réellement du sujet : s'il y en a peu, choisis-en peu ;
si aucune n'est pertinente, renvoie une liste vide. Ne justifie jamais une source hors sujet.

Critères, par ordre d'importance :
1. Fiabilité : privilégie les sources officielles (institutions, agences de santé, administrations),
   académiques (universités, revues, articles scientifiques), encyclopédiques et la presse reconnue.
2. Évite : les sites qui vendent un produit lié au sujet, les forums, les pages publicitaires
   ou de contenu générique sans auteur ni source.
3. Pertinence : l'extrait doit laisser penser que la page répond à au moins une sous-question.
4. Diversité : choisis des domaines différents entre eux, et de préférence non marqués « domaine déjà lu » :
   une confirmation par un site indépendant vaut plus qu'une page de plus du même site.

Donne pour chaque source choisie une raison en une phrase."""


def extraction_prompt(question: str, sub_questions: str, source_header: str, passages: str) -> str:
    return f"""Question de recherche : {question}
Sous-questions :
{sub_questions}

Source : {source_header}

Passages de cette source :
{passages}

Tâche 1 — Extrais les affirmations factuelles utiles pour répondre aux sous-questions.
- Uniquement ce que disent explicitement les passages : aucune connaissance extérieure, aucune déduction.
- Chaque affirmation est autonome et compréhensible seule : remplace « il », « cette étude », « ce produit »
  par ce qu'ils désignent. Garde les chiffres, dates, populations étudiées et auteurs mentionnés.
- Rattache chaque affirmation à UNE sous-question (Q1, Q2...) et aux passages qui l'appuient,
  en n'utilisant que les identifiants de passages affichés ci-dessus.
- `quote` : copie EXACTE d'un extrait du passage (moins de 30 mots, sans rien modifier) ; chaîne vide sinon.
- 0 à 8 affirmations. Si les passages ne parlent pas du sujet, renvoie une liste vide.

Tâche 2 — Évalue la source.
- source_kind : official (institution, administration), academic (université, revue, étude), press (média,
  y compris un article de presse qui rapporte une étude),
  encyclopedia, company (site d'entreprise ou marchand), blog (blog, site personnel, contenu générique), other.
- reliability : high (institution ou publication scientifique), medium (presse reconnue, encyclopédie),
  low (site marchand, blog, contenu non sourcé).
- publication_date : AAAA-MM-JJ si une date est visible dans les passages, sinon chaîne vide."""


def assessment_prompt(question: str, sq_id: str, sq_text: str, claims: str, metrics: str) -> str:
    return f"""Question de recherche : {question}
Sous-question à évaluer — {sq_id} : {sq_text}

Affirmations recueillies pour cette sous-question (avec leur source) :
{claims}

Faits mesurés par le programme :
{metrics}

Tâche : juge si ces affirmations suffisent pour répondre à la sous-question {sq_id}.

- covered : les affirmations répondent à l'essentiel de la sous-question, de façon précise.
  Une réponse peut être « covered » sans être exhaustive : n'exige pas des détails que la sous-question ne demande pas.
- partial : un aspect central de la sous-question elle-même reste sans réponse, ou les affirmations sont vagues.
- missing : les affirmations ne répondent pas à la sous-question.

Ne juge que le contenu des affirmations ; la corroboration entre sources est vérifiée par le programme.

Si le statut n'est pas « covered » :
- missing_aspects : les aspects centraux qui manquent (1 à 3, précis) ;
- follow_up_queries : 1 à 3 requêtes de moteur de recherche pour les trouver.
  Des mots-clés, pas des questions : 3 à 8 mots, sans point d'interrogation,
  différentes des requêtes évidentes déjà faites sur le sujet général.
Si le statut est « covered », renvoie des listes vides."""


def verification_prompt(claim: str, claim_domain: str, evidence: str) -> str:
    return f"""Affirmation à vérifier (tirée de {claim_domain}) :
« {claim} »

Passages d'autres sources (identifiant, domaine, fiabilité) :
{evidence}

Tâche : ces passages confirment-ils ou contredisent-ils l'affirmation ?

- confirmed : au moins un passage énonce explicitement le même fait (mêmes chiffres, ou chiffres compatibles,
  même degré de certitude : « probable » ne confirme pas « certain »).
- contested : au moins un passage énonce explicitement le CONTRAIRE du même fait, ou des chiffres nettement
  différents pour la même mesure. Si certains passages confirment et d'autres contredisent, réponds
  « contested » et cite les deux.
  Ne sont PAS des contradictions : une critique de la méthode, une opinion, une procédure en justice,
  une décision ultérieure, un fait voisin ou un autre sujet.
- unverifiable : les passages parlent du sujet sans énoncer ce fait précis ni son contraire.
  En cas de doute, réponds « unverifiable ».

Règles :
- Appuie-toi uniquement sur les passages ci-dessus, jamais sur tes connaissances.
- supporting_passage_ids / contradicting_passage_ids : uniquement des identifiants affichés ci-dessus.
- note : une ou deux phrases qui expliquent le verdict, en citant les chiffres ou formulations décisifs."""


def divergence_prompt(question: str, sq_id: str, sq_text: str, claims: str) -> str:
    return f"""Question de recherche : {question}
Sous-question {sq_id} : {sq_text}

Affirmations recueillies (identifiant, texte, source) :
{claims}

Tâche : compare les sources. Repère les points sur lesquels des sources DIFFÉRENTES donnent des réponses
incompatibles à cette sous-question.

Exemples de divergences :
- deux organismes ou études aboutissent à des conclusions opposées (« probable » contre « peu probable ») ;
- des chiffres nettement différents pour la même mesure ;
- une source affirme un effet, une autre affirme son absence.

Ne sont PAS des divergences : des affirmations complémentaires sur des aspects différents, des nuances
de formulation, deux affirmations venant de la même source, ou des chiffres qui portent sur des mesures
différentes (par exemple une limite de sécurité recommandée et un seuil à partir duquel un effet apparaît).

Pour chaque divergence (0 à 3), indique les affirmations de chaque côté (uniquement des identifiants
affichés ci-dessus) et explique l'opposition. S'il n'y a pas de divergence, renvoie une liste vide."""


def section_prompt(question: str, sq_id: str, sq_text: str, claims: str, divergences: str) -> str:
    div_block = (
        f"\nDivergences repérées entre sources sur cette sous-question :\n{divergences}\n"
        if divergences
        else ""
    )
    return f"""Question de recherche : {question}
Sous-question {sq_id} : {sq_text}

Affirmations vérifiées, de la plus solide à la moins solide (avec leurs sources et leur statut) :
{claims}
{div_block}
Tâche : rédige la partie du rapport qui répond à cette sous-question.

- 1 à 3 paragraphes de prose, sans titre, sans liste à puces.
- Uniquement les informations ci-dessus : n'ajoute aucune connaissance extérieure.
- Après chaque phrase qui contient une information, mets entre crochets l'identifiant de la ou des sources
  fournies, par exemple [S3] ou [S3][S7]. N'invente aucun identifiant.
- Nuance selon le statut : présente comme établi ce qui est « corroborée » ou « confirmée » ;
  attribue à sa source ce qui vient d'une « source unique » (« selon ... ») ;
  s'il y a une divergence, présente les deux positions et qui les soutient, sans trancher.
- Conserve les chiffres, dates et noms d'organismes. Style clair et neutre."""


def short_answer_prompt(question: str, sections: str) -> str:
    return f"""Question de recherche : {question}

Parties du rapport déjà rédigées, avec leurs références entre crochets :
{sections}

Tâche : rédige la « réponse courte » placée en tête du rapport.

- 3 à 5 phrases qui répondent directement à la question, sans titre ni liste.
- Uniquement ce que disent les parties ci-dessus ; reprends leurs références entre crochets ([S3]...).
- S'il existe un désaccord entre sources, dis-le explicitement."""
