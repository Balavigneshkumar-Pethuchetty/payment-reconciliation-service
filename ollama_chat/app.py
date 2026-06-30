"""Ollama Chat — Gemini-style UI · Keycloak/Google auth · PostgreSQL history."""
import base64
import csv
import io
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone

import asyncpg
import httpx
import jwt as pyjwt
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

OLLAMA_BASE_URL   = os.getenv("OLLAMA_BASE_URL", "http://ollama:11434")
DEFAULT_MODEL     = os.getenv("OLLAMA_MODEL", "llava")
MAX_CTX_CHARS     = 8_000
DATABASE_URL      = os.getenv("DATABASE_URL", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_DEFAULT    = "claude-sonnet-4-6"
CLAUDE_MODELS     = ["claude-sonnet-4-6", "claude-haiku-4-5-20251001", "claude-opus-4-8"]

_anthropic_client = None

def _get_anthropic():
    global _anthropic_client
    if _anthropic_client is None:
        import anthropic
        _anthropic_client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    return _anthropic_client

# Keycloak — leave KC_URL empty to run without auth (anonymous mode)
KC_URL         = os.getenv("KEYCLOAK_URL", "").rstrip("/")
KC_REALM       = os.getenv("KEYCLOAK_REALM", "ollama-chat")
KC_CLIENT      = os.getenv("KEYCLOAK_CLIENT_ID", "ollama-chat-app")
# Internal URL for JWKS fetch — avoids Cloudflare which blocks server-side requests
KC_INTERNAL_URL = os.getenv("KEYCLOAK_INTERNAL_URL", KC_URL).rstrip("/")

_pool: asyncpg.Pool | None = None
_jwks_cache: dict = {"keys": [], "ts": 0.0}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


app = FastAPI(title="Ollama Chat")


# ── Startup / Shutdown ────────────────────────────────────────────────────────

@app.on_event("startup")
async def _startup():
    global _pool
    if not DATABASE_URL:
        return
    _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    async with _pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS chat_conversations (
                id         TEXT PRIMARY KEY,
                user_id    TEXT NOT NULL DEFAULT 'anonymous',
                title      TEXT NOT NULL DEFAULT 'New chat',
                messages   TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        # migrate: add user_id to existing tables
        await conn.execute("""
            ALTER TABLE chat_conversations
            ADD COLUMN IF NOT EXISTS user_id TEXT NOT NULL DEFAULT 'anonymous'
        """)


@app.on_event("shutdown")
async def _shutdown():
    if _pool:
        await _pool.close()


# ── Keycloak JWT verification ─────────────────────────────────────────────────

async def _get_jwks() -> list:
    if time.time() - _jwks_cache["ts"] < 3600 and _jwks_cache["keys"]:
        return _jwks_cache["keys"]
    # Use internal URL so the request doesn't go through Cloudflare (which blocks it)
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(f"{KC_INTERNAL_URL}/realms/{KC_REALM}/protocol/openid-connect/certs")
        r.raise_for_status()
        _jwks_cache["keys"] = r.json()["keys"]
        _jwks_cache["ts"]   = time.time()
    return _jwks_cache["keys"]


async def _verify_jwt(token: str) -> dict:
    keys  = await _get_jwks()
    hdr   = pyjwt.get_unverified_header(token)
    key   = next((k for k in keys if k.get("kid") == hdr.get("kid")), None)
    if not key:
        raise HTTPException(401, "Unknown signing key")
    pub = pyjwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(key))
    # Skip audience check — Keycloak sets aud=["account"], not the client ID
    return pyjwt.decode(token, pub, algorithms=["RS256"],
                        options={"verify_aud": False})


async def _get_user(authorization: str = Header(default="")) -> dict:
    """FastAPI dependency — returns user dict; raises 401 when auth is misconfigured."""
    if not KC_URL:
        # Auth not configured → anonymous mode
        return {"sub": "anonymous", "name": "User", "email": ""}
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Not authenticated")
    try:
        claims = await _verify_jwt(authorization[7:])
        return {
            "sub":     claims["sub"],
            "name":    claims.get("name") or claims.get("preferred_username", "User"),
            "email":   claims.get("email", ""),
            "picture": claims.get("picture", ""),
        }
    except pyjwt.ExpiredSignatureError:
        raise HTTPException(401, "Token expired")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(401, f"Invalid token: {e}")


# ── DB helpers ────────────────────────────────────────────────────────────────

async def _db_list(user_id: str) -> list[dict]:
    if not _pool:
        return []
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id,title,updated_at,messages FROM chat_conversations"
            " WHERE user_id=$1 ORDER BY updated_at DESC", user_id
        )
    return [{"id": r["id"], "title": r["title"], "updated_at": r["updated_at"],
             "message_count": len(json.loads(r["messages"]))} for r in rows]


async def _db_create(user_id: str) -> str:
    cid, now = str(uuid.uuid4()), _now()
    async with _pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO chat_conversations(id,user_id,title,messages,created_at,updated_at)"
            " VALUES($1,$2,'New chat','[]',$3,$3)",
            cid, user_id, now,
        )
    return cid


async def _db_get(cid: str, user_id: str) -> dict | None:
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id,title,messages,created_at,updated_at FROM chat_conversations"
            " WHERE id=$1 AND user_id=$2", cid, user_id
        )
    if not row:
        return None
    return {"id": row["id"], "title": row["title"], "messages": json.loads(row["messages"]),
            "created_at": row["created_at"], "updated_at": row["updated_at"]}


async def _db_update(cid: str, user_id: str, messages: list, title: str = "") -> bool:
    async with _pool.acquire() as conn:
        r = await conn.execute(
            "UPDATE chat_conversations SET messages=$1,"
            " title=CASE WHEN $2!='' THEN $2 ELSE title END, updated_at=$3"
            " WHERE id=$4 AND user_id=$5",
            json.dumps(messages), title, _now(), cid, user_id,
        )
    return r != "UPDATE 0"


async def _db_delete(cid: str, user_id: str) -> None:
    async with _pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM chat_conversations WHERE id=$1 AND user_id=$2", cid, user_id
        )


# ── Auth config endpoint (public) ─────────────────────────────────────────────

@app.get("/auth/config")
async def auth_config():
    if not KC_URL:
        return {"enabled": False}
    return {
        "enabled":  True,
        "kcUrl":    KC_URL,
        "realm":    KC_REALM,
        "clientId": KC_CLIENT,
    }


# ── Conversation REST endpoints ───────────────────────────────────────────────

@app.get("/conversations")
async def list_conversations(user=Depends(_get_user)):
    return {"conversations": await _db_list(user["sub"])}


@app.post("/conversations")
async def create_conversation(user=Depends(_get_user)):
    if not _pool:
        raise HTTPException(503, "Database not configured")
    return {"id": await _db_create(user["sub"])}


@app.get("/conversations/{cid}")
async def get_conversation(cid: str, user=Depends(_get_user)):
    data = await _db_get(cid, user["sub"])
    if not data:
        raise HTTPException(404, "Not found")
    return data


@app.put("/conversations/{cid}")
async def update_conversation(
    cid: str, messages: str = Form(...), title: str = Form(default=""),
    user=Depends(_get_user),
):
    ok = await _db_update(cid, user["sub"], json.loads(messages), title)
    if not ok:
        raise HTTPException(404, "Not found")
    return {"ok": True}


@app.delete("/conversations/{cid}")
async def delete_conversation(cid: str, user=Depends(_get_user)):
    await _db_delete(cid, user["sub"])
    return {"ok": True}


# ── Embedded UI ───────────────────────────────────────────────────────────────

_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ollama Chat</title>
<style>
:root{
  --bg:#131314;--sb:#1e1f20;--hover:#282a2c;--active:#35363a;
  --border:#3c4043;--text:#e3e3e3;--dim:#9aa0a6;
  --ubg:#282a2c;--err:#f28b82;--acc:#89b4f8;
}
*{box-sizing:border-box;margin:0;padding:0;}
html,body{height:100%;}
body{background:var(--bg);color:var(--text);font:14px/1.6 system-ui,sans-serif;overflow:hidden;}

/* ── loading overlay ── */
#loading{
  position:fixed;inset:0;background:var(--bg);
  display:flex;align-items:center;justify-content:center;z-index:200;
}
.spin{
  width:36px;height:36px;border:3px solid var(--border);
  border-top-color:var(--acc);border-radius:50%;
  animation:spin .8s linear infinite;
}
@keyframes spin{to{transform:rotate(360deg);}}

/* ── login screen ── */
#login-screen{
  position:fixed;inset:0;background:var(--bg);
  display:none;align-items:center;justify-content:center;z-index:100;
}
.login-card{
  width:100%;max-width:380px;padding:48px 40px;
  text-align:center;
}
.login-logo{margin:0 auto 20px;display:block;}
.login-h1{
  font-size:28px;font-weight:400;margin-bottom:8px;
  background:linear-gradient(90deg,#4285f4,#9c27b0);
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;
}
.login-sub{color:var(--dim);font-size:14px;margin-bottom:36px;}
.login-btn{
  display:flex;align-items:center;justify-content:center;gap:12px;
  width:100%;padding:13px 20px;border-radius:50px;
  font:15px/1 inherit;cursor:pointer;border:none;
  transition:opacity .15s,filter .15s;margin-bottom:12px;
}
.login-btn:hover{filter:brightness(1.08);}
.btn-google{background:#fff;color:#3c4043;font-weight:500;}
.btn-kc{background:var(--hover);color:var(--text);border:1px solid var(--border);}
.login-divider{display:flex;align-items:center;gap:12px;margin:4px 0;color:var(--dim);font-size:12px;}
.login-divider::before,.login-divider::after{content:'';flex:1;height:1px;background:var(--border);}
.btn-register{background:transparent;color:var(--acc);border:1px solid var(--acc);}
.btn-register:hover{background:rgba(137,180,248,.08);}
.login-note{color:var(--dim);font-size:12px;margin-top:28px;}

/* ── main app ── */
#app{display:none;height:100vh;flex-direction:row;}

/* sidebar */
#sb{
  width:240px;min-width:240px;background:var(--sb);
  display:flex;flex-direction:column;height:100%;
  transition:width .2s,min-width .2s;overflow:hidden;
}
#sb.col{width:64px;min-width:64px;}
#sb-head{display:flex;align-items:center;gap:8px;padding:12px 10px;flex-shrink:0;}
.hbtn{
  width:40px;height:40px;flex-shrink:0;background:none;border:none;
  color:var(--dim);border-radius:50%;cursor:pointer;
  display:flex;align-items:center;justify-content:center;
  transition:background .15s,color .15s;
}
.hbtn:hover{background:var(--hover);color:var(--text);}
.brand{display:flex;align-items:center;gap:9px;overflow:hidden;flex:1;}
.brand-txt{font-size:18px;font-weight:500;white-space:nowrap;}
#sb.col .brand{display:none;}
.sb-nav{padding:2px 8px;flex-shrink:0;}
.ni{
  display:flex;align-items:center;gap:14px;
  width:100%;background:none;border:none;color:var(--text);
  font:14px/1 inherit;cursor:pointer;
  padding:11px 14px;border-radius:50px;
  transition:background .12s;text-align:left;white-space:nowrap;
}
.ni:hover{background:var(--hover);}
.ni-ico{flex-shrink:0;display:flex;align-items:center;}
.ni-lbl{overflow:hidden;}
#sb.col .ni-lbl{display:none;}
#sb.col .ni{padding:11px;justify-content:center;}
.sb-sec{padding:0 8px;}
.sb-hdr{font-size:12px;font-weight:500;color:var(--dim);padding:10px 14px 3px;white-space:nowrap;overflow:hidden;}
#sb.col .sb-hdr{display:none;}
#clist{flex:1;overflow-y:auto;padding:0 8px;}
#clist::-webkit-scrollbar{width:4px;}
#clist::-webkit-scrollbar-thumb{background:var(--border);border-radius:4px;}
.glbl{font-size:12px;font-weight:500;color:var(--dim);padding:8px 14px 3px;white-space:nowrap;overflow:hidden;}
#sb.col .glbl{display:none;}
.ci{
  display:flex;align-items:center;gap:8px;
  padding:8px 14px;border-radius:50px;cursor:pointer;transition:background .12s;
}
.ci:hover{background:var(--hover);}
.ci.on{background:var(--active);}
.ci-info{flex:1;min-width:0;}
.ci-title{font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.ci-del{
  flex-shrink:0;background:none;border:none;color:var(--dim);
  cursor:pointer;padding:4px;border-radius:50%;
  opacity:0;transition:opacity .15s,color .15s;display:flex;align-items:center;
}
.ci:hover .ci-del{opacity:1;}
.ci-del:hover{color:var(--err);}
#sb.col .ci-info,.sb.col .ci-del{display:none;}
#sb.col .ci{justify-content:center;padding:10px;}
#sb-foot{flex-shrink:0;border-top:1px solid var(--border);padding:8px;}
.prow{
  display:flex;align-items:center;gap:10px;
  padding:8px 10px;border-radius:50px;cursor:pointer;transition:background .12s;
}
.prow:hover{background:var(--hover);}
#pav{
  width:32px;height:32px;border-radius:50%;flex-shrink:0;
  background:linear-gradient(135deg,#4285f4,#9c27b0);
  display:flex;align-items:center;justify-content:center;
  font-size:13px;font-weight:600;color:#fff;overflow:hidden;
}
#pav img{width:32px;height:32px;border-radius:50%;object-fit:cover;}
#pname{flex:1;font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
#sb.col #pname{display:none;}
#sb.col .prow{justify-content:center;padding:8px;}
#logout-btn{
  flex-shrink:0;width:30px;height:30px;background:none;border:none;
  color:var(--dim);border-radius:50%;cursor:pointer;
  display:flex;align-items:center;justify-content:center;
  transition:background .15s,color .15s;
}
#logout-btn:hover{background:var(--hover);color:var(--err);}
#sb.col #logout-btn{display:none;}

/* main */
#main{flex:1;display:flex;flex-direction:column;overflow:hidden;min-width:0;position:relative;}
#top-acts{position:absolute;top:10px;right:12px;display:flex;gap:2px;z-index:10;}
.ibtn{
  width:40px;height:40px;background:none;border:none;color:var(--dim);border-radius:50%;
  cursor:pointer;display:flex;align-items:center;justify-content:center;
  transition:background .15s,color .15s;
}
.ibtn:hover{background:var(--hover);color:var(--text);}

/* messages */
#msgs{flex:1;overflow-y:auto;padding:28px 0 8px;}
#msgs::-webkit-scrollbar{width:5px;}
#msgs::-webkit-scrollbar-thumb{background:var(--border);border-radius:4px;}
.mrow{max-width:760px;margin:0 auto;padding:6px 28px;}
.mrow.u{display:flex;justify-content:flex-end;}
.bu{
  background:var(--ubg);border-radius:20px 20px 4px 20px;
  padding:12px 18px;max-width:72%;font-size:14px;line-height:1.65;word-break:break-word;
}
.bu img{max-width:100%;max-height:240px;border-radius:10px;display:block;margin-bottom:8px;object-fit:contain;}
.hist-file-chip{font-size:12px;color:var(--dim);background:var(--hover);border-radius:8px;padding:4px 10px;margin-bottom:6px;display:inline-block;}
.mrow.b{display:flex;flex-direction:column;gap:6px;}
.bb{font-size:14px;line-height:1.75;word-break:break-word;}
.bb p{margin:0 0 10px;}.bb p:last-child{margin:0;}
.bb strong{font-weight:600;}
.bb h1{font-size:18px;font-weight:600;margin:14px 0 6px;}
.bb h2{font-size:16px;font-weight:600;margin:12px 0 5px;}
.bb h3{font-size:14px;font-weight:600;margin:10px 0 4px;}
.bb ul,.bb ol{margin:4px 0 10px 20px;}.bb li{margin-bottom:3px;}
.bb code{background:var(--hover);padding:2px 6px;border-radius:4px;font-size:12px;font-family:monospace;}
.bb pre{background:var(--hover);padding:12px 16px;border-radius:10px;overflow-x:auto;margin:8px 0;}
.bb pre code{background:none;padding:0;}
.mrow.er .bb{color:var(--err);}
.bacts{display:flex;gap:2px;opacity:0;transition:opacity .2s;}
.mrow.b:hover .bacts{opacity:1;}
.abt{
  background:none;border:none;color:var(--dim);cursor:pointer;
  padding:6px;border-radius:50%;display:flex;align-items:center;justify-content:center;
  transition:background .12s,color .12s;
}
.abt:hover{background:var(--hover);color:var(--text);}
.tdots{display:flex;gap:5px;align-items:center;padding:6px 0;}
.tdots span{
  width:8px;height:8px;border-radius:50%;background:var(--dim);
  animation:td 1.2s infinite ease-in-out;
}
.tdots span:nth-child(2){animation-delay:.2s;}
.tdots span:nth-child(3){animation-delay:.4s;}
@keyframes td{0%,80%,100%{transform:scale(.75);opacity:.35;}40%{transform:scale(1.1);opacity:1;}}

/* welcome */
#welcome{
  height:100%;display:flex;flex-direction:column;
  align-items:center;justify-content:center;
  gap:14px;padding:40px;color:var(--dim);text-align:center;
}
#welcome svg.wlogo{width:56px;height:56px;}
#welcome h2{
  font-size:30px;font-weight:400;
  background:linear-gradient(90deg,#4285f4,#9c27b0);
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;
}
#welcome p{font-size:14px;max-width:380px;line-height:1.7;}

/* input */
#inp-area{
  flex-shrink:0;padding:4px 28px 22px;
  max-width:800px;width:100%;margin:0 auto;align-self:center;
}
/* chip shown above pill for non-image files only */
#fchip{
  display:none;align-items:center;gap:8px;
  background:var(--hover);border:1px solid var(--border);
  border-radius:12px;padding:8px 12px;margin-bottom:8px;
  font-size:12px;color:var(--dim);
}
#fchip.on{display:flex;}
#cname{color:var(--text);font-weight:500;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
#cmeta{color:var(--dim);}
#fcclose{background:none;border:none;color:var(--dim);cursor:pointer;font-size:15px;padding:3px 7px;border-radius:50%;transition:color .15s;}
#fcclose:hover{color:var(--err);}
/* image preview inside pill */
#img-preview{
  display:none;padding:10px 14px 4px;
}
#img-preview.on{display:block;}
#img-preview-wrap{
  position:relative;display:inline-block;
}
#cthumb{
  max-height:96px;max-width:130px;border-radius:10px;
  object-fit:cover;display:block;
}
#cclose{
  position:absolute;top:-7px;right:-7px;
  width:22px;height:22px;border-radius:50%;
  background:var(--dim);border:2px solid var(--sb);
  color:#fff;font-size:11px;line-height:1;
  cursor:pointer;display:flex;align-items:center;justify-content:center;
  transition:background .15s;
}
#cclose:hover{background:#c00;}
#finput{display:none;}
#pill{
  display:flex;flex-direction:column;
  background:var(--sb);border:1px solid var(--border);
  border-radius:20px;
  transition:border-color .2s;
}
#pill:focus-within{border-color:#6c6f72;}
#pill-row{
  display:flex;align-items:flex-end;
  padding:6px 12px 8px 6px;gap:4px;
}
#att{
  flex-shrink:0;width:40px;height:40px;background:none;border:none;color:var(--dim);
  cursor:pointer;border-radius:50%;display:flex;align-items:center;justify-content:center;
  transition:background .15s,color .15s;
}
#att:hover{background:var(--hover);color:var(--text);}
#q{
  flex:1;background:none;border:none;color:var(--text);
  font:15px/1.5 inherit;resize:none;outline:none;
  min-height:24px;max-height:180px;padding:7px 4px;overflow-y:auto;align-self:center;
}
#q::placeholder{color:var(--dim);}
#pr{display:flex;align-items:center;gap:4px;flex-shrink:0;}
.mpw{position:relative;display:inline-flex;align-items:center;}
#model{
  appearance:none;background:none;color:var(--dim);
  border:1px solid var(--border);border-radius:20px;
  padding:6px 26px 6px 12px;font-size:12px;cursor:pointer;
  font-family:inherit;transition:border-color .15s,color .15s;
  max-width:130px;
}
#model:hover{border-color:#6c6f72;color:var(--text);}
#model:focus{outline:none;}
#model option{background:#2d2e30;}
.mc{position:absolute;right:9px;pointer-events:none;color:var(--dim);font-size:10px;}
.pib{
  width:36px;height:36px;background:none;border:none;color:var(--dim);
  cursor:pointer;border-radius:50%;display:flex;align-items:center;justify-content:center;
  transition:background .15s,color .15s;
}
.pib:hover{background:var(--hover);color:var(--text);}
#sbtn{
  width:38px;height:38px;border-radius:50%;flex-shrink:0;
  background:var(--acc);border:none;cursor:pointer;
  display:flex;align-items:center;justify-content:center;transition:opacity .15s;
}
#sbtn:disabled{opacity:.3;cursor:default;background:#444;}
#sbtn:not(:disabled):hover{opacity:.85;}
#cap{text-align:center;font-size:11px;color:var(--dim);margin-top:10px;}
</style>
</head>
<body>

<!-- loading -->
<div id="loading"><div class="spin"></div></div>

<!-- login screen -->
<div id="login-screen">
  <div class="login-card">
    <svg class="login-logo" width="56" height="56" viewBox="0 0 24 24">
      <defs>
        <linearGradient id="llg" x1="0" y1="0" x2="24" y2="24" gradientUnits="userSpaceOnUse">
          <stop stop-color="#4285f4"/><stop offset="1" stop-color="#d170ff"/>
        </linearGradient>
      </defs>
      <path fill="url(#llg)" fill-rule="evenodd" d="M7.905 1.09c.216.085.411.225.588.41.295.306.544.744.734 1.263.191.522.315 1.1.362 1.68a5.054 5.054 0 012.049-.636l.051-.004c.87-.07 1.73.087 2.48.474.101.053.2.11.297.17.05-.569.172-1.134.36-1.644.19-.52.439-.957.733-1.264a1.67 1.67 0 01.589-.41c.257-.1.53-.118.796-.042.401.114.745.368 1.016.737.248.337.434.769.561 1.287.23.934.27 2.163.115 3.645l.053.04.026.019c.757.576 1.284 1.397 1.563 2.35.435 1.487.216 3.155-.534 4.088l-.018.021.002.003c.417.762.67 1.567.724 2.4l.002.03c.064 1.065-.2 2.137-.814 3.19l-.007.01.01.024c.472 1.157.62 2.322.438 3.486l-.006.039a.651.651 0 01-.747.536.648.648 0 01-.54-.742c.167-1.033.01-2.069-.48-3.123a.643.643 0 01.04-.617l.004-.006c.604-.924.854-1.83.8-2.72-.046-.779-.325-1.544-.8-2.273a.644.644 0 01.18-.886l.009-.006c.243-.159.467-.565.58-1.12a4.229 4.229 0 00-.095-1.974c-.205-.7-.58-1.284-1.105-1.683-.595-.454-1.383-.673-2.38-.61a.653.653 0 01-.632-.371c-.314-.665-.772-1.141-1.343-1.436a3.288 3.288 0 00-1.772-.332c-1.245.099-2.343.801-2.67 1.686a.652.652 0 01-.61.425c-1.067.002-1.893.252-2.497.703-.522.39-.878.935-1.066 1.588a4.07 4.07 0 00-.068 1.886c.112.558.331 1.02.582 1.269l.008.007c.212.207.257.53.109.785-.36.622-.629 1.549-.673 2.44-.05 1.018.186 1.902.719 2.536l.016.019a.643.643 0 01.095.69c-.576 1.236-.753 2.252-.562 3.052a.652.652 0 01-1.269.298c-.243-1.018-.078-2.184.473-3.498l.014-.035-.008-.012a4.339 4.339 0 01-.598-1.309l-.005-.019a5.764 5.764 0 01-.177-1.785c.044-.91.278-1.842.622-2.59l.012-.026-.002-.002c-.293-.418-.51-.953-.63-1.545l-.005-.024a5.352 5.352 0 01.093-2.49c.262-.915.777-1.701 1.536-2.269.06-.045.123-.09.186-.132-.159-1.493-.119-2.73.112-3.67.127-.518.314-.95.562-1.287.27-.368.614-.622 1.015-.737.266-.076.54-.059.797.042zm4.116 9.09c.936 0 1.8.313 2.446.855.63.527 1.005 1.235 1.005 1.94 0 .888-.406 1.58-1.133 2.022-.62.375-1.451.557-2.403.557-1.009 0-1.871-.259-2.493-.734-.617-.47-.963-1.13-.963-1.845 0-.707.398-1.417 1.056-1.946.668-.537 1.55-.849 2.485-.849zm0 .896a3.07 3.07 0 00-1.916.65c-.461.37-.722.835-.722 1.25 0 .428.21.829.61 1.134.455.347 1.124.548 1.943.548.799 0 1.473-.147 1.932-.426.463-.28.7-.686.7-1.257 0-.423-.246-.89-.683-1.256-.484-.405-1.14-.643-1.864-.643zm.662 1.21l.004.004c.12.151.095.37-.056.49l-.292.23v.446a.375.375 0 01-.376.373.375.375 0 01-.376-.373v-.46l-.271-.218a.347.347 0 01-.052-.49.353.353 0 01.494-.051l.215.172.22-.174a.353.353 0 01.49.051zm-5.04-1.919c.478 0 .867.39.867.871a.87.87 0 01-.868.871.87.87 0 01-.867-.87.87.87 0 01.867-.872zm8.706 0c.48 0 .868.39.868.871a.87.87 0 01-.868.871.87.87 0 01-.867-.87.87.87 0 01.867-.872zM7.44 2.3l-.003.002a.659.659 0 00-.285.238l-.005.006c-.138.189-.258.467-.348.832-.17.692-.216 1.631-.124 2.782.43-.128.899-.208 1.404-.237l.01-.001.019-.034c.046-.082.095-.161.148-.239.123-.771.022-1.692-.253-2.444-.134-.364-.297-.65-.453-.813a.628.628 0 00-.107-.09L7.44 2.3zm9.174.04l-.002.001a.628.628 0 00-.107.09c-.156.163-.32.45-.453.814-.29.794-.387 1.776-.23 2.572l.058.097.008.014h.03a5.184 5.184 0 011.466.212c.086-1.124.038-2.043-.128-2.722-.09-.365-.21-.643-.349-.832l-.004-.006a.659.659 0 00-.285-.239h-.004z"/>
    </svg>
    <h1 class="login-h1">Ollama Chat</h1>
    <p class="login-sub">Sign in to continue</p>

    <button class="login-btn btn-google" onclick="startLogin('google')">
      <!-- Google G logo SVG -->
      <svg width="18" height="18" viewBox="0 0 24 24">
        <path fill="#4285F4" d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z"/>
        <path fill="#34A853" d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z"/>
        <path fill="#FBBC05" d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z"/>
        <path fill="#EA4335" d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z"/>
      </svg>
      Continue with Google
    </button>

    <button class="login-btn btn-kc" onclick="startLogin(null)">
      <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <rect x="3" y="11" width="18" height="11" rx="2" ry="2"/>
        <path d="M7 11V7a5 5 0 0 1 10 0v4"/>
      </svg>
      Sign in with email
    </button>

    <div class="login-divider"><span>or</span></div>

    <button class="login-btn btn-register" onclick="startRegister()">
      <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/>
        <circle cx="12" cy="7" r="4"/>
        <line x1="19" y1="8" x2="19" y2="14"/><line x1="22" y1="11" x2="16" y2="11"/>
      </svg>
      Create account
    </button>

    <p class="login-note">Local AI · your data stays on your server</p>
  </div>
</div>

<!-- main app -->
<div id="app">
<aside id="sb">
  <div id="sb-head">
    <button class="hbtn" onclick="toggleSb()" title="Menu">
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
        <line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="18" x2="21" y2="18"/>
      </svg>
    </button>
    <div class="brand">
      <svg width="22" height="22" viewBox="0 0 24 24">
        <defs>
          <linearGradient id="lg" x1="0" y1="0" x2="24" y2="24" gradientUnits="userSpaceOnUse">
            <stop stop-color="#4285f4"/><stop offset="1" stop-color="#d170ff"/>
          </linearGradient>
        </defs>
        <path fill="url(#lg)" fill-rule="evenodd" d="M7.905 1.09c.216.085.411.225.588.41.295.306.544.744.734 1.263.191.522.315 1.1.362 1.68a5.054 5.054 0 012.049-.636l.051-.004c.87-.07 1.73.087 2.48.474.101.053.2.11.297.17.05-.569.172-1.134.36-1.644.19-.52.439-.957.733-1.264a1.67 1.67 0 01.589-.41c.257-.1.53-.118.796-.042.401.114.745.368 1.016.737.248.337.434.769.561 1.287.23.934.27 2.163.115 3.645l.053.04.026.019c.757.576 1.284 1.397 1.563 2.35.435 1.487.216 3.155-.534 4.088l-.018.021.002.003c.417.762.67 1.567.724 2.4l.002.03c.064 1.065-.2 2.137-.814 3.19l-.007.01.01.024c.472 1.157.62 2.322.438 3.486l-.006.039a.651.651 0 01-.747.536.648.648 0 01-.54-.742c.167-1.033.01-2.069-.48-3.123a.643.643 0 01.04-.617l.004-.006c.604-.924.854-1.83.8-2.72-.046-.779-.325-1.544-.8-2.273a.644.644 0 01.18-.886l.009-.006c.243-.159.467-.565.58-1.12a4.229 4.229 0 00-.095-1.974c-.205-.7-.58-1.284-1.105-1.683-.595-.454-1.383-.673-2.38-.61a.653.653 0 01-.632-.371c-.314-.665-.772-1.141-1.343-1.436a3.288 3.288 0 00-1.772-.332c-1.245.099-2.343.801-2.67 1.686a.652.652 0 01-.61.425c-1.067.002-1.893.252-2.497.703-.522.39-.878.935-1.066 1.588a4.07 4.07 0 00-.068 1.886c.112.558.331 1.02.582 1.269l.008.007c.212.207.257.53.109.785-.36.622-.629 1.549-.673 2.44-.05 1.018.186 1.902.719 2.536l.016.019a.643.643 0 01.095.69c-.576 1.236-.753 2.252-.562 3.052a.652.652 0 01-1.269.298c-.243-1.018-.078-2.184.473-3.498l.014-.035-.008-.012a4.339 4.339 0 01-.598-1.309l-.005-.019a5.764 5.764 0 01-.177-1.785c.044-.91.278-1.842.622-2.59l.012-.026-.002-.002c-.293-.418-.51-.953-.63-1.545l-.005-.024a5.352 5.352 0 01.093-2.49c.262-.915.777-1.701 1.536-2.269.06-.045.123-.09.186-.132-.159-1.493-.119-2.73.112-3.67.127-.518.314-.95.562-1.287.27-.368.614-.622 1.015-.737.266-.076.54-.059.797.042zm4.116 9.09c.936 0 1.8.313 2.446.855.63.527 1.005 1.235 1.005 1.94 0 .888-.406 1.58-1.133 2.022-.62.375-1.451.557-2.403.557-1.009 0-1.871-.259-2.493-.734-.617-.47-.963-1.13-.963-1.845 0-.707.398-1.417 1.056-1.946.668-.537 1.55-.849 2.485-.849zm0 .896a3.07 3.07 0 00-1.916.65c-.461.37-.722.835-.722 1.25 0 .428.21.829.61 1.134.455.347 1.124.548 1.943.548.799 0 1.473-.147 1.932-.426.463-.28.7-.686.7-1.257 0-.423-.246-.89-.683-1.256-.484-.405-1.14-.643-1.864-.643zm.662 1.21l.004.004c.12.151.095.37-.056.49l-.292.23v.446a.375.375 0 01-.376.373.375.375 0 01-.376-.373v-.46l-.271-.218a.347.347 0 01-.052-.49.353.353 0 01.494-.051l.215.172.22-.174a.353.353 0 01.49.051zm-5.04-1.919c.478 0 .867.39.867.871a.87.87 0 01-.868.871.87.87 0 01-.867-.87.87.87 0 01.867-.872zm8.706 0c.48 0 .868.39.868.871a.87.87 0 01-.868.871.87.87 0 01-.867-.87.87.87 0 01.867-.872zM7.44 2.3l-.003.002a.659.659 0 00-.285.238l-.005.006c-.138.189-.258.467-.348.832-.17.692-.216 1.631-.124 2.782.43-.128.899-.208 1.404-.237l.01-.001.019-.034c.046-.082.095-.161.148-.239.123-.771.022-1.692-.253-2.444-.134-.364-.297-.65-.453-.813a.628.628 0 00-.107-.09L7.44 2.3zm9.174.04l-.002.001a.628.628 0 00-.107.09c-.156.163-.32.45-.453.814-.29.794-.387 1.776-.23 2.572l.058.097.008.014h.03a5.184 5.184 0 011.466.212c.086-1.124.038-2.043-.128-2.722-.09-.365-.21-.643-.349-.832l-.004-.006a.659.659 0 00-.285-.239h-.004z"/>
      </svg>
      <span class="brand-txt">Ollama</span>
    </div>
  </div>

  <div class="sb-nav">
    <button class="ni" onclick="newChat()" title="New chat">
      <span class="ni-ico">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/>
        </svg>
      </span>
      <span class="ni-lbl">New chat</span>
    </button>
    <button class="ni" title="Search chats">
      <span class="ni-ico">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>
        </svg>
      </span>
      <span class="ni-lbl">Search chats</span>
    </button>
    <button class="ni" title="Images">
      <span class="ni-ico">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/>
          <polyline points="21 15 16 10 5 21"/>
        </svg>
      </span>
      <span class="ni-lbl">Images</span>
    </button>
    <button class="ni" title="Library">
      <span class="ni-ico">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/>
          <path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>
        </svg>
      </span>
      <span class="ni-lbl">Library</span>
    </button>
  </div>

  <div class="sb-sec">
    <div class="sb-hdr">Notebooks</div>
    <button class="ni" title="New notebook">
      <span class="ni-ico">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <circle cx="12" cy="12" r="10"/>
          <line x1="12" y1="8" x2="12" y2="16"/><line x1="8" y1="12" x2="16" y2="12"/>
        </svg>
      </span>
      <span class="ni-lbl">New notebook</span>
    </button>
  </div>

  <div class="sb-sec"><div class="sb-hdr">Recents</div></div>
  <div id="clist"></div>

  <div id="sb-foot">
    <div class="prow">
      <div id="pav">U</div>
      <span id="pname">User</span>
      <button id="logout-btn" onclick="doLogout()" title="Sign out">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/>
          <polyline points="16 17 21 12 16 7"/>
          <line x1="21" y1="12" x2="9" y2="12"/>
        </svg>
      </button>
    </div>
  </div>
</aside>

<div id="main">
  <div id="top-acts">
    <button class="ibtn" id="delbtn" onclick="delCurrent()" title="Delete" style="display:none">
      <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <polyline points="3 6 5 6 21 6"/>
        <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/>
        <path d="M10 11v6M14 11v6"/>
      </svg>
    </button>
    <button class="ibtn" title="More options">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor">
        <circle cx="12" cy="5" r="1.5"/><circle cx="12" cy="12" r="1.5"/><circle cx="12" cy="19" r="1.5"/>
      </svg>
    </button>
  </div>

  <div id="msgs">
    <div id="welcome">
      <svg class="wlogo" viewBox="0 0 24 24">
        <defs>
          <linearGradient id="wlg" x1="0" y1="0" x2="24" y2="24" gradientUnits="userSpaceOnUse">
            <stop stop-color="#4285f4"/><stop offset="1" stop-color="#d170ff"/>
          </linearGradient>
        </defs>
        <path fill="url(#wlg)" fill-rule="evenodd" d="M7.905 1.09c.216.085.411.225.588.41.295.306.544.744.734 1.263.191.522.315 1.1.362 1.68a5.054 5.054 0 012.049-.636l.051-.004c.87-.07 1.73.087 2.48.474.101.053.2.11.297.17.05-.569.172-1.134.36-1.644.19-.52.439-.957.733-1.264a1.67 1.67 0 01.589-.41c.257-.1.53-.118.796-.042.401.114.745.368 1.016.737.248.337.434.769.561 1.287.23.934.27 2.163.115 3.645l.053.04.026.019c.757.576 1.284 1.397 1.563 2.35.435 1.487.216 3.155-.534 4.088l-.018.021.002.003c.417.762.67 1.567.724 2.4l.002.03c.064 1.065-.2 2.137-.814 3.19l-.007.01.01.024c.472 1.157.62 2.322.438 3.486l-.006.039a.651.651 0 01-.747.536.648.648 0 01-.54-.742c.167-1.033.01-2.069-.48-3.123a.643.643 0 01.04-.617l.004-.006c.604-.924.854-1.83.8-2.72-.046-.779-.325-1.544-.8-2.273a.644.644 0 01.18-.886l.009-.006c.243-.159.467-.565.58-1.12a4.229 4.229 0 00-.095-1.974c-.205-.7-.58-1.284-1.105-1.683-.595-.454-1.383-.673-2.38-.61a.653.653 0 01-.632-.371c-.314-.665-.772-1.141-1.343-1.436a3.288 3.288 0 00-1.772-.332c-1.245.099-2.343.801-2.67 1.686a.652.652 0 01-.61.425c-1.067.002-1.893.252-2.497.703-.522.39-.878.935-1.066 1.588a4.07 4.07 0 00-.068 1.886c.112.558.331 1.02.582 1.269l.008.007c.212.207.257.53.109.785-.36.622-.629 1.549-.673 2.44-.05 1.018.186 1.902.719 2.536l.016.019a.643.643 0 01.095.69c-.576 1.236-.753 2.252-.562 3.052a.652.652 0 01-1.269.298c-.243-1.018-.078-2.184.473-3.498l.014-.035-.008-.012a4.339 4.339 0 01-.598-1.309l-.005-.019a5.764 5.764 0 01-.177-1.785c.044-.91.278-1.842.622-2.59l.012-.026-.002-.002c-.293-.418-.51-.953-.63-1.545l-.005-.024a5.352 5.352 0 01.093-2.49c.262-.915.777-1.701 1.536-2.269.06-.045.123-.09.186-.132-.159-1.493-.119-2.73.112-3.67.127-.518.314-.95.562-1.287.27-.368.614-.622 1.015-.737.266-.076.54-.059.797.042zm4.116 9.09c.936 0 1.8.313 2.446.855.63.527 1.005 1.235 1.005 1.94 0 .888-.406 1.58-1.133 2.022-.62.375-1.451.557-2.403.557-1.009 0-1.871-.259-2.493-.734-.617-.47-.963-1.13-.963-1.845 0-.707.398-1.417 1.056-1.946.668-.537 1.55-.849 2.485-.849zm0 .896a3.07 3.07 0 00-1.916.65c-.461.37-.722.835-.722 1.25 0 .428.21.829.61 1.134.455.347 1.124.548 1.943.548.799 0 1.473-.147 1.932-.426.463-.28.7-.686.7-1.257 0-.423-.246-.89-.683-1.256-.484-.405-1.14-.643-1.864-.643zm.662 1.21l.004.004c.12.151.095.37-.056.49l-.292.23v.446a.375.375 0 01-.376.373.375.375 0 01-.376-.373v-.46l-.271-.218a.347.347 0 01-.052-.49.353.353 0 01.494-.051l.215.172.22-.174a.353.353 0 01.49.051zm-5.04-1.919c.478 0 .867.39.867.871a.87.87 0 01-.868.871.87.87 0 01-.867-.87.87.87 0 01.867-.872zm8.706 0c.48 0 .868.39.868.871a.87.87 0 01-.868.871.87.87 0 01-.867-.87.87.87 0 01.867-.872zM7.44 2.3l-.003.002a.659.659 0 00-.285.238l-.005.006c-.138.189-.258.467-.348.832-.17.692-.216 1.631-.124 2.782.43-.128.899-.208 1.404-.237l.01-.001.019-.034c.046-.082.095-.161.148-.239.123-.771.022-1.692-.253-2.444-.134-.364-.297-.65-.453-.813a.628.628 0 00-.107-.09L7.44 2.3zm9.174.04l-.002.001a.628.628 0 00-.107.09c-.156.163-.32.45-.453.814-.29.794-.387 1.776-.23 2.572l.058.097.008.014h.03a5.184 5.184 0 011.466.212c.086-1.124.038-2.043-.128-2.722-.09-.365-.21-.643-.349-.832l-.004-.006a.659.659 0 00-.285-.239h-.004z"/>
      </svg>
      <h2>Hello</h2>
      <p>Attach a payment screenshot to extract UTR numbers and transaction details, or ask me anything.</p>
    </div>
  </div>

  <div id="inp-area">
    <!-- chip shown above pill for non-image files only -->
    <div id="fchip">
      <span id="cname"></span>
      <span id="cmeta"></span>
      <button id="fcclose" onclick="clearFile()">✕</button>
    </div>
    <input type="file" id="finput" onchange="handleFile(this.files[0])">
    <div id="pill">
      <!-- image preview lives inside pill at the top -->
      <div id="img-preview">
        <div id="img-preview-wrap">
          <img id="cthumb" alt="">
          <button id="cclose" onclick="clearFile()">✕</button>
        </div>
      </div>
      <div id="pill-row">
        <button id="att" onclick="document.getElementById('finput').click()" title="Attach file">
          <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>
          </svg>
        </button>
        <textarea id="q" rows="1" placeholder="Ask Ollama…" onkeydown="onKey(event)" oninput="onInput(this)"></textarea>
        <div id="pr">
          <div class="mpw">
            <select id="model"><option>Loading…</option></select>
            <span class="mc">▾</span>
          </div>
          <button class="pib" title="Voice input">
            <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/>
              <path d="M19 10v2a7 7 0 0 1-14 0v-2"/>
              <line x1="12" y1="19" x2="12" y2="23"/><line x1="8" y1="23" x2="16" y2="23"/>
            </svg>
          </button>
          <button id="sbtn" onclick="send()" title="Send" disabled>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#000" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
              <line x1="12" y1="19" x2="12" y2="5"/><polyline points="5 12 12 5 19 12"/>
            </svg>
          </button>
        </div>
      </div>
    </div>
    <div id="cap">Runs locally on Ollama · responses may be inaccurate</div>
  </div>
</div>
</div>

<script>
/* ── State ── */
let hist=[], fctx=null, busy=false, convId=null, sbOpen=true, imgShown=false;
let AUTH=null; // null = no auth; object = KC config

/* ── PKCE helpers ── */
function b64url(buf){
  return btoa(String.fromCharCode(...new Uint8Array(buf)))
    .replace(/\+/g,'-').replace(/\//g,'_').replace(/=/g,'');
}
async function generatePKCE(){
  const v=b64url(crypto.getRandomValues(new Uint8Array(32)));
  const c=b64url(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(v)));
  return{verifier:v,challenge:c};
}

/* ── Token storage ── */
function getToken(){return sessionStorage.getItem('at');}
function getUser(){const u=sessionStorage.getItem('user');return u?JSON.parse(u):null;}
function tokenExp(){return parseInt(sessionStorage.getItem('at_exp')||'0');}

async function exchangeCode(code){
  const v=sessionStorage.getItem('pkce_v');
  if(!v)return false;
  const res=await fetch(`${AUTH.kcUrl}/realms/${AUTH.realm}/protocol/openid-connect/token`,{
    method:'POST',
    headers:{'Content-Type':'application/x-www-form-urlencoded'},
    body:new URLSearchParams({
      grant_type:'authorization_code',
      client_id:AUTH.clientId,
      code,
      redirect_uri:location.origin+location.pathname,
      code_verifier:v
    })
  });
  if(!res.ok)return false;
  const t=await res.json();
  if(!t.access_token)return false;
  sessionStorage.setItem('at',t.access_token);
  sessionStorage.setItem('rt',t.refresh_token||'');
  sessionStorage.setItem('at_exp',Date.now()+t.expires_in*1000);
  if(t.id_token){
    try{
      const p=JSON.parse(atob(t.id_token.split('.')[1].replace(/-/g,'+').replace(/_/g,'/')));
      sessionStorage.setItem('user',JSON.stringify({
        sub:p.sub,name:p.name||p.preferred_username,
        email:p.email||'',picture:p.picture||''
      }));
    }catch{}
  }
  sessionStorage.removeItem('pkce_v');
  history.replaceState({},document.title,location.pathname);
  return true;
}

async function refreshToken(){
  const rt=sessionStorage.getItem('rt');
  if(!rt||!AUTH)return false;
  const res=await fetch(`${AUTH.kcUrl}/realms/${AUTH.realm}/protocol/openid-connect/token`,{
    method:'POST',
    headers:{'Content-Type':'application/x-www-form-urlencoded'},
    body:new URLSearchParams({
      grant_type:'refresh_token',
      client_id:AUTH.clientId,
      refresh_token:rt
    })
  });
  if(!res.ok)return false;
  const t=await res.json();
  sessionStorage.setItem('at',t.access_token);
  sessionStorage.setItem('at_exp',Date.now()+t.expires_in*1000);
  return true;
}

function scheduleRefresh(){
  const delay=Math.max(tokenExp()-Date.now()-60000,10000);
  setTimeout(async()=>{
    if(await refreshToken())scheduleRefresh();
    else{sessionStorage.clear();location.reload();}
  },delay);
}

function authHeaders(){
  const t=getToken();
  return t?{Authorization:`Bearer ${t}`}:{};
}

/* ── Login / Logout ── */
async function startLogin(hint){
  const{verifier,challenge}=await generatePKCE();
  sessionStorage.setItem('pkce_v',verifier);
  const p=new URLSearchParams({
    client_id:AUTH.clientId,
    redirect_uri:location.origin+location.pathname,
    response_type:'code',
    scope:'openid profile email',
    code_challenge:challenge,
    code_challenge_method:'S256',
  });
  if(hint)p.set('kc_idp_hint',hint);
  location.href=`${AUTH.kcUrl}/realms/${AUTH.realm}/protocol/openid-connect/auth?${p}`;
}

async function startRegister(){
  const{verifier,challenge}=await generatePKCE();
  sessionStorage.setItem('pkce_v',verifier);
  const p=new URLSearchParams({
    client_id:AUTH.clientId,
    redirect_uri:location.origin+location.pathname,
    response_type:'code',
    scope:'openid profile email',
    code_challenge:challenge,
    code_challenge_method:'S256',
  });
  location.href=`${AUTH.kcUrl}/realms/${AUTH.realm}/protocol/openid-connect/registrations?${p}`;
}

async function doLogout(){
  const t=getToken();
  const rt=sessionStorage.getItem('rt');
  sessionStorage.clear();
  if(AUTH&&t){
    const p=new URLSearchParams({
      client_id:AUTH.clientId,
      post_logout_redirect_uri:location.origin+location.pathname
    });
    if(rt)p.set('refresh_token',rt);
    location.href=`${AUTH.kcUrl}/realms/${AUTH.realm}/protocol/openid-connect/logout?${p}`;
  }else{
    location.reload();
  }
}

/* ── Init ── */
async function init(){
  document.getElementById('loading').style.display='flex';

  // Fetch auth config from backend
  const cfg=await fetch('/auth/config').then(r=>r.json()).catch(()=>({enabled:false}));

  if(!cfg.enabled){
    AUTH=null;
    showApp();
    return;
  }

  AUTH=cfg;

  // Handle OAuth callback (?code=...)
  const code=new URLSearchParams(location.search).get('code');
  if(code){
    const ok=await exchangeCode(code);
    if(!ok){showLogin();return;}
    scheduleRefresh();
    showApp();
    return;
  }

  // Check existing token
  const token=getToken();
  if(!token){showLogin();return;}

  // Refresh if about to expire
  if(tokenExp()<Date.now()+60000){
    const ok=await refreshToken();
    if(!ok){showLogin();return;}
  }

  scheduleRefresh();
  showApp();
}

function showLogin(){
  document.getElementById('loading').style.display='none';
  document.getElementById('login-screen').style.display='flex';
  document.getElementById('app').style.display='none';
}

function showApp(){
  document.getElementById('loading').style.display='none';
  document.getElementById('login-screen').style.display='none';
  document.getElementById('app').style.display='flex';

  // Update user info in sidebar
  const u=getUser();
  if(u){
    document.getElementById('pname').textContent=u.name||u.email||'User';
    const av=document.getElementById('pav');
    if(u.picture){
      av.innerHTML=`<img src="${u.picture}" alt="">`;
    }else{
      av.textContent=(u.name||'U')[0].toUpperCase();
    }
  }

  // Only show logout button when auth is enabled
  document.getElementById('logout-btn').style.display=AUTH?'flex':'none';

  loadModels();
  refreshSb();
}

/* ── Models ── */
async function loadModels(){
  try{
    const{models}=await fetch('/models',{headers:authHeaders()}).then(r=>r.json());
    const s=document.getElementById('model');
    s.innerHTML=models.map(m=>`<option value="${m}">${m}</option>`).join('');
  }catch{}
}

/* ── Sidebar helpers ── */
function toggleSb(){sbOpen=!sbOpen;document.getElementById('sb').classList.toggle('col',!sbOpen);}

function relTime(iso){
  const d=Date.now()-new Date(iso).getTime(),m=Math.floor(d/60000);
  if(m<1)return'just now';if(m<60)return m+'m ago';
  const h=Math.floor(m/60);if(h<24)return h+'h ago';
  return Math.floor(h/24)+'d ago';
}
function groupByDate(convs){
  const g=[{l:'Today',i:[]},{l:'Yesterday',i:[]},{l:'Last 7 days',i:[]},{l:'Older',i:[]}];
  const t=new Date();t.setHours(0,0,0,0);
  const y=new Date(t);y.setDate(y.getDate()-1);
  const w=new Date(t);w.setDate(w.getDate()-7);
  for(const c of convs){
    const d=new Date(c.updated_at);
    if(d>=t)g[0].i.push(c);else if(d>=y)g[1].i.push(c);
    else if(d>=w)g[2].i.push(c);else g[3].i.push(c);
  }
  return g;
}
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}

async function refreshSb(){
  const{conversations}=await fetch('/conversations',{headers:authHeaders()}).then(r=>r.json()).catch(()=>({conversations:[]}));
  const el=document.getElementById('clist');
  if(!conversations.length){
    el.innerHTML='<div class="glbl" style="text-align:center;padding-top:20px">No conversations</div>';
    return;
  }
  el.innerHTML='';
  for(const g of groupByDate(conversations)){
    if(!g.i.length)continue;
    const lbl=document.createElement('div');lbl.className='glbl';lbl.textContent=g.l;
    el.appendChild(lbl);
    for(const c of g.i){
      const d=document.createElement('div');
      d.className='ci'+(c.id===convId?' on':'');
      d.dataset.id=c.id;d.title=c.title;
      d.innerHTML=`<div class="ci-info"><div class="ci-title">${esc(c.title)}</div></div>
        <button class="ci-del" onclick="delConv(event,'${c.id}')" title="Delete">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <polyline points="3 6 5 6 21 6"/>
            <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/>
          </svg>
        </button>`;
      d.addEventListener('click',()=>loadConv(c.id));
      el.appendChild(d);
    }
  }
}

/* ── Markdown renderer ── */
function md(t){
  let s=t.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  s=s.replace(/```([\s\S]*?)```/g,(_,c)=>`<pre><code>${c.trim()}</code></pre>`);
  s=s.replace(/`([^`\n]+)`/g,'<code>$1</code>');
  s=s.replace(/\*\*([^*\n]+)\*\*/g,'<strong>$1</strong>');
  s=s.replace(/\*([^*\n]+)\*/g,'<em>$1</em>');
  s=s.replace(/^### (.+)$/gm,'<h3>$1</h3>');
  s=s.replace(/^## (.+)$/gm,'<h2>$1</h2>');
  s=s.replace(/^# (.+)$/gm,'<h1>$1</h1>');
  s=s.replace(/^[-*] (.+)$/gm,'<li>$1</li>');
  s=s.replace(/(<li>[\s\S]*?<\/li>(\n|$))+/g,m=>`<ul>${m}</ul>`);
  s=s.split(/\n{2,}/).map(chunk=>/^<(h[1-3]|ul|pre)/.test(chunk.trim())?chunk:`<p>${chunk.replace(/\n/g,'<br>')}</p>`).join('');
  return s;
}

/* ── Action icons ── */
const I_LIKE=`<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 9V5a3 3 0 0 0-3-3l-4 9v11h11.28a2 2 0 0 0 2-1.7l1.38-9a2 2 0 0 0-2-2.3H14z"/><path d="M7 22H4a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2h3"/></svg>`;
const I_DISLIKE=`<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10 15v4a3 3 0 0 0 3 3l4-9V2H5.72a2 2 0 0 0-2 1.7l-1.38 9a2 2 0 0 0 2 2.3H10z"/><path d="M17 2h2.67A2.31 2.31 0 0 1 22 4v7a2.31 2.31 0 0 1-2.33 2H17"/></svg>`;
const I_COPY=`<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>`;
const I_REGEN=`<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 .49-3.51"/></svg>`;

/* ── DOM helpers ── */
function scrollDown(){const m=document.getElementById('msgs');m.scrollTop=m.scrollHeight;}

function showWelcome(){
  document.getElementById('msgs').innerHTML=`<div id="welcome">
    <svg class="wlogo" viewBox="0 0 24 24">
      <defs><linearGradient id="wlg2" x1="0" y1="0" x2="24" y2="24" gradientUnits="userSpaceOnUse">
        <stop stop-color="#4285f4"/><stop offset="1" stop-color="#d170ff"/>
      </linearGradient></defs>
      <path fill="url(#wlg2)" fill-rule="evenodd" d="M7.905 1.09c.216.085.411.225.588.41.295.306.544.744.734 1.263.191.522.315 1.1.362 1.68a5.054 5.054 0 012.049-.636l.051-.004c.87-.07 1.73.087 2.48.474.101.053.2.11.297.17.05-.569.172-1.134.36-1.644.19-.52.439-.957.733-1.264a1.67 1.67 0 01.589-.41c.257-.1.53-.118.796-.042.401.114.745.368 1.016.737.248.337.434.769.561 1.287.23.934.27 2.163.115 3.645l.053.04.026.019c.757.576 1.284 1.397 1.563 2.35.435 1.487.216 3.155-.534 4.088l-.018.021.002.003c.417.762.67 1.567.724 2.4l.002.03c.064 1.065-.2 2.137-.814 3.19l-.007.01.01.024c.472 1.157.62 2.322.438 3.486l-.006.039a.651.651 0 01-.747.536.648.648 0 01-.54-.742c.167-1.033.01-2.069-.48-3.123a.643.643 0 01.04-.617l.004-.006c.604-.924.854-1.83.8-2.72-.046-.779-.325-1.544-.8-2.273a.644.644 0 01.18-.886l.009-.006c.243-.159.467-.565.58-1.12a4.229 4.229 0 00-.095-1.974c-.205-.7-.58-1.284-1.105-1.683-.595-.454-1.383-.673-2.38-.61a.653.653 0 01-.632-.371c-.314-.665-.772-1.141-1.343-1.436a3.288 3.288 0 00-1.772-.332c-1.245.099-2.343.801-2.67 1.686a.652.652 0 01-.61.425c-1.067.002-1.893.252-2.497.703-.522.39-.878.935-1.066 1.588a4.07 4.07 0 00-.068 1.886c.112.558.331 1.02.582 1.269l.008.007c.212.207.257.53.109.785-.36.622-.629 1.549-.673 2.44-.05 1.018.186 1.902.719 2.536l.016.019a.643.643 0 01.095.69c-.576 1.236-.753 2.252-.562 3.052a.652.652 0 01-1.269.298c-.243-1.018-.078-2.184.473-3.498l.014-.035-.008-.012a4.339 4.339 0 01-.598-1.309l-.005-.019a5.764 5.764 0 01-.177-1.785c.044-.91.278-1.842.622-2.59l.012-.026-.002-.002c-.293-.418-.51-.953-.63-1.545l-.005-.024a5.352 5.352 0 01.093-2.49c.262-.915.777-1.701 1.536-2.269.06-.045.123-.09.186-.132-.159-1.493-.119-2.73.112-3.67.127-.518.314-.95.562-1.287.27-.368.614-.622 1.015-.737.266-.076.54-.059.797.042zm4.116 9.09c.936 0 1.8.313 2.446.855.63.527 1.005 1.235 1.005 1.94 0 .888-.406 1.58-1.133 2.022-.62.375-1.451.557-2.403.557-1.009 0-1.871-.259-2.493-.734-.617-.47-.963-1.13-.963-1.845 0-.707.398-1.417 1.056-1.946.668-.537 1.55-.849 2.485-.849zm0 .896a3.07 3.07 0 00-1.916.65c-.461.37-.722.835-.722 1.25 0 .428.21.829.61 1.134.455.347 1.124.548 1.943.548.799 0 1.473-.147 1.932-.426.463-.28.7-.686.7-1.257 0-.423-.246-.89-.683-1.256-.484-.405-1.14-.643-1.864-.643zm.662 1.21l.004.004c.12.151.095.37-.056.49l-.292.23v.446a.375.375 0 01-.376.373.375.375 0 01-.376-.373v-.46l-.271-.218a.347.347 0 01-.052-.49.353.353 0 01.494-.051l.215.172.22-.174a.353.353 0 01.49.051zm-5.04-1.919c.478 0 .867.39.867.871a.87.87 0 01-.868.871.87.87 0 01-.867-.87.87.87 0 01.867-.872zm8.706 0c.48 0 .868.39.868.871a.87.87 0 01-.868.871.87.87 0 01-.867-.87.87.87 0 01.867-.872zM7.44 2.3l-.003.002a.659.659 0 00-.285.238l-.005.006c-.138.189-.258.467-.348.832-.17.692-.216 1.631-.124 2.782.43-.128.899-.208 1.404-.237l.01-.001.019-.034c.046-.082.095-.161.148-.239.123-.771.022-1.692-.253-2.444-.134-.364-.297-.65-.453-.813a.628.628 0 00-.107-.09L7.44 2.3zm9.174.04l-.002.001a.628.628 0 00-.107.09c-.156.163-.32.45-.453.814-.29.794-.387 1.776-.23 2.572l.058.097.008.014h.03a5.184 5.184 0 011.466.212c.086-1.124.038-2.043-.128-2.722-.09-.365-.21-.643-.349-.832l-.004-.006a.659.659 0 00-.285-.239h-.004z"/>
    </svg>
    <h2>Hello${getUser()?.name?', '+getUser().name.split(' ')[0]:''}</h2>
    <p>Attach a payment screenshot to extract UTR numbers and transaction details, or ask me anything.</p>
  </div>`;
}

function addUserBubble(text,imgB64,fileName){
  document.getElementById('welcome')?.remove();
  const row=document.createElement('div');row.className='mrow u';
  const b=document.createElement('div');b.className='bu';
  if(imgB64){
    const img=document.createElement('img');
    img.src='data:image/jpeg;base64,'+imgB64;
    b.appendChild(img);
  } else if(fileName){
    const chip=document.createElement('div');chip.className='hist-file-chip';
    chip.textContent='📎 '+fileName;
    b.appendChild(chip);
  }
  const sp=document.createElement('span');sp.textContent=text;b.appendChild(sp);
  row.appendChild(b);document.getElementById('msgs').appendChild(row);
  scrollDown();return b;
}

function addBotBubble(text){
  document.getElementById('welcome')?.remove();
  const row=document.createElement('div');row.className='mrow b';
  const b=document.createElement('div');b.className='bb';
  if(text)b.innerHTML=md(text);
  const acts=document.createElement('div');acts.className='bacts';
  acts.innerHTML=`
    <button class="abt" title="Good response">${I_LIKE}</button>
    <button class="abt" title="Bad response">${I_DISLIKE}</button>
    <button class="abt" title="Copy" onclick="copyBubble(this)">${I_COPY}</button>
    <button class="abt" title="Regenerate">${I_REGEN}</button>`;
  row.appendChild(b);row.appendChild(acts);
  document.getElementById('msgs').appendChild(row);
  scrollDown();return b;
}

function addTyping(){
  document.getElementById('welcome')?.remove();
  const row=document.createElement('div');row.className='mrow b';row.id='tdrow';
  const d=document.createElement('div');d.className='tdots';
  d.innerHTML='<span></span><span></span><span></span>';
  row.appendChild(d);document.getElementById('msgs').appendChild(row);
  scrollDown();return row;
}

function copyBubble(btn){
  navigator.clipboard.writeText(btn.closest('.mrow.b').querySelector('.bb').innerText).catch(()=>{});
}

/* ── File handling ── */
document.body.addEventListener('dragover',e=>e.preventDefault());
document.body.addEventListener('drop',e=>{e.preventDefault();const f=e.dataTransfer.files[0];if(f)handleFile(f);});

async function handleFile(file){
  if(!file)return;
  const fd=new FormData();fd.append('file',file);
  try{
    const resp=await fetch('/upload',{method:'POST',headers:authHeaders(),body:fd});
    if(resp.status===401){
      sessionStorage.clear();
      if(AUTH)showLogin();
      return;
    }
    const data=await resp.json();
    if(data.type==='error'){alert('File error: '+data.error);return;}
    fctx={...data,name:file.name};imgShown=false;showChip(file,data);
  }catch(e){alert('Upload failed: '+e);}
}

function showChip(file,data){
  if(data.type==='image'){
    // Show thumbnail inside the pill
    const th=document.getElementById('cthumb');
    th.src='data:'+file.type+';base64,'+data.b64;
    document.getElementById('img-preview').classList.add('on');
    document.getElementById('fchip').classList.remove('on');
  }else{
    // Show chip above pill for non-image files
    document.getElementById('img-preview').classList.remove('on');
    document.getElementById('cthumb').src='';
    document.getElementById('cname').textContent=file.name;
    const x=[data.rows?data.rows+' rows':null,data.pages?data.pages+' pages':null].filter(Boolean).join(' · ');
    document.getElementById('cmeta').textContent=(file.size/1024).toFixed(1)+' KB'+(x?' · '+x:'');
    document.getElementById('fchip').classList.add('on');
  }
}

function clearFile(){
  fctx=null;imgShown=false;
  document.getElementById('img-preview').classList.remove('on');
  document.getElementById('fchip').classList.remove('on');
  document.getElementById('finput').value='';
  document.getElementById('cthumb').src='';
}

/* ── Input helpers ── */
function onKey(e){if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send();}}
function onInput(el){
  el.style.height='auto';el.style.height=Math.min(el.scrollHeight,180)+'px';
  document.getElementById('sbtn').disabled=el.value.trim()===''||busy;
}

/* ── Conversations ── */
function newChat(){
  convId=null;hist=[];clearFile();
  document.getElementById('delbtn').style.display='none';
  document.querySelectorAll('.ci').forEach(e=>e.classList.remove('on'));
  showWelcome();
  document.getElementById('q').focus();
}

async function loadConv(id){
  const data=await fetch(`/conversations/${id}`,{headers:authHeaders()}).then(r=>r.json());
  convId=id;hist=data.messages;clearFile();
  document.getElementById('delbtn').style.display='flex';
  document.querySelectorAll('.ci').forEach(e=>e.classList.toggle('on',e.dataset.id===id));
  document.getElementById('msgs').innerHTML='';
  for(const m of data.messages){
    if(m.role==='user')addUserBubble(m.content, m.image_b64||null, m.file_name||null);
    else addBotBubble(m.content);
  }
  scrollDown();document.getElementById('q').focus();
}

async function delConv(e,id){
  e.stopPropagation();
  await fetch(`/conversations/${id}`,{method:'DELETE',headers:authHeaders()});
  if(id===convId)newChat();
  refreshSb();
}

async function delCurrent(){
  if(!convId)return;
  if(!confirm('Delete this conversation?'))return;
  await fetch(`/conversations/${convId}`,{method:'DELETE',headers:authHeaders()});
  newChat();refreshSb();
}

async function saveConv(){
  if(!convId)return;
  const title=hist.find(m=>m.role==='user')?.content.slice(0,60)||'New chat';
  const fd=new FormData();fd.append('messages',JSON.stringify(hist));fd.append('title',title);
  await fetch(`/conversations/${convId}`,{method:'PUT',headers:authHeaders(),body:fd});
  refreshSb();
}

/* ── Send ── */
async function send(){
  if(busy)return;
  const inp=document.getElementById('q');
  const q=inp.value.trim();if(!q)return;

  if(!convId){
    const r=await fetch('/conversations',{method:'POST',headers:authHeaders()}).then(r=>r.json());
    convId=r.id;
    document.getElementById('delbtn').style.display='flex';
    refreshSb();
  }

  const hasImg=fctx?.type==='image'&&fctx.b64;
  addUserBubble(q, hasImg ? fctx.b64 : null);
  inp.value='';onInput(inp);

  // Capture image/file context before clearing
  const sendImgB64     = hasImg ? fctx.b64 : '';
  const sendOcrText    = hasImg ? fctx?.ocr_text||'' : '';
  const sendExtractedUtr = hasImg ? fctx?.extracted_utr||'' : '';
  const sendFileTxt    = fctx?.type==='text' ? fctx.content||'' : '';
  const sendFileName   = fctx?.name||'';

  // Clear preview immediately — image is already in the user bubble
  clearFile();

  const tdrow=addTyping();
  busy=true;document.getElementById('sbtn').disabled=true;

  const fd=new FormData();
  fd.append('question',q);
  fd.append('model',document.getElementById('model').value);
  fd.append('history',JSON.stringify(hist));
  if(sendImgB64){
    fd.append('image_b64',sendImgB64);
    if(sendOcrText)fd.append('ocr_text',sendOcrText);
    if(sendExtractedUtr)fd.append('extracted_utr',sendExtractedUtr);
  }
  else if(sendFileTxt){fd.append('file_context',sendFileTxt);fd.append('file_name',sendFileName);}

  let ans='',bb=null;
  try{
    const resp=await fetch('/chat',{method:'POST',headers:authHeaders(),body:fd});
    if(resp.status===401){
      tdrow.remove();sessionStorage.clear();
      if(AUTH)showLogin();
      return;
    }
    const reader=resp.body.getReader();
    const dec=new TextDecoder();let buf='';
    while(true){
      const{done,value}=await reader.read();if(done)break;
      buf+=dec.decode(value,{stream:true});
      const lines=buf.split('\n');buf=lines.pop();
      for(const line of lines){
        if(!line.startsWith('data: '))continue;
        const chunk=JSON.parse(line.slice(6));
        const tok=chunk.content||'';if(!tok)continue;
        if(!bb){tdrow.remove();bb=addBotBubble('');}
        ans+=tok;bb.innerHTML=md(ans);scrollDown();
      }
    }
  }catch(e){
    tdrow.remove();
    if(!bb)bb=addBotBubble('');
    bb.parentElement.classList.replace('b','er');
    bb.textContent='Error: '+e.message;
  }

  if(!bb){tdrow.remove();bb=addBotBubble('(no response)');}
  busy=false;
  document.getElementById('sbtn').disabled=inp.value.trim()==='';
  const userEntry={role:'user',content:q};
  if(sendImgB64)userEntry.image_b64=sendImgB64;
  if(sendFileName)userEntry.file_name=sendFileName;
  hist.push(userEntry);
  hist.push({role:'assistant',content:ans});
  saveConv();
  inp.focus();
}

/* ── Boot ── */
init();
</script>
</body>
</html>"""


# ── API endpoints ─────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    return _HTML


@app.get("/models")
async def list_models():
    if ANTHROPIC_API_KEY:
        return {"models": CLAUDE_MODELS, "provider": "claude"}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{OLLAMA_BASE_URL}/api/tags")
            r.raise_for_status()
            models = [m["name"] for m in r.json().get("models", [])]
            return {"models": models or [DEFAULT_MODEL], "provider": "ollama"}
    except Exception:
        return {"models": [DEFAULT_MODEL], "provider": "ollama"}


@app.post("/upload")
async def upload_file(file: UploadFile = File(...), user=Depends(_get_user)):
    content_type = file.content_type or ""
    filename     = file.filename or "file"
    data         = await file.read()

    if content_type.startswith("image/"):
        ocr_text = ""
        try:
            from PIL import Image, ImageOps, ImageEnhance
            img = Image.open(io.BytesIO(data))
            img = ImageOps.exif_transpose(img)
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            img.thumbnail((1920, 1920), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=92)
            b64 = base64.b64encode(buf.getvalue()).decode()
            # Run Tesseract OCR server-side so the LLM gets accurate text
            try:
                import pytesseract
                from PIL import ImageStat
                gray = img.convert("L")
                mean_px = ImageStat.Stat(gray).mean[0]
                if mean_px < 128:          # dark background (e.g. dark-mode receipts)
                    gray = ImageOps.invert(gray)
                gray = ImageEnhance.Contrast(gray).enhance(2.0)
                ocr_text = pytesseract.image_to_string(gray, config="--oem 3 --psm 3").strip()
            except Exception:
                pass
        except Exception:
            b64 = base64.b64encode(data).decode()

        # Regex: extract UTR/RRN directly from OCR text — purely numeric, 12 digits,
        # preceded by a UTR/RRN label. Excludes PhonePe T-prefixed transaction IDs.
        utr_match = re.search(
            r'(?:UTR\s*(?:No\.?|Number|#|:)?|RRN\s*(?:No\.?|:)?|UPI\s*Ref(?:erence)?\s*(?:No\.?|:)?|Bank\s*Ref(?:erence)?\s*(?:No\.?|:)?)\s*([0-9]{12,22})',
            ocr_text, re.IGNORECASE
        )
        extracted_utr = utr_match.group(1) if utr_match else ""

        return {"type": "image", "b64": b64, "filename": filename,
                "ocr_text": ocr_text, "extracted_utr": extracted_utr}

    if content_type == "application/pdf" or filename.lower().endswith(".pdf"):
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(data))
            text   = "\n\n".join(p.extract_text() or "" for p in reader.pages)
            return {"type": "text", "content": text, "filename": filename, "pages": len(reader.pages)}
        except ImportError:
            return {"type": "error", "error": "pypdf not installed"}
        except Exception as exc:
            return {"type": "error", "error": f"PDF read failed: {exc}"}

    if content_type in ("text/csv", "application/csv") or filename.lower().endswith(".csv"):
        try:
            text = data.decode("utf-8", errors="replace")
            rows = list(csv.reader(io.StringIO(text)))
            return {"type": "text", "content": "\n".join(",".join(r) for r in rows[:500]),
                    "filename": filename, "rows": len(rows)}
        except Exception as exc:
            return {"type": "error", "error": f"CSV read failed: {exc}"}

    if content_type == "application/json" or filename.lower().endswith(".json"):
        try:
            text = json.dumps(json.loads(data), indent=2, ensure_ascii=False)
            return {"type": "text", "content": text, "filename": filename}
        except Exception as exc:
            return {"type": "error", "error": f"JSON parse failed: {exc}"}

    try:
        return {"type": "text", "content": data.decode("utf-8", errors="replace"), "filename": filename}
    except Exception as exc:
        return {"type": "error", "error": f"Cannot read file: {exc}"}


@app.post("/chat")
async def chat(
    question:      str = Form(...),
    model:         str = Form(default=DEFAULT_MODEL),
    history:       str = Form(default="[]"),
    file_context:  str = Form(default=""),
    file_name:     str = Form(default=""),
    image_b64:     str = Form(default=""),
    ocr_text:      str = Form(default=""),
    extracted_utr: str = Form(default=""),
    user=Depends(_get_user),
):
    raw_prev = json.loads(history)

    # ── Shared: regex extractions from OCR (used by both Claude and Ollama paths) ─
    _amount_m = re.search(r'(?:Rs\.?|₹|INR)\s*([\d,]+(?:\.\d{1,2})?)', ocr_text, re.IGNORECASE) if ocr_text else None
    _status_m = re.search(r'\b(Success(?:ful)?|Completed|Failed|Pending|Declined)\b', ocr_text, re.IGNORECASE) if ocr_text else None
    _txn_id_m = re.search(r'\bT\d{15,}\b', ocr_text) if ocr_text else None  # PhonePe Transaction ID

    # ════════════════════════════════════════════════════════════════════════
    # Claude path — used when ANTHROPIC_API_KEY is set
    # ════════════════════════════════════════════════════════════════════════
    if ANTHROPIC_API_KEY:
        _claude_system = (
            "You are an expert at reading Indian payment receipts and UPI transaction screenshots. "
            "UTR (Unique Transaction Reference) / RRN is always exactly 12 purely-numeric digits, "
            "assigned by NPCI for every UPI transfer. "
            "PhonePe Transaction IDs start with the letter T — they are NOT the UTR. "
            "When OCR text or verified fields are provided, treat them as ground truth."
        )

        # Build message list in Claude format
        def _to_claude(m: dict) -> dict:
            if m["role"] == "assistant":
                return {"role": "assistant", "content": m["content"]}
            content: list = []
            if m.get("image_b64"):
                content.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/jpeg", "data": m["image_b64"]},
                })
            content.append({"type": "text", "text": m["content"]})
            return {"role": "user", "content": content}

        claude_msgs = [_to_claude(m) for m in raw_prev]

        # Build current user message content
        cur: list = []
        if image_b64:
            cur.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64},
            })

        # Assemble text block
        text_parts: list[str] = []
        if ocr_text:
            verified: list[str] = []
            if extracted_utr:
                verified.append(f"UTR Number: {extracted_utr}")
            if _txn_id_m:
                verified.append(f"PhonePe Transaction ID: {_txn_id_m.group(0)} (NOT the UTR)")
            if _amount_m:
                verified.append(f"Amount: ₹{_amount_m.group(1)}")
            if _status_m:
                verified.append(f"Status: {_status_m.group(1)}")
            if verified:
                text_parts.append("Verified (regex-extracted):\n" + "\n".join(verified))
            text_parts.append(f"Full OCR text from screenshot:\n{ocr_text}")
        elif file_context:
            text_parts.append(f"Uploaded file — {file_name}:\n```\n{file_context[:MAX_CTX_CHARS]}\n```")

        text_parts.append(question)
        cur.append({"type": "text", "text": "\n\n".join(text_parts)})
        claude_msgs.append({"role": "user", "content": cur})

        # Pick model: honour user's selection if it's a Claude model, else default
        claude_model = model if model.startswith("claude") else CLAUDE_DEFAULT

        async def generate_claude():
            try:
                client = _get_anthropic()
                async with client.messages.stream(
                    model=claude_model,
                    max_tokens=2048,
                    system=_claude_system,
                    messages=claude_msgs,
                ) as stream:
                    async for text in stream.text_stream:
                        yield f"data: {json.dumps({'content': text, 'done': False})}\n\n"
                yield f"data: {json.dumps({'content': '', 'done': True})}\n\n"
            except Exception as exc:
                yield f"data: {json.dumps({'content': f'\\n\\n[Error: {exc}]', 'done': True})}\n\n"

        return StreamingResponse(generate_claude(), media_type="text/event-stream")

    # ════════════════════════════════════════════════════════════════════════
    # Ollama fallback path
    # ════════════════════════════════════════════════════════════════════════
    def _to_ollama(m: dict) -> dict:
        msg: dict = {"role": m["role"], "content": m["content"]}
        if m.get("image_b64"):
            msg["images"] = [m["image_b64"]]
        return msg

    prev = [_to_ollama(m) for m in raw_prev]

    _vision_system = (
        "You are a payment receipt reader. "
        "Use only the OCR text provided — do not guess or invent any numbers."
    )

    if ocr_text:
        _known: list[str] = []
        if extracted_utr:
            _known.append(f"UTR Number: {extracted_utr}")
        if _txn_id_m:
            _known.append(f"PhonePe Transaction ID: {_txn_id_m.group(0)} (NOT the UTR)")
        if _amount_m:
            _known.append(f"Amount: ₹{_amount_m.group(1)}")
        if _status_m:
            _known.append(f"Status: {_status_m.group(1)}")
        _preamble = ("Confirmed:\n" + "\n".join(_known) + "\n\n") if _known else ""
        _vision_prompt = f"{_preamble}OCR text:\n{ocr_text}\n\nAnswer: {question}"
    else:
        _vision_prompt = (
            "Read this payment screenshot. "
            "UTR is a 12-digit number, NOT the PhonePe Transaction ID (which starts with T).\n\n"
            f"Answer: {question}"
        )

    if image_b64:
        if not prev:
            payload     = {"model": model, "prompt": _vision_prompt, "system": _vision_system, "images": [image_b64], "stream": True}
            endpoint    = f"{OLLAMA_BASE_URL}/api/generate"
            is_generate = True
        else:
            payload     = {"model": model, "messages": [*prev, {"role": "user", "content": _vision_prompt, "images": [image_b64]}], "stream": True}
            endpoint    = f"{OLLAMA_BASE_URL}/api/chat"
            is_generate = False
    elif any(m.get("images") for m in prev):
        payload     = {"model": model, "messages": [*prev, {"role": "user", "content": question}], "stream": True}
        endpoint    = f"{OLLAMA_BASE_URL}/api/chat"
        is_generate = False
    elif file_context:
        sys_msg  = {"role": "system", "content": f"File '{file_name}':\n```\n{file_context[:MAX_CTX_CHARS]}\n```"}
        payload  = {"model": model, "messages": [sys_msg, *prev, {"role": "user", "content": question}], "stream": True}
        endpoint    = f"{OLLAMA_BASE_URL}/api/chat"
        is_generate = False
    else:
        payload     = {"model": model, "messages": [*prev, {"role": "user", "content": question}], "stream": True}
        endpoint    = f"{OLLAMA_BASE_URL}/api/chat"
        is_generate = False

    async def generate_ollama():
        try:
            async with httpx.AsyncClient(timeout=180.0) as client:
                async with client.stream("POST", endpoint, json=payload) as resp:
                    async for line in resp.aiter_lines():
                        if not line:
                            continue
                        chunk   = json.loads(line)
                        content = chunk.get("response", "") if is_generate else chunk.get("message", {}).get("content", "")
                        done    = chunk.get("done", False)
                        yield f"data: {json.dumps({'content': content, 'done': done})}\n\n"
        except Exception as exc:
            yield f"data: {json.dumps({'content': f'\\n\\n[Error: {exc}]', 'done': True})}\n\n"

    return StreamingResponse(generate_ollama(), media_type="text/event-stream")
