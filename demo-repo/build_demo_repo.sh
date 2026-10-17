#!/usr/bin/env bash
# Builds the seeded demo repo "shop-api": a small FastAPI app whose git history tells a story
# (sessions -> JWT decision, pyjwt bump, aud-mismatch bug + fix, a reverted attempt, a new 401).
# Usage: bash build_demo_repo.sh [target_dir]   (default: ./shop-api next to this script)
set -euo pipefail

DIR="${1:-$(cd "$(dirname "$0")" && pwd)/shop-api}"
rm -rf "$DIR"
mkdir -p "$DIR"
cd "$DIR"
git init -q -b main
git config user.name "Demo"
git config user.email "demo@example.com"

commit() { # commit "<date>" "<author name>" "<author email>" "<message>"
  git add -A
  GIT_AUTHOR_DATE="$1" GIT_COMMITTER_DATE="$1" \
    git -c user.name="$2" -c user.email="$3" commit -q -m "$4"
}

ALICE=("Alice Chen" "alice@shop.example")
BOB=("Bob Okafor" "bob@shop.example")
PRIYA=("Priya Raman" "priya@shop.example")

# ------------------------------------------------------------------ 1. initial app, session auth
mkdir -p app/auth app/orders app/users tests
touch app/__init__.py app/auth/__init__.py app/orders/__init__.py app/users/__init__.py tests/__init__.py

cat > .gitignore <<'EOF'
.venv/
__pycache__/
.pytest_cache/
EOF

cat > requirements.txt <<'EOF'
fastapi==0.110.0
httpx==0.27.0
pytest==8.1.1
EOF

cat > app/main.py <<'EOF'
from fastapi import FastAPI

from app.auth.routes import router as auth_router
from app.orders.router import router as orders_router
from app.users.router import router as users_router

app = FastAPI(title="shop-api")
app.include_router(auth_router)
app.include_router(orders_router)
app.include_router(users_router)
EOF

cat > app/auth/session.py <<'EOF'
import secrets

SESSIONS: dict[str, str] = {}


def create_session(username: str) -> str:
    sid = secrets.token_urlsafe(16)
    SESSIONS[sid] = username
    return sid


def get_session_user(sid: str) -> str | None:
    return SESSIONS.get(sid)
EOF

cat > app/auth/middleware.py <<'EOF'
from fastapi import Cookie, HTTPException

from app.auth.session import get_session_user


def current_user(session_id: str = Cookie(default="")) -> dict:
    user = get_session_user(session_id)
    if not user:
        raise HTTPException(401, "Not logged in")
    return {"sub": user}
EOF

cat > app/auth/routes.py <<'EOF'
from fastapi import APIRouter, Response

from app.auth.session import create_session

router = APIRouter(prefix="/api/auth")


@router.post("/login")
def login(username: str, response: Response):
    response.set_cookie("session_id", create_session(username))
    return {"ok": True}
EOF

cat > app/users/router.py <<'EOF'
from fastapi import APIRouter, Depends

from app.auth.middleware import current_user

router = APIRouter(prefix="/api/users")


@router.get("/me")
def me(user: dict = Depends(current_user)):
    return {"username": user["sub"]}
EOF

cat > app/orders/router.py <<'EOF'
from fastapi import APIRouter, Depends

from app.auth.middleware import current_user

router = APIRouter(prefix="/api/orders")

ORDERS = [
    {"id": 1, "user": "alice", "total": 42.0},
    {"id": 2, "user": "bob", "total": 13.5},
]


@router.get("")
def list_orders(user: dict = Depends(current_user)):
    return [o for o in ORDERS if o["user"] == user["sub"]]
EOF

cat > tests/conftest.py <<'EOF'
import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client():
    return TestClient(app)
EOF

cat > tests/test_users.py <<'EOF'
def test_me_requires_login(client):
    assert client.get("/api/users/me").status_code == 401
EOF
commit "2026-04-02T10:00:00" "${ALICE[@]}" "Initial shop-api: users and orders with session-based auth"

# ------------------------------------------------------------------ small noise commit
cat > README.md <<'EOF'
# shop-api

Run: `uvicorn app.main:app --reload`
Test: `pytest`
EOF
commit "2026-04-10T15:20:00" "${BOB[@]}" "Add README"

# ------------------------------------------------------------------ 2. ADR + switch to JWT
mkdir -p docs/adr
cat > docs/adr/001-jwt-auth.md <<'EOF'
# ADR 001: Switch auth from server sessions to JWT (RS256)

Status: Accepted (2026-05-12)

## Context
The mobile app is launching. Mobile clients cannot rely on cookies and we want the API
to scale horizontally without a shared session store. Session lookups were also a
per-request database hit.

## Decision
Use stateless JWT access tokens signed with RS256. The auth service holds the private key;
every other service (orders, users) only needs the public key. Tokens carry `sub`, `aud`,
`iss` and `exp`. Services MUST validate `aud`.

## Consequences
- No server-side logout; access tokens are short-lived (15 min) and refreshed.
- Every service that validates tokens must agree on the audience string.
EOF

rm app/auth/session.py
cat >> requirements.txt <<'EOF'
pyjwt==1.7.1
cryptography==42.0.5
EOF

cat > app/auth/keys.py <<'EOF'
"""Dev keypair generated at startup. Production loads these from the secret store."""
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PRIVATE_KEY = _key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                 serialization.NoEncryption())
PUBLIC_KEY = _key.public_key().public_bytes(serialization.Encoding.PEM,
                                            serialization.PublicFormat.SubjectPublicKeyInfo)
EOF

cat > app/auth/tokens.py <<'EOF'
import time

import jwt

from app.auth.keys import PRIVATE_KEY, PUBLIC_KEY

AUDIENCE = "shop-api"
ISSUER = "shop-auth"


def create_token(sub: str, ttl: int = 900) -> str:
    payload = {"sub": sub, "aud": AUDIENCE, "iss": ISSUER, "exp": int(time.time()) + ttl}
    return jwt.encode(payload, PRIVATE_KEY, algorithm="RS256").decode()


def decode_token(token: str) -> dict:
    return jwt.decode(token, PUBLIC_KEY, algorithms=["RS256"], audience=AUDIENCE, issuer=ISSUER)
EOF

cat > app/auth/middleware.py <<'EOF'
import jwt
from fastapi import Header, HTTPException

from app.auth.tokens import decode_token


def current_user(authorization: str = Header(default="")) -> dict:
    token = authorization.removeprefix("Bearer ").strip()
    try:
        return decode_token(token)
    except jwt.InvalidTokenError as e:
        raise HTTPException(401, f"Invalid token: {e}")
EOF

cat > app/auth/routes.py <<'EOF'
import time

import jwt
from fastapi import APIRouter

from app.auth.keys import PRIVATE_KEY
from app.auth.tokens import create_token, decode_token

router = APIRouter(prefix="/api/auth")

# The mobile identity provider issues refresh tokens for this audience.
MOBILE_AUDIENCE = "https://api.shop.example"


@router.post("/login")
def login(username: str):
    return {"access_token": create_token(username), "refresh_token": create_token(username, ttl=86400)}


@router.post("/refresh")
def refresh(refresh_token: str):
    claims = decode_token(refresh_token)
    payload = {"sub": claims["sub"], "aud": MOBILE_AUDIENCE, "iss": "shop-auth", "exp": int(time.time()) + 900}
    return {"access_token": jwt.encode(payload, PRIVATE_KEY, algorithm="RS256").decode()}
EOF

cat > app/orders/router.py <<'EOF'
import jwt
from fastapi import APIRouter, Depends, Header, HTTPException

from app.auth.keys import PUBLIC_KEY

router = APIRouter(prefix="/api/orders")

ORDERS = [
    {"id": 1, "user": "alice", "total": 42.0},
    {"id": 2, "user": "bob", "total": 13.5},
]


def order_claims(authorization: str = Header(default="")) -> dict:
    # Orders decodes the JWT itself: it is deployed separately and only has the public key.
    token = authorization.removeprefix("Bearer ").strip()
    try:
        return jwt.decode(token, PUBLIC_KEY, audience="shop-api")
    except jwt.InvalidTokenError as e:
        raise HTTPException(401, f"Invalid token: {e}")


@router.get("")
def list_orders(claims: dict = Depends(order_claims)):
    return [o for o in ORDERS if o["user"] == claims["sub"]]
EOF
commit "2026-05-12T11:30:00" "${ALICE[@]}" "Switch auth from sessions to JWT (RS256) because mobile clients need stateless auth

See docs/adr/001-jwt-auth.md. Removes the in-memory session store; orders validates
tokens locally with the public key."

# ------------------------------------------------------------------ 3. bump pyjwt 1.7 -> 2.8
sed -i 's/pyjwt==1.7.1/pyjwt==2.8.0/' requirements.txt
sed -i 's/algorithm="RS256").decode()/algorithm="RS256")/' app/auth/tokens.py app/auth/routes.py
sed -i 's/jwt.decode(token, PUBLIC_KEY, audience="shop-api")/jwt.decode(token, PUBLIC_KEY, algorithms=["RS256"], audience="shop-api")/' app/orders/router.py
commit "2026-06-03T09:10:00" "${BOB[@]}" "Bump pyjwt 1.7.1 -> 2.8.0

pyjwt 2 returns str from encode() and requires algorithms= on decode()."

# ------------------------------------------------------------------ scopes claim (more auth<->orders coupling)
sed -i 's/def create_token(sub: str, ttl: int = 900) -> str:/def create_token(sub: str, ttl: int = 900, scopes: tuple = ("orders:read",)) -> str:/' app/auth/tokens.py
sed -i 's/"exp": int(time.time()) + ttl}/"exp": int(time.time()) + ttl, "scopes": list(scopes)}/' app/auth/tokens.py
cat > app/orders/router.py <<'EOF'
import jwt
from fastapi import APIRouter, Depends, Header, HTTPException

from app.auth.keys import PUBLIC_KEY

router = APIRouter(prefix="/api/orders")

ORDERS = [
    {"id": 1, "user": "alice", "total": 42.0},
    {"id": 2, "user": "bob", "total": 13.5},
]


def order_claims(authorization: str = Header(default="")) -> dict:
    # Orders decodes the JWT itself: it is deployed separately and only has the public key.
    token = authorization.removeprefix("Bearer ").strip()
    try:
        return jwt.decode(token, PUBLIC_KEY, algorithms=["RS256"], audience="shop-api")
    except jwt.InvalidTokenError as e:
        raise HTTPException(401, f"Invalid token: {e}")


@router.get("")
def list_orders(claims: dict = Depends(order_claims)):
    if "orders:read" not in claims.get("scopes", []):
        raise HTTPException(403, "missing scope orders:read")
    return [o for o in ORDERS if o["user"] == claims["sub"]]
EOF
cat > tests/test_orders.py <<'EOF'
def test_orders_requires_token(client):
    assert client.get("/api/orders").status_code == 401
EOF
commit "2026-06-20T14:45:00" "${PRIYA[@]}" "Add scopes claim to access tokens; orders checks orders:read"

# ------------------------------------------------------------------ 4. the aud-mismatch fix
cat > app/auth/tokens.py <<'EOF'
import time

import jwt

from app.auth.keys import PRIVATE_KEY, PUBLIC_KEY

# NOTE: every service that validates our tokens must use AUDIENCE from this module, never a hardcoded string.
AUDIENCE = "https://api.shop.example"
ISSUER = "shop-auth"


def create_token(sub: str, ttl: int = 900, scopes: tuple = ("orders:read",)) -> str:
    payload = {"sub": sub, "aud": AUDIENCE, "iss": ISSUER, "exp": int(time.time()) + ttl, "scopes": list(scopes)}
    return jwt.encode(payload, PRIVATE_KEY, algorithm="RS256")


def decode_token(token: str) -> dict:
    return jwt.decode(token, PUBLIC_KEY, algorithms=["RS256"], audience=AUDIENCE, issuer=ISSUER)
EOF
cat > app/auth/routes.py <<'EOF'
from fastapi import APIRouter

from app.auth.tokens import create_token, decode_token

router = APIRouter(prefix="/api/auth")


@router.post("/login")
def login(username: str):
    return {"access_token": create_token(username), "refresh_token": create_token(username, ttl=86400)}


@router.post("/refresh")
def refresh(refresh_token: str):
    claims = decode_token(refresh_token)
    return {"access_token": create_token(claims["sub"], scopes=tuple(claims.get("scopes", [])))}
EOF
cat > tests/test_auth.py <<'EOF'
def test_login_and_refresh(client):
    tokens = client.post("/api/auth/login", params={"username": "alice"}).json()
    new = client.post("/api/auth/refresh", params={"refresh_token": tokens["refresh_token"]}).json()
    r = client.get("/api/users/me", headers={"Authorization": f"Bearer {new['access_token']}"})
    assert r.status_code == 200
EOF
commit "2026-07-15T17:05:00" "${BOB[@]}" "fix: 401 on token refresh — aud claim mismatch (closes #12)

Refresh minted access tokens with aud=https://api.shop.example (the mobile IdP audience)
while decode_token expected aud=shop-api, so every refreshed token failed with
jwt.InvalidAudienceError -> 401. Standardised on AUDIENCE = https://api.shop.example
in app/auth/tokens.py and made refresh reuse create_token()."

# ------------------------------------------------------------------ 5. failed attempt + revert
sed -i 's/return jwt.decode(token, PUBLIC_KEY, algorithms=\["RS256"\], audience=AUDIENCE, issuer=ISSUER)/return jwt.decode(token, PUBLIC_KEY, algorithms=["RS256"], issuer=ISSUER, options={"verify_aud": False})/' app/auth/tokens.py
commit "2026-07-22T19:40:00" "${PRIYA[@]}" "Disable aud verification to stop 401s from mobile clients"
BAD_SHA=$(git rev-parse HEAD)
git revert --no-commit HEAD
commit "2026-07-23T09:15:00" "${ALICE[@]}" "Revert \"Disable aud verification to stop 401s from mobile clients\"

This reverts commit ${BAD_SHA}.

Skipping audience verification accepts tokens minted for other services (security review
SEC-31). ADR 001 requires aud validation; fix the audience mismatch at the source instead."

# ------------------------------------------------------------------ noise
cat > app/users/router.py <<'EOF'
from fastapi import APIRouter, Depends

from app.auth.middleware import current_user

router = APIRouter(prefix="/api/users")


@router.get("/me")
def me(user: dict = Depends(current_user)):
    """Return the logged-in user's profile."""
    return {"username": user["sub"], "scopes": user.get("scopes", [])}
EOF
commit "2026-08-05T10:00:00" "${BOB[@]}" "Return scopes from /api/users/me"

# ------------------------------------------------------------------ 6. current state: orders 401s
cat > tests/test_orders.py <<'EOF'
def test_orders_requires_token(client):
    assert client.get("/api/orders").status_code == 401


def test_list_orders_authenticated(client):
    token = client.post("/api/auth/login", params={"username": "alice"}).json()["access_token"]
    r = client.get("/api/orders", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    assert [o["id"] for o in r.json()] == [1]
EOF
commit "2026-09-28T16:30:00" "${PRIYA[@]}" "test: cover authenticated order listing"

echo "Built demo repo at $DIR"
git --no-pager log --oneline
