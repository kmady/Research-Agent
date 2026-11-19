"""Schémas Pydantic des sorties JSON demandées au modèle.

Règles de conception (modèle 8B) :
- structures plates, au plus un niveau de liste d'objets ;
- valeurs fermées en Literal (le décodage contraint garantit qu'elles sont respectées) ;
- descriptions courtes : le schéma est aussi montré au modèle dans la consigne ;
- le modèle ne cite que des identifiants (S3, S3:p2, Q1) que le code vérifie ensuite.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Level = Literal["high", "medium", "low"]


# --- Étape 1 : plan ----------------------------------------------------------

class SubQuestion(BaseModel):
    id: str = Field(description="Identifiant : Q1, Q2, ...")
    question: str = Field(description="Sous-question précise et vérifiable")


class ResearchPlan(BaseModel):
    sub_questions: list[SubQuestion] = Field(description="2 à 4 sous-questions qui couvrent la question")
    initial_queries: list[str] = Field(description="3 à 5 requêtes de moteur de recherche, courtes et variées")


# --- Étape 2b : sélection des URLs à lire -----------------------------------

class SelectedSource(BaseModel):
    source_id: str = Field(description="Identifiant de la source : S1, S2, ...")
    reason: str = Field(description="Pourquoi la lire, en une phrase")


class UrlSelection(BaseModel):
    selected: list[SelectedSource]


# --- Étape 3 : extraction (une source à la fois) -----------------------------

class ExtractedClaim(BaseModel):
    statement: str = Field(description="Affirmation factuelle, autonome, en une phrase")
    sub_question_id: str = Field(description="Sous-question concernée : Q1, Q2, ...")
    passage_ids: list[str] = Field(description="Passages qui l'appuient : S3:p1, S3:p2, ...")
    quote: str = Field(description="Courte citation exacte du passage, ou chaîne vide")


SourceKind = Literal["official", "academic", "press", "encyclopedia", "company", "blog", "other"]


class Extraction(BaseModel):
    claims: list[ExtractedClaim] = Field(description="Affirmations utiles à la question ; liste vide si aucune")
    source_kind: SourceKind
    reliability: Level = Field(description="Fiabilité estimée de la source")
    publication_date: str = Field(description="Date de publication AAAA-MM-JJ si visible, sinon chaîne vide")


# --- Étape 4 : suffisance (une sous-question à la fois) ----------------------

class SubQuestionAssessment(BaseModel):
    status: Literal["covered", "partial", "missing"]
    rationale: str = Field(description="Justification courte du statut")
    missing_aspects: list[str] = Field(description="Ce qui manque encore ; vide si couvert")
    follow_up_queries: list[str] = Field(description="0 à 3 requêtes pour combler les manques")


# --- Étape 5 : vérification d'une affirmation --------------------------------

class ClaimVerdict(BaseModel):
    verdict: Literal["confirmed", "contested", "unverifiable"]
    supporting_passage_ids: list[str]
    contradicting_passage_ids: list[str]
    note: str = Field(description="Explication courte du verdict")


# --- Étape 6 (suite) : divergences entre sources, par sous-question ------------

class Divergence(BaseModel):
    point: str = Field(description="Le point précis sur lequel les sources divergent")
    side_a_claim_ids: list[str] = Field(description="Affirmations d'un côté : C1, C2, ...")
    side_b_claim_ids: list[str] = Field(description="Affirmations de l'autre côté")
    explanation: str = Field(description="En quoi les deux côtés s'opposent, en une ou deux phrases")


class DivergenceReport(BaseModel):
    divergences: list[Divergence] = Field(description="0 à 3 divergences ; liste vide s'il n'y en a pas")
