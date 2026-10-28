"""Shared EOB_v9 / charge-sheet Azure Key Vault -> Anthropic Foundry auth.

Normal internal developer flow:
    az login
    python run_v3.py ...

The Anthropic credential is never stored in this package. DefaultAzureCredential
uses the signed-in Azure identity to read the existing Key Vault secret, then
constructs the Anthropic Foundry client. Environment variables may override the
shared infrastructure defaults for deployment.
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


def get_kv_client() -> SecretClient:
    return SecretClient(
        vault_url=KEY_VAULT_URL,
        credential=DefaultAzureCredential(),
    )


def get_secret(kv: SecretClient, name: str) -> str:
    value = kv.get_secret(name).value
    if not value:
        raise RuntimeError(f"Key Vault secret {name!r} is empty")
    return value


def get_anthropic_client(kv: SecretClient | None = None):
    kv = kv or get_kv_client()
    api_key = get_secret(kv, ANTHROPIC_KEY_SECRET)
    logger.info("Loaded Anthropic credential from shared EOB_v9 Key Vault path")
    return anthropic.AnthropicFoundry(api_key=api_key, base_url=FOUNDRY_ENDPOINT)
