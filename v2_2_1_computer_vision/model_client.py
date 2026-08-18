"""Model client shared by header/notes and constrained mark review.

Credential order:
1) ANTHROPIC_API_KEY (+ optional ANTHROPIC_BASE_URL) when explicitly supplied.
2) Shared auth.py: Azure DefaultAzureCredential -> Key Vault -> Anthropic Foundry.

The normal internal test path is therefore simply `az login` followed by run.py.
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
            "Could not initialize the shared Anthropic client. Run `az login` and "
            "make sure you can read secret '834-claude-key' from the shared Key Vault. "
            "Alternatively set ANTHROPIC_API_KEY (+ optional ANTHROPIC_BASE_URL). "
            f"Underlying error: {exc}"
        ) from exc
