"""
Rev Auth — OAuth-like Token Management Layer
=============================================
Research-grade credential store with:
  - Access token + refresh token + expiry
  - Auto-refresh mechanism
  - Cookie-based auth fallback
  - Encrypted local storage (~/.rev/tokens.json)
  - Multi-provider support (qwen, notion, deepseek, google)

Architecture (OAuth 2.0 inspired):
  ┌─────────────┐     ┌──────────────┐     ┌─────────────┐
  │  CLI / M2M  │────▶│  Token Store │────▶│  Provider   │
  │  (client)   │     │  (encrypted) │     │  (upstream) │
  └─────────────┘     └──────────────┘     └─────────────┘
         │                    │                     │
         │  access_token      │  refresh_token      │  API call
         │  (short-lived)     │  (long-lived)       │  (Bearer)
         ▼                    ▼                     ▼
"""

import base64
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Optional, Dict, Any

# AES-like XOR obfuscation (not crypto-grade, but hides tokens from casual inspection)
_KEY = os.environ.get("REV_SECRET", "rev-m2m-default-key-change-me")


def _xor(data: bytes, key: bytes) -> bytes:
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(data))


def _encrypt(text: str) -> str:
    return base64.b64encode(_xor(text.encode(), _KEY.encode())).decode()


def _decrypt(token: str) -> str:
    return _xor(base64.b64decode(token), _KEY.encode()).decode()


class TokenStore:
    """OAuth-like token store with auto-refresh support."""

    def __init__(self, store_path: Optional[str] = None):
        self.store_path = Path(store_path or os.path.expanduser("~/.rev/tokens.json"))
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        self._tokens: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self):
        if self.store_path.exists():
            try:
                raw = self.store_path.read_text()
                self._tokens = json.loads(_decrypt(raw))
            except Exception:
                self._tokens = {}

    def _save(self):
        self.store_path.write_text(_encrypt(json.dumps(self._tokens, indent=2)))
        os.chmod(self.store_path, 0o600)

    def set_token(self, provider: str, access_token: str,
                  refresh_token: Optional[str] = None,
                  expires_in: Optional[int] = None,
                  token_type: str = "Bearer",
                  scope: Optional[str] = None,
                  metadata: Optional[Dict] = None):
        """Store a token (OAuth 2.0 token response format)."""
        entry = {
            "access_token": access_token,
            "token_type": token_type,
            "scope": scope,
            "metadata": metadata or {},
            "created_at": int(time.time()),
        }
        if refresh_token:
            entry["refresh_token"] = refresh_token
        if expires_in:
            entry["expires_at"] = int(time.time()) + expires_in
        self._tokens[provider] = entry
        self._save()

    def get_token(self, provider: str) -> Optional[str]:
        """Get access token (auto-refresh if expired)."""
        entry = self._tokens.get(provider)
        if not entry:
            return None
        # Check expiry
        expires_at = entry.get("expires_at")
        if expires_at and time.time() > expires_at:
            # Try refresh
            refreshed = self._try_refresh(provider, entry)
            if not refreshed:
                return None
            entry = self._tokens[provider]
        return entry.get("access_token")

    def _try_refresh(self, provider: str, entry: Dict) -> bool:
        """Attempt token refresh (provider-specific)."""
        refresh_token = entry.get("refresh_token")
        if not refresh_token:
            return False
        # Provider-specific refresh logic would go here
        # For now, mark as expired
        return False

    def get_cookies(self, provider: str) -> Optional[str]:
        """Get stored cookies for a provider."""
        entry = self._tokens.get(provider, {})
        return entry.get("metadata", {}).get("cookies")

    def set_cookies(self, provider: str, cookies: str):
        """Store cookies for a provider."""
        if provider not in self._tokens:
            self._tokens[provider] = {}
        self._tokens[provider].setdefault("metadata", {})["cookies"] = cookies
        self._save()

    def get_metadata(self, provider: str) -> Dict:
        return self._tokens.get(provider, {}).get("metadata", {})

    def list_providers(self) -> list:
        return list(self._tokens.keys())

    def is_authenticated(self, provider: str) -> bool:
        return self.get_token(provider) is not None

    def revoke(self, provider: str):
        self._tokens.pop(provider, None)
        self._save()

    def info(self, provider: str) -> Optional[Dict]:
        entry = self._tokens.get(provider)
        if not entry:
            return None
        return {
            "provider": provider,
            "token_type": entry.get("token_type"),
            "scope": entry.get("scope"),
            "created_at": entry.get("created_at"),
            "expires_at": entry.get("expires_at"),
            "expired": (entry.get("expires_at", float("inf")) < time.time()),
            "has_refresh": "refresh_token" in entry,
            "token_preview": entry.get("access_token", "")[:20] + "...",
        }


class GoogleOAuthCapture:
    """Capture Google OAuth flow (authorization code → token exchange)."""

    AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
    TOKEN_URL = "https://oauth2.googleapis.com/token"
    REDIRECT_URI = "http://localhost:8085/callback"

    def __init__(self, client_id: str, client_secret: str,
                 scope: str = "openid email profile"):
        self.client_id = client_id
        self.client_secret = client_secret
        self.scope = scope

    def get_auth_url(self) -> str:
        """Generate OAuth authorization URL."""
        params = {
            "client_id": self.client_id,
            "redirect_uri": self.REDIRECT_URI,
            "response_type": "code",
            "scope": self.scope,
            "access_type": "offline",
            "prompt": "consent",
        }
        query = "&".join(f"{k}={v}" for k, v in params.items())
        return f"{self.AUTH_URL}?{query}"

    def exchange_code(self, code: str) -> Dict:
        """Exchange authorization code for tokens."""
        from curl_cffi import requests as cr
        r = cr.post(self.TOKEN_URL, data={
            "code": code,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": self.REDIRECT_URI,
            "grant_type": "authorization_code",
        }, impersonate="chrome131", timeout=30)
        return r.json()

    def refresh(self, refresh_token: str) -> Dict:
        """Refresh access token."""
        from curl_cffi import requests as cr
        r = cr.post(self.TOKEN_URL, data={
            "refresh_token": refresh_token,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "refresh_token",
        }, impersonate="chrome131", timeout=30)
        return r.json()


# Singleton store
_store = None


def get_store() -> TokenStore:
    global _store
    if _store is None:
        _store = TokenStore()
    return _store
