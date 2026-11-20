"""Client du modèle local (Ollama `/api/chat`).

- `chat()`        : texte libre (utilisé pour la synthèse finale) ;
- `structured()`  : JSON contraint par un schéma Pydantic, validé, avec un nouvel essai ;
- contrôle de la taille du contexte : Ollama tronque silencieusement un message trop long,
  on préfère donc refuser l'appel avant (ContextOverflowError) ;
- statistiques cumulées (appels, tokens, durée) pour l'évaluation.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)

_THINK_TAGS = re.compile(r"<think>.*?</think>", re.DOTALL)


class LLMError(RuntimeError):
    pass


class ContextOverflowError(LLMError):
    pass


@dataclass
class LLMStats:
    calls: int = 0
    retries: int = 0
    failures: int = 0
    prompt_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def schema_for_prompt(schema: type[BaseModel]) -> str:
    """Schéma JSON compact à inclure dans la consigne (le modèle ne voit pas le paramètre `format`)."""
    return json.dumps(schema.model_json_schema(), ensure_ascii=False, separators=(",", ":"))


class OllamaLLM:
    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        num_ctx: int = 8192,
        temperature: float = 0.2,
        timeout_s: float = 300.0,
        max_output_tokens: int = 2048,
        max_output_tokens_thinking: int = 4096,
        chars_per_token: float = 3.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.num_ctx = num_ctx
        self.temperature = temperature
        self.timeout_s = timeout_s
        self.max_output_tokens = max_output_tokens
        self.max_output_tokens_thinking = max_output_tokens_thinking
        self.chars_per_token = chars_per_token  # estimation prudente (surestime le nombre de tokens)
        self.client = client or httpx.Client()
        self.stats = LLMStats()

    # --- Bas niveau ------------------------------------------------------------

    def estimate_tokens(self, messages: list[dict]) -> int:
        chars = sum(len(m.get("content", "")) for m in messages)
        return int(chars / self.chars_per_token) + 10 * len(messages)

    def prompt_budget(self, think: bool) -> int:
        """Nombre de tokens disponibles pour le message, une fois la sortie réservée."""
        reserve = self.max_output_tokens_thinking if think else self.max_output_tokens
        return self.num_ctx - reserve

    def _call(self, messages: list[dict], *, think: bool, fmt: dict | None = None) -> tuple[str, str]:
        """Renvoie (contenu, done_reason)."""
        budget = self.prompt_budget(think)
        estimate = self.estimate_tokens(messages)
        if estimate > budget:
            raise ContextOverflowError(
                f"message estimé à {estimate} tokens pour un budget de {budget} "
                f"(num_ctx={self.num_ctx}, think={think}) : réduire les passages envoyés"
            )
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "think": think,
            "options": {
                "num_ctx": self.num_ctx,
                "temperature": self.temperature,
                "num_predict": self.max_output_tokens_thinking if think else self.max_output_tokens,
            },
        }
        if fmt is not None:
            payload["format"] = fmt

        t0 = time.time()
        try:
            r = self.client.post(f"{self.base_url}/api/chat", json=payload, timeout=self.timeout_s)
            r.raise_for_status()
            data = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            self.stats.failures += 1
            raise LLMError(f"appel au modèle en échec : {exc}") from exc
        finally:
            self.stats.seconds += time.time() - t0
            self.stats.calls += 1

        self.stats.prompt_tokens += data.get("prompt_eval_count", 0) or 0
        self.stats.output_tokens += data.get("eval_count", 0) or 0
        content = _THINK_TAGS.sub("", data.get("message", {}).get("content", "")).strip()
        return content, data.get("done_reason", "")

    # --- Haut niveau -----------------------------------------------------------

    def chat(self, messages: list[dict], *, think: bool = False) -> str:
        content, done_reason = self._call(messages, think=think)
        if done_reason == "length":
            raise LLMError("réponse tronquée (limite de tokens de sortie atteinte)")
        return content

    def structured(
        self, messages: list[dict], schema: type[T], *, think: bool = False, retries: int = 1
    ) -> T:
        """Appel avec sortie JSON contrainte par `schema`, validée par Pydantic.

        En cas de JSON invalide : nouvel essai en montrant l'erreur au modèle.
        Si la sortie a été tronquée pendant la réflexion, le nouvel essai se fait sans réflexion.
        """
        instruction = (
            "\n\nRéponds uniquement avec un objet JSON conforme à ce schéma JSON :\n"
            + schema_for_prompt(schema)
        )
        msgs = [*messages[:-1], {**messages[-1], "content": messages[-1]["content"] + instruction}]
        fmt = schema.model_json_schema()

        last_error = ""
        for attempt in range(retries + 1):
            content, done_reason = self._call(msgs, think=think, fmt=fmt)
            try:
                return schema.model_validate_json(content)
            except ValidationError as exc:
                last_error = f"{done_reason or 'stop'} : {exc.errors()[:3]}"
                if attempt == retries:
                    break
                self.stats.retries += 1
                if done_reason == "length":
                    think = False  # la réflexion a consommé toute la sortie
                    continue
                msgs = [
                    *msgs,
                    {"role": "assistant", "content": content[:2000]},
                    {
                        "role": "user",
                        "content": f"Ce JSON est invalide ({exc.errors()[:3]}). "
                        "Renvoie uniquement le JSON corrigé, conforme au schéma.",
                    },
                ]
        self.stats.failures += 1
        raise LLMError(
            f"sortie JSON invalide pour {schema.__name__} après {retries + 1} essai(s) : {last_error}"
        )
