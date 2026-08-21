"""Anthropic client construction.

Credential precedence:
  1. Explicit ANTHROPIC_API_KEY override, when intentionally supplied.
  2. Shared EOB_v9 auth.py path:
       DefaultAzureCredential -> Azure Key Vault -> AnthropicFoundry.

The normal internal path is simply `az login` followed by run_v3.py. No secret
value is stored in this package.
"""
from __future__ import annotations

import os


def make_client():
    import anthropic

    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    base_url = os.getenv("ANTHROPIC_BASE_URL", "").strip()
    if api_key:
        if base_url:
            return anthropic.AnthropicFoundry(api_key=api_key, base_url=base_url)
        return anthropic.Anthropic(api_key=api_key)

    try:
        from auth import get_anthropic_client, get_kv_client
        return get_anthropic_client(get_kv_client())
    except Exception as exc:
        raise RuntimeError(
            "Could not initialize the shared EOB_v9 Anthropic client. "
            "Run `az login` and make sure your Azure identity can read the "
            "configured Anthropic secret from the shared Key Vault. "
            "Alternatively, intentionally set ANTHROPIC_API_KEY. "
            f"Underlying error: {exc}"
        ) from exc
