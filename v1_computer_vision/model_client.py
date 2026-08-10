"""Minimal model client for header/notes transcription only.

Billing-code extraction never uses this client.

Credential options:
1) ANTHROPIC_API_KEY (+ optional ANTHROPIC_BASE_URL), or
2) Azure Key Vault via `az login` / DefaultAzureCredential using:
   AZURE_KEY_VAULT_URL and ANTHROPIC_FOUNDRY_ENDPOINT.
"""
from __future__ import annotations

import os

import anthropic

from settings import get_settings


def make_client():
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    base_url = os.getenv("ANTHROPIC_BASE_URL", "").strip()
    if api_key:
        if base_url:
            return anthropic.AnthropicFoundry(api_key=api_key, base_url=base_url)
        return anthropic.Anthropic(api_key=api_key)

    s = get_settings()
    if s.key_vault_url and s.anthropic_foundry_endpoint:
        from azure.identity import DefaultAzureCredential
        from azure.keyvault.secrets import SecretClient

        kv = SecretClient(vault_url=s.key_vault_url, credential=DefaultAzureCredential())
        key = kv.get_secret(s.anthropic_key_secret).value
        return anthropic.AnthropicFoundry(api_key=key, base_url=s.anthropic_foundry_endpoint)

    raise RuntimeError(
        "No header/notes model credentials configured. Either set ANTHROPIC_API_KEY "
        "(and optional ANTHROPIC_BASE_URL), or set AZURE_KEY_VAULT_URL and "
        "ANTHROPIC_FOUNDRY_ENDPOINT and authenticate with `az login`. "
        "Use --no-ai to run geometry-only extraction."
    )
