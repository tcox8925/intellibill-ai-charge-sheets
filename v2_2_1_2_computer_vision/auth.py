"""Shared Azure Key Vault -> Anthropic Foundry authentication.

This keeps the charge-sheet package compatible with the shared auth pattern used
by the earlier chargesheet/EOB pipeline: `az login` supplies the Azure identity,
the Anthropic key stays in Key Vault, and the Foundry endpoint is not a secret.
Environment variables may override the shared defaults when needed.
"""
from __future__ import annotations

import logging
import os

import anthropic
from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient

logger = logging.getLogger("chargesheet.auth")

KEY_VAULT_URL = os.getenv(
    "AZURE_KEY_VAULT_URL",
    "https://keyvault-834analytics.vault.azure.net/",
).strip()
FOUNDRY_ENDPOINT = os.getenv(
    "ANTHROPIC_FOUNDRY_ENDPOINT",
    "https://sql-test-resource.services.ai.azure.com/anthropic/",
).strip()
ANTHROPIC_KEY_SECRET = os.getenv("ANTHROPIC_KEY_SECRET", "834-claude-key").strip()
OPUS_MODEL = os.getenv("CHARGESHEET_HEADER_MODEL", "claude-opus-4-6").strip()
HAIKU_MODEL = os.getenv("CHARGE_HAIKU", "claude-haiku-4-5").strip()


def get_kv_client() -> SecretClient:
    return SecretClient(vault_url=KEY_VAULT_URL, credential=DefaultAzureCredential())


def get_secret(kv: SecretClient, name: str) -> str:
    return kv.get_secret(name).value


def get_anthropic_client(kv: SecretClient | None = None):
    kv = kv or get_kv_client()
    api_key = get_secret(kv, ANTHROPIC_KEY_SECRET)
    logger.info("Loaded Anthropic credential from shared Key Vault")
    return anthropic.AnthropicFoundry(api_key=api_key, base_url=FOUNDRY_ENDPOINT)


def get_haiku_client(kv: SecretClient | None = None):
    # Compatibility alias retained for callers from the earlier shared auth module.
    return get_anthropic_client(kv)
