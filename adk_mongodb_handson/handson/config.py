"""接続先の解決。接続情報は環境変数 (.env) から読み、コードに埋め込まない。"""

from __future__ import annotations

import os
from urllib.parse import quote_plus

from dotenv import load_dotenv

load_dotenv()

# 認証情報を含まないローカル検証用の既定値 (docker-compose.yml のポートと対応)
_DEFAULT_URIS = {
    "standalone": "mongodb://localhost:27117/?directConnection=true",
    "replicaset": "mongodb://localhost:27118/?directConnection=true",
    "atlas_local": "mongodb://localhost:27119/?directConnection=true",
}


def get_uri(kind: str) -> str:
    """kind: standalone / replicaset / atlas_local / atlas"""
    value = os.environ.get(f"MONGODB_URI_{kind.upper()}")
    if value:
        return value
    if kind in _DEFAULT_URIS:
        return _DEFAULT_URIS[kind]
    raise RuntimeError(f"MONGODB_URI_{kind.upper()} が未設定です (.env を確認してください)")


def get_auth_uri(user: str, password_env: str) -> str:
    """auth profile 用の URI をユーザー名と環境変数名から組み立てる。"""
    password = os.environ.get(password_env)
    if not password:
        raise RuntimeError(f"{password_env} が未設定です (.env を確認してください)")
    port = os.environ.get("MONGO_AUTH_PORT", "27120")
    db = os.environ.get("HANDSON_AUTH_DB", "adk_handson_auth")
    return (
        f"mongodb://{quote_plus(user)}:{quote_plus(password)}@localhost:{port}/"
        f"{db}?directConnection=true&authSource={db}"
    )


def auth_db_name() -> str:
    return os.environ.get("HANDSON_AUTH_DB", "adk_handson_auth")
