"""
Shared authentication — Azure Key Vault, Postgres, Claude, Embeddings.
v8: unchanged from v7.
"""

import logging
import os
import anthropic
import psycopg2
from openai import AzureOpenAI
from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("eob.auth")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
KEY_VAULT_URL = "https://keyvault-834analytics.vault.azure.net/"

# Claude Opus — extraction only (Stage 2, Class 2/3)
FOUNDRY_ENDPOINT = "https://sql-test-resource.services.ai.azure.com/anthropic/"
OPUS_MODEL = "claude-opus-4-6"

# Haiku — page classification (Stage 1) + correspondence extraction (Class 4)
HAIKU_MODEL = "claude-haiku-4-5"

# Embeddings — carrier schema matching
EMBEDDING_ENDPOINT = os.environ.get(
    "EMBEDDING_ENDPOINT",
    "https://sql-test-resource.cognitiveservices.azure.com/",
)
EMBEDDING_DEPLOYMENT = os.environ.get("EMBEDDING_DEPLOYMENT", "text-embedding-3-small")
EMBEDDING_API_VERSION = os.environ.get("EMBEDDING_API_VERSION", "2024-12-01-preview")

# Postgres
DB_HOST = os.environ.get("DB_HOST", "pch-db-dev001.postgres.database.azure.com")
DB_NAME = os.environ.get("DB_NAME", "postgres")
DB_USER = os.environ.get("DB_USERNAME") or os.environ.get("DB_USER", "834data_syndb_adm")
DB_PASSWORD = os.environ.get("DB_PASSWORD")
DB_PORT = int(os.environ.get("DB_PORT", "5432"))
PG_CONNECTION_STRING = os.environ.get("PG_CONNECTION_STRING") or os.environ.get("DATABASE_URL")


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if value:
        return value
    raise RuntimeError(f"Missing required environment variable: {name}")


def get_kv_client() -> SecretClient:
    return SecretClient(vault_url=KEY_VAULT_URL, credential=DefaultAzureCredential())


def get_secret(kv: SecretClient, name: str) -> str:
    return kv.get_secret(name).value


def get_anthropic_client(kv: SecretClient) -> anthropic.AnthropicFoundry:
    api_key = _required_env("ANTHROPIC_API_KEY")
    logger.info("Loaded Claude API key")
    return anthropic.AnthropicFoundry(api_key=api_key, base_url=FOUNDRY_ENDPOINT)


def get_haiku_client(kv: SecretClient = None) -> anthropic.AnthropicFoundry:
    api_key = _required_env("ANTHROPIC_API_KEY")
    client = anthropic.AnthropicFoundry(api_key=api_key, base_url=FOUNDRY_ENDPOINT)
    logger.info("Haiku client ready")
    return client


def get_embedding_client(kv: SecretClient = None) -> AzureOpenAI:
    client = AzureOpenAI(
        azure_endpoint=EMBEDDING_ENDPOINT,
        api_key=_required_env("EMBEDDING_API_KEY"),
        api_version=EMBEDDING_API_VERSION,
    )
    logger.info("Embedding client ready")
    return client


def _pg_connect_args() -> dict:
    if PG_CONNECTION_STRING:
        return {"dsn": PG_CONNECTION_STRING}

    if DB_HOST and DB_NAME and DB_USER and DB_PASSWORD:
        return {
            "host": DB_HOST,
            "dbname": DB_NAME,
            "user": DB_USER,
            "password": DB_PASSWORD,
            "port": DB_PORT,
            "sslmode": "require",
        }

    raise RuntimeError(
        "Missing database configuration: set PG_CONNECTION_STRING or all of "
        "DB_HOST, DB_PORT, DB_NAME, DB_USERNAME/DB_USER, and DB_PASSWORD"
    )


def get_pg_connection(kv: SecretClient) -> psycopg2.extensions.connection:
    connect_args = _pg_connect_args()
    conn = psycopg2.connect(**connect_args)
    conn.autocommit = False
    logger.info("Connected to Postgres")
    return conn


def check_db_connection() -> dict:
    """Return Postgres connectivity status for the configured database."""
    try:
        connect_args = _pg_connect_args()
        with psycopg2.connect(**connect_args) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT current_database(), current_user")
                database_name, current_user = cur.fetchone()
        return {
            "connected": True,
            "host": DB_HOST,
            "port": DB_PORT,
            "database": database_name,
            "user": current_user,
        }
    except Exception as exc:
        return {
            "connected": False,
            "host": DB_HOST,
            "port": DB_PORT,
            "database": DB_NAME,
            "user": DB_USER,
            "error": str(exc),
        }


def reconnect_if_stale(conn, kv: SecretClient) -> psycopg2.extensions.connection:
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
        return conn
    except Exception as e:
        logger.warning(f"DB connection stale ({e}) — reconnecting...")
        try:
            conn.close()
        except Exception:
            pass
        return get_pg_connection(kv)
