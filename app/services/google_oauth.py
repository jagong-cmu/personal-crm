"""Google OAuth: consent URL, code exchange, and credential load/refresh.

Slice 0 scope: contacts.readonly only. Gmail/Calendar scopes are added in later
slices (Gmail uses gmail.metadata per the privacy decision).

Tokens are stored Fernet-encrypted in oauth_credentials. In Google "Testing"
publishing status the refresh token expires after 7 days, so re-running the
consent flow weekly is expected for v1 (see PLAN.md, NOT-in-scope).
"""
from __future__ import annotations

import os

# Local-dev OAuth relaxations, read by oauthlib at token-exchange time:
#  - RELAX_TOKEN_SCOPE: Google may return scopes in a different order or include
#    previously-granted ones (we pass include_granted_scopes), which otherwise
#    raises "Scope has changed" and 500s the callback.
#  - INSECURE_TRANSPORT: allow the http://localhost redirect URI without HTTPS.
# Both are safe ONLY for local single-user dev. Remove for any real deployment.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")

from datetime import datetime, timezone

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import OAuthCredential
from app.services.crypto import decrypt, encrypt

SCOPES = ["https://www.googleapis.com/auth/contacts.readonly"]
_TOKEN_URI = "https://oauth2.googleapis.com/token"


def _flow() -> Flow:
    s = get_settings()
    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": s.google_client_id,
                "client_secret": s.google_client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": _TOKEN_URI,
                "redirect_uris": [s.google_redirect_uri],
            }
        },
        scopes=SCOPES,
        # Disable PKCE: we build a fresh Flow per request, so the start request's
        # code_verifier wouldn't survive to the callback ("Missing code verifier").
        # We're a confidential client (client_secret), so PKCE is optional and the
        # secret already protects the exchange.
        autogenerate_code_verifier=False,
    )
    flow.redirect_uri = s.google_redirect_uri
    return flow


def authorization_url() -> tuple[str, str]:
    """Return (consent_url, state). access_type=offline + prompt=consent to get a refresh token."""
    url, state = _flow().authorization_url(
        access_type="offline", include_granted_scopes="true", prompt="consent"
    )
    return url, state


def exchange_and_store(db: Session, tenant_id, code: str) -> OAuthCredential:
    flow = _flow()
    flow.fetch_token(code=code)
    creds = flow.credentials

    row = (
        db.query(OAuthCredential)
        .filter_by(tenant_id=tenant_id, provider="google")
        .one_or_none()
    )
    if row is None:
        row = OAuthCredential(tenant_id=tenant_id, provider="google")
        db.add(row)

    row.encrypted_access_token = encrypt(creds.token)
    if creds.refresh_token:  # only returned on first consent / prompt=consent
        row.encrypted_refresh_token = encrypt(creds.refresh_token)
    row.scopes = list(creds.scopes or SCOPES)
    row.expires_at = creds.expiry.replace(tzinfo=timezone.utc) if creds.expiry else None
    db.commit()
    return row


def load_credentials(db: Session, tenant_id) -> Credentials:
    """Rebuild google Credentials from storage, refreshing + persisting if expired."""
    s = get_settings()
    row = (
        db.query(OAuthCredential)
        .filter_by(tenant_id=tenant_id, provider="google")
        .one_or_none()
    )
    if row is None or not row.encrypted_refresh_token:
        raise RuntimeError("Google not connected. Visit /auth/google/start first.")

    creds = Credentials(
        token=decrypt(row.encrypted_access_token) if row.encrypted_access_token else None,
        refresh_token=decrypt(row.encrypted_refresh_token),
        token_uri=_TOKEN_URI,
        client_id=s.google_client_id,
        client_secret=s.google_client_secret,
        scopes=row.scopes,
    )

    if not creds.valid:
        creds.refresh(Request())
        row.encrypted_access_token = encrypt(creds.token)
        row.expires_at = (
            creds.expiry.replace(tzinfo=timezone.utc) if creds.expiry else None
        )
        db.commit()
    return creds
