"""Chat and embedding calls for the LLM gateway's data plane (`/v1/chat/completions`, `/v1/embeddings`).

Rotating synthetic prompts (generic text about fictional topics: no personal data), a list of model aliases to rotate through, a
mix of chat and embedding calls and a range of token sizes. Auth is a client key (`lg_...`, from the environment) or, when none is
set (`LLMGW_CLIENT_KEY`, else `LLM_API_KEY`, the variable `ecoctl` injects into an app with `llm: true`, whose gateway client is
named like the app and only allows the aliases `LLM_CHAT_MODEL` and `EMBEDDING_MODEL`, which are therefore the default aliases here),
a service token from the identity provider with scope `llmgw.use`. The gateway matches that token's client id (`cpsim-service`) against
the NAME of a registered client, so for this route a client of exactly that name must exist at the gateway. 429 `budget_exceeded` and `rate_limit_exceeded` are EXPECTED outcome classes: they are what a gateway with
limits is for, and the loop backs off when it sees them.
"""
import os
import re
from dataclasses import dataclass

from ..model import SpecError
from .base import (
    BaseSender,
    Result,
    classify_status,
    json_error_code,
    network_result,
    p_int,
    p_list,
    p_num,
    parse_retry_after,
    unknown_params,
)

ALIAS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,79}$")
PARAMS = {"chat_models", "embedding_models", "chat_pct", "max_tokens_min", "max_tokens_max", "embed_inputs", "temperature", "seed"}
TOPICS = ["a lighthouse keeper", "tidal energy", "a village bakery", "orbital debris", "mountain railways", "a community garden",
          "bird migration", "paper recycling", "a night market", "wind turbines", "a public library", "river ferries",
          "solar panels on a school", "a bicycle workshop", "a weather station", "glassblowing", "a ship canal", "beekeeping"]
TASKS = ["Summarise in two sentences", "Write a short friendly note about", "List three facts about", "Explain simply how it works for",
         "Suggest a title for an article on", "Give one risk and one benefit of", "Draft a tweet-length announcement about"]
SYSTEMS = ["You are a concise assistant.", "You answer in plain language.", "You are a careful technical writer."]


@dataclass
class ChatOp:
    model: str
    body: dict


@dataclass
class EmbedOp:
    model: str
    body: dict


def default_alias(env: str, fallback: str) -> list[str]:
    """The alias an `llm: true` app is allowed (set by ecoctl in `env`), else the one a fresh gateway has."""
    value = os.environ.get(env, "").strip()
    return [value if ALIAS.match(value) else fallback]


def validate(params: dict) -> dict:
    unknown_params(params, PARAMS)
    out = {"chat_models": p_list(params, "chat_models", default_alias("LLM_CHAT_MODEL", "echo-chat"), ALIAS),
           "embedding_models": p_list(params, "embedding_models", default_alias("EMBEDDING_MODEL", "echo-embed"), ALIAS),
           "chat_pct": p_num(params, "chat_pct", 80, 0, 100),
           "max_tokens_min": p_int(params, "max_tokens_min", 16, 1, 4096),
           "max_tokens_max": p_int(params, "max_tokens_max", 128, 1, 4096),
           "embed_inputs": p_int(params, "embed_inputs", 4, 1, 32),
           "temperature": p_num(params, "temperature", 0.7, 0, 2),
           "seed": p_int(params, "seed", None, 0, 2**32 - 1)}
    if out["max_tokens_min"] > out["max_tokens_max"]:
        raise SpecError("params.max_tokens_min must not exceed max_tokens_max")
    return out


def prompt(rng) -> str:
    return f"{rng.choice(TASKS)} {rng.choice(TOPICS)} (note {rng.randrange(10_000):04d})."


def plan_calls(rng, n: int, params: dict, seq: int = 0) -> list:
    """The next `n` calls: chat or embedding by `chat_pct`, models rotated, token sizes and prompts varied. Pure."""
    ops: list = []
    for i in range(n):
        if rng.random() * 100 < params["chat_pct"] or not params["embedding_models"]:
            model = params["chat_models"][(seq + i) % len(params["chat_models"])]
            ops.append(ChatOp(model, {"model": model, "temperature": round(params["temperature"], 2),
                                      "max_tokens": rng.randint(params["max_tokens_min"], params["max_tokens_max"]),
                                      "messages": [{"role": "system", "content": rng.choice(SYSTEMS)},
                                                   {"role": "user", "content": prompt(rng)}]}))
        else:
            model = params["embedding_models"][(seq + i) % len(params["embedding_models"])]
            ops.append(EmbedOp(model, {"model": model, "input": [prompt(rng) for _ in range(rng.randint(1, params["embed_inputs"]))]}))
    return ops


def classify_llm(status: int, code: str, headers=None) -> Result:
    """The gateway's error table (API.md): budget and rate limits are expected; bad credentials are fatal."""
    if 200 <= status < 300:
        return Result("ok")
    if status == 429 and code in ("budget_exceeded", "rate_limit_exceeded"):
        return Result(code, throttled=True, retry_after=parse_retry_after(headers or {}))
    if status == 403 and code in ("wrong_credential", "client_not_registered", "client_disabled"):
        return Result("auth_error", error=f"HTTP 403 {code}", fatal=True)
    if status == 403 and code == "model_not_allowed":
        return Result("client_error", error="HTTP 403 model_not_allowed: the client may not use that alias")
    if status == 404 and code == "model_not_found":
        return Result("client_error", error="HTTP 404 model_not_found: no such alias on the gateway")
    if status == 429 and code in ("upstream_rate_limited", "too_many_failures"):
        return Result("rate_limited", throttled=True, retry_after=parse_retry_after(headers or {}))
    if status == 400 and code:
        return Result("client_error", error=f"HTTP 400 {code}")
    return classify_status(status, headers)


class LlmgwSender(BaseSender):
    def __init__(self, params: dict, http, rng):
        super().__init__(params, http, rng)
        self.seq = 0

    def plan(self, n: int) -> list:
        ops = plan_calls(self.rng, n, self.params, self.seq)
        self.seq += n
        return ops

    async def execute(self, op) -> Result:
        path = "/v1/chat/completions" if isinstance(op, ChatOp) else "/v1/embeddings"
        r, latency, err = await self.http.call("POST", path, json=op.body)
        if r is None:
            return network_result(latency, err)
        res = classify_llm(r.status_code, json_error_code(r), r.headers)
        res.latency = latency
        return res
