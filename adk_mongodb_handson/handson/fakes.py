"""API キー不要で再現できる決定的なテストダブル (Embedding / LLM)。

重要: FakeGenaiClient はトークンのハッシュ化による *字句的* ベクトルを返すだけで、
意味的な類似度 (同義語・多言語) は再現しない。本物の Embedding モデルの品質検証は
README の「未検証事項」を参照。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from types import SimpleNamespace
from typing import Any

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.genai import types

EMBEDDING_DIM = 64


def fake_embed(text: str, dim: int = EMBEDDING_DIM) -> list[float]:
    vec = [0.0] * dim
    for token in re.findall(r"\w+", text.lower()):
        vec[int(hashlib.md5(token.encode()).hexdigest(), 16) % dim] += 1.0
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


class _FakeModels:
    async def embed_content(self, model: str, contents: list[str], config: Any = None):
        dim = getattr(config, "output_dimensionality", None) or EMBEDDING_DIM
        return SimpleNamespace(
            embeddings=[SimpleNamespace(values=fake_embed(c, dim)) for c in contents]
        )


class FakeGenaiClient:
    """`google.genai.Client` の `aio.models.embed_content` だけを模倣する。"""

    def __init__(self) -> None:
        self.aio = SimpleNamespace(models=_FakeModels())


def _text(content: types.Content | None) -> str:
    if not content or not content.parts:
        return ""
    return " ".join(p.text for p in content.parts if p.text)


class ScriptedLlm(BaseLlm):
    """外部 API を使わない決定的な LLM。

    - `tool_name` が設定されていれば、ユーザー発話を引数 `query` にして 1 回ツールを呼ぶ。
    - ツール結果を受け取ったら、結果行の `summary_field` を連結して最終回答にする。
    - ツールなしなら `echo: <発話>` を返す。
    """

    model: str = "scripted"
    tool_name: str | None = None
    tool_args: dict[str, Any] = {}
    summary_field: str = "name"

    @classmethod
    def supported_models(cls) -> list[str]:
        return ["scripted"]

    async def generate_content_async(self, llm_request, stream: bool = False):
        part = llm_request.contents[-1].parts[0]
        if part.function_response is not None:
            yield self._reply(self._summarize(part.function_response.response))
        elif self.tool_name:
            args = json.loads(
                json.dumps(self.tool_args).replace("{query}", (part.text or "").replace('"', "'"))
            )
            call = types.FunctionCall(name=self.tool_name, args=args)
            yield LlmResponse(
                content=types.Content(role="model", parts=[types.Part(function_call=call)])
            )
        else:
            yield self._reply("echo: " + (part.text or ""))

    def _summarize(self, response: Any) -> str:
        rows = (response or {}).get("rows")
        if rows is None:
            return "result: " + json.dumps(response, default=str, ensure_ascii=False)
        names = [str(r.get(self.summary_field)) for r in rows]
        return "search results: " + ", ".join(names) if names else "search results: (none)"

    @staticmethod
    def _reply(text: str) -> LlmResponse:
        return LlmResponse(content=types.Content(role="model", parts=[types.Part(text=text)]))
