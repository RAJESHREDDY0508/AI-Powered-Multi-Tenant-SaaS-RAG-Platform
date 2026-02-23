"""
LLM Gateway Package

Provides a provider-agnostic interface over multiple LLM backends:
  - OpenAI           (GPT-4o, GPT-4o-mini)
  - Azure OpenAI     (same models, different endpoint — failover target)
  - AWS Bedrock      (Claude 3.5, Llama 3 — data-residency option)
  - Ollama / Local   (Llama 3, Mistral — air-gapped / privacy mode)

Public API::

    from app.llm import LLMGateway, ModelRequirements

    gateway = LLMGateway()
    response = await gateway.invoke(prompt_messages, requirements=ModelRequirements())
    # or
    async for token in gateway.stream(prompt_messages):
        ...
"""

from app.llm.gateway import LLMGateway
from app.llm.router import ModelRequirements, ModelSpec, PrivacyLevel, RoutingStrategy

__all__ = [
    "LLMGateway",
    "ModelRequirements",
    "ModelSpec",
    "PrivacyLevel",
    "RoutingStrategy",
]
