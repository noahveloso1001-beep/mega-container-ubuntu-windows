"""
Noel Online - servidor completo (FastAPI + SQLite).

Feito para casar exatamente com o APK "Noel-Android-Modes-Online".

Rodar (no terminal do codespace):
    pip install fastapi "uvicorn[standard]"
    uvicorn noel_server:app --host 0.0.0.0 --port 8081

Os dados ficam no arquivo noel.db (mesma pasta). Apague-o para zerar tudo.
"""
import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

VERSION = "3.0"
CAPACITY = 30
ONLINE_WINDOW = 90          # segundos sem sinal => jogador considerado offline
CHAT_MAX_LEN = 200
DB_PATH = os.environ.get(
    "NOEL_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "noel.db")
)

# ----------------------------------------------------------------- banco ---
_db = sqlite3.connect(DB_PATH, check_same_thread=False)
_db.row_factory = sqlite3.Row
_lock = threading.RLock()

_db.executescript(
    """
CREATE TABLE IF NOT EXISTS accounts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL UNIQUE COLLATE NOCASE,
  salt TEXT NOT NULL,
  pass_hash TEXT NOT NULL,
  created REAL NOT NULL,
  last_seen REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions(
  token TEXT PRIMARY KEY,
  account_id INTEGER NOT NULL,
  created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS scores(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id INTEGER NOT NULL,
  beatmap_id INTEGER NOT NULL,
  beatmap_set_id INTEGER NOT NULL DEFAULT 0,
  score INTEGER NOT NULL,
  accuracy REAL NOT NULL DEFAULT 0,
  max_combo INTEGER NOT NULL DEFAULT 0,
  hit300 INTEGER NOT NULL DEFAULT 0,
  hit100 INTEGER NOT NULL DEFAULT 0,
  hit50 INTEGER NOT NULL DEFAULT 0,
  geki INTEGER NOT NULL DEFAULT 0,
  katu INTEGER NOT NULL DEFAULT 0,
  miss INTEGER NOT NULL DEFAULT 0,
  mods INTEGER NOT NULL DEFAULT 0,
  perfect INTEGER NOT NULL DEFAULT 0,
  title TEXT NOT NULL DEFAULT '',
  artist TEXT NOT NULL DEFAULT '',
  difficulty TEXT NOT NULL DEFAULT '',
  created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scores_map ON scores(beatmap_id, score DESC);
CREATE TABLE IF NOT EXISTS chat(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id INTEGER NOT NULL,
  username TEXT NOT NULL,
  text TEXT NOT NULL,
  created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS beatmaps(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  beatmap_id INTEGER NOT NULL DEFAULT 0,
  beatmap_set_id INTEGER NOT NULL DEFAULT 0,
  title TEXT NOT NULL DEFAULT '',
  artist TEXT NOT NULL DEFAULT '',
  creator TEXT NOT NULL DEFAULT '',
  difficulty TEXT NOT NULL DEFAULT '',
  bpm REAL NOT NULL DEFAULT 0,
  download_url TEXT NOT NULL DEFAULT '',
  uploader TEXT NOT NULL DEFAULT '',
  created REAL NOT NULL
);
"""
)
_db.commit()


def q(sql: str, args: tuple = (), commit: bool = False):
    with _lock:
        cur = _db.execute(sql, args)
        rows = cur.fetchall()
        lastrowid = cur.lastrowid
        if commit:
            _db.commit()
    return rows, lastrowid


# ---------------------------------------------------------------- helpers ---
def hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000
    ).hex()


class ApiError(Exception):
    def __init__(self, status: int, message: str, code: str = "error"):
        self.status, self.message, self.code = status, message, code


def err(status: int, message: str, code: str = "error"):
    raise ApiError(status, message, code)


async def read_body(request: Request) -> Dict[str, Any]:
    """Aceita JSON ou formulário; nunca devolve 422."""
    raw = await request.body()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        pass
    try:
        form = await request.form()
        return dict(form)
    except Exception:
        return {}


def to_int(v: Any, default: int = 0) -> int:
    try:
        if isinstance(v, bool):
            return int(v)
        return int(float(v))
    except Exception:
        return default


def to_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


def to_str(v: Any, limit: int = 200) -> str:
    return ("" if v is None else str(v)).strip()[:limit]


def bearer(request: Request) -> Optional[str]:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.query_params.get("token")


def account_from_token(token: Optional[str]):
    if not token:
        return None
    rows, _ = q(
        "SELECT a.* FROM sessions s JOIN accounts a ON a.id=s.account_id WHERE s.token=?",
        (token,),
    )
    return rows[0] if rows else None


def require_account(request: Request):
    acc = account_from_token(bearer(request))
    if not acc:
        err(401, "Entre na sua conta Noel para continuar.", "authentication_required")
    touch(acc["id"])
    return acc


def touch(account_id: int):
    q("UPDATE accounts SET last_seen=? WHERE id=?", (time.time(), account_id), commit=True)


def online_rows():
    rows, _ = q(
        "SELECT id, username FROM accounts WHERE last_seen>? ORDER BY username COLLATE NOCASE",
        (time.time() - ONLINE_WINDOW,),
    )
    return rows


def new_session(account_id: int) -> str:
    token = secrets.token_hex(24)  # 48 caracteres (o APK exige >= 40)
    q(
        "INSERT INTO sessions(token, account_id, created) VALUES(?,?,?)",
        (token, account_id, time.time()),
        commit=True,
    )
    return token


def session_payload(acc, token: str) -> Dict[str, Any]:
    account = {"id": acc["id"], "username": acc["username"]}
    return {
        "success": True,
        "token": token,
        "access_token": token,
        "token_type": "bearer",
        "username": acc["username"],
        "account": account,
    }


def total_score(account_id: int) -> int:
    rows, _ = q(
        "SELECT COALESCE(SUM(best),0) AS t FROM "
        "(SELECT MAX(score) AS best FROM scores WHERE account_id=? GROUP BY beatmap_id)",
        (account_id,),
    )
    return int(rows[0]["t"])


def chat_row(r) -> Dict[str, Any]:
    return {
        "id": r["id"],
        "username": r["username"],
        "text": r["text"],
        "message": r["text"],
        "time": r["created"],
    }


# -------------------------------------------------------------- websocket ---
class Hub:
    def __init__(self):
        self.clients: List[WebSocket] = []

    async def broadcast(self, payload: Dict[str, Any]):
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            if ws in self.clients:
                self.clients.remove(ws)


hub = Hub()
_last_chat: Dict[int, float] = {}


async def post_chat(acc, text: str) -> Dict[str, Any]:
    text = to_str(text, CHAT_MAX_LEN)
    if not text:
        err(400, "Mensagem vazia.", "empty_message")
    now = time.time()
    if now - _last_chat.get(acc["id"], 0) < 0.8:
        err(429, "Devagar! Espere um instante antes de enviar outra mensagem.", "rate_limited")
    _last_chat[acc["id"]] = now
    _, mid = q(
        "INSERT INTO chat(account_id, username, text, created) VALUES(?,?,?,?)",
        (acc["id"], acc["username"], text, now),
        commit=True,
    )
    msg = {"id": mid, "username": acc["username"], "text": text, "message": text, "time": now}
    await hub.broadcast({"type": "chat", **msg})
    return msg


# -------------------------------------------------------------------- app ---
app = FastAPI(title="Noel Online", version=VERSION)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


@app.exception_handler(ApiError)
async def _api_error(_: Request, e: ApiError):
    return JSONResponse(
        {"success": False, "error": e.message, "message": e.message, "code": e.code},
        status_code=e.status,
    )


@app.exception_handler(StarletteHTTPException)
async def _http_error(_: Request, e: StarletteHTTPException):
    msg = "Endereço não encontrado." if e.status_code == 404 else str(e.detail)
    return JSONResponse(
        {"success": False, "error": msg, "message": msg}, status_code=e.status_code
    )


@app.exception_handler(RequestValidationError)
async def _validation_error(_: Request, e: RequestValidationError):
    return JSONResponse(
        {"success": False, "error": "Dados inválidos.", "message": "Dados inválidos."},
        status_code=400,
    )


@app.exception_handler(Exception)
async def _unexpected(_: Request, e: Exception):
    print("ERRO INTERNO:", repr(e))
    return JSONResponse(
        {"success": False, "error": "Erro interno do servidor.", "message": "Erro interno do servidor."},
        status_code=500,
    )


# ----- status
def status_payload() -> Dict[str, Any]:
    n = len(online_rows())
    return {
        "server": "Noel Online",
        "name": "Noel Server",
        "version": VERSION,
        "status": "online",
        "mode": "online",
        "capacity": CAPACITY,
        "onlineCount": n,
        "systems": ["accounts", "ranking", "beatmaps", "chat", "scores", "players"],
    }


@app.get("/")
@app.get("/api")
@app.get("/status")
@app.get("/api/status")
async def status():
    return status_payload()


# ----- contas
@app.post("/api/register")
@app.post("/register")
@app.post("/api/account/register")
async def register(request: Request):
    b = await read_body(request)
    username = to_str(b.get("username") or b.get("name") or b.get("user"), 40)
    password = to_str(b.get("password") or b.get("pass"), 200)
    if not re.fullmatch(r"[A-Za-z0-9_\-]{3,20}", username):
        err(400, "Nome de usuário: 3 a 20 letras, números, _ ou -.", "invalid_username")
    if len(password) < 6:
        err(400, "A senha precisa ter pelo menos 6 caracteres.", "weak_password")
    if q("SELECT 1 FROM accounts WHERE username=?", (username,))[0]:
        err(409, "Esse nome de usuário já existe.", "username_taken")
    salt = secrets.token_hex(8)
    _, aid = q(
        "INSERT INTO accounts(username, salt, pass_hash, created, last_seen) VALUES(?,?,?,?,?)",
        (username, salt, hash_password(password, salt), time.time(), time.time()),
        commit=True,
    )
    acc = q("SELECT * FROM accounts WHERE id=?", (aid,))[0][0]
    return session_payload(acc, new_session(aid))


@app.post("/api/login")
@app.post("/login")
@app.post("/api/account/login")
async def login(request: Request):
    b = await read_body(request)
    username = to_str(b.get("username") or b.get("name") or b.get("user"), 40)
    password = to_str(b.get("password") or b.get("pass"), 200)
    rows, _ = q("SELECT * FROM accounts WHERE username=?", (username,))
    if not rows or hash_password(password, rows[0]["salt"]) != rows[0]["pass_hash"]:
        err(401, "Usuário ou senha incorretos.", "invalid_credentials")
    acc = rows[0]
    touch(acc["id"])
    return session_payload(acc, new_session(acc["id"]))


@app.get("/api/me")
async def me(request: Request):
    acc = require_account(request)
    return {
        "success": True,
        "account": {"id": acc["id"], "username": acc["username"]},
        "username": acc["username"],
        "totalBestScores": total_score(acc["id"]),
    }


@app.delete("/api/sessions/current")
@app.delete("/sessions/current")
async def logout(request: Request):
    tok = bearer(request)
    if tok:
        q("DELETE FROM sessions WHERE token=?", (tok,), commit=True)
    return {"success": True}


# ----- presença / jogadores
@app.post("/api/heartbeat")
async def heartbeat(request: Request):
    acc = account_from_token(bearer(request))
    if acc:
        touch(acc["id"])
    return {"success": True, "onlineCount": len(online_rows())}


@app.get("/api/players")
async def players():
    out = []
    for r in online_rows():
        t = total_score(r["id"])
        out.append({"username": r["username"], "score": t, "totalBestScores": t})
    return {"success": True, "players": out, "data": out, "onlineCount": len(out)}


# ----- chat
@app.get("/api/chat")
async def chat_history(after: int = 0, limit: int = 50):
    limit = max(1, min(limit, 100))
    if after > 0:
        rows, _ = q("SELECT * FROM chat WHERE id>? ORDER BY id ASC LIMIT ?", (after, limit))
    else:
        rows, _ = q("SELECT * FROM chat ORDER BY id DESC LIMIT ?", (limit,))
        rows = list(reversed(rows))
    msgs = [chat_row(r) for r in rows]
    return {"success": True, "messages": msgs, "data": msgs}


@app.post("/api/chat")
async def chat_send(request: Request):
    acc = require_account(request)
    b = await read_body(request)
    msg = await post_chat(acc, b.get("text") or b.get("message") or b.get("content"))
    return {"success": True, "message": msg["text"], "id": msg["id"], "chat": msg}


@app.websocket("/ws")
async def ws_chat(ws: WebSocket):
    await ws.accept()
    hub.clients.append(ws)
    acc = account_from_token(ws.query_params.get("token"))
    try:
        rows, _ = q("SELECT * FROM chat ORDER BY id DESC LIMIT 30")
        await ws.send_json({"type": "history", "messages": [chat_row(r) for r in reversed(rows)]})
        while True:
            raw = await ws.receive_text()
            try:
                data = json.loads(raw)
                if not isinstance(data, dict):
                    data = {"text": raw}
            except Exception:
                data = {"text": raw}
            who = acc or account_from_token(to_str(data.get("token"), 100))
            if not who:
                await ws.send_json({"type": "error", "error": "Entre na sua conta para falar no chat."})
                continue
            try:
                await post_chat(who, data.get("text") or data.get("message") or data.get("content"))
            except ApiError as e:
                await ws.send_json({"type": "error", "error": e.message})
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        if ws in hub.clients:
            hub.clients.remove(ws)


# ----- scores
@app.post("/api/scores")
@app.post("/api/score")
@app.post("/scores")
async def submit_score(request: Request):
    acc = require_account(request)
    b = await read_body(request)
    beatmap_id = to_int(b.get("beatmapId") or b.get("beatmap_id"))
    score = to_int(b.get("score"))
    if beatmap_id <= 0 and not b.get("title"):
        err(400, "Beatmap inválido.", "invalid_beatmap")
    if score < 0 or score > 2_000_000_000:
        err(400, "Pontuação inválida.", "invalid_score")
    if beatmap_id <= 0:  # mapa local sem id: usa um id estável derivado do título
        key = f"{b.get('artist','')}|{b.get('title','')}|{b.get('difficulty','')}"
        beatmap_id = -(int(hashlib.sha1(key.encode()).hexdigest()[:7], 16) + 1)
    acc_pct = max(0.0, min(100.0, to_float(b.get("accuracy"))))
    prev = q(
        "SELECT MAX(score) AS m FROM scores WHERE account_id=? AND beatmap_id=?",
        (acc["id"], beatmap_id),
    )[0][0]["m"]
    q(
        "INSERT INTO scores(account_id,beatmap_id,beatmap_set_id,score,accuracy,max_combo,"
        "hit300,hit100,hit50,geki,katu,miss,mods,perfect,title,artist,difficulty,created) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            acc["id"], beatmap_id, to_int(b.get("beatmapSetId") or b.get("beatmap_set_id")),
            score, acc_pct, to_int(b.get("maxCombo")),
            to_int(b.get("hit300")), to_int(b.get("hit100")), to_int(b.get("hit50")),
            to_int(b.get("geki")), to_int(b.get("katu")), to_int(b.get("miss")),
            to_int(b.get("mods")), to_int(b.get("perfect")),
            to_str(b.get("title")), to_str(b.get("artist")), to_str(b.get("difficulty")),
            time.time(),
        ),
        commit=True,
    )
    return {
        "success": True,
        "message": "Pontuação enviada!",
        "personalBest": prev is None or score > prev,
        "totalBestScores": total_score(acc["id"]),
    }


# ----- ranking
@app.get("/api/ranking")
@app.get("/api/leaderboard")
@app.get("/ranking")
async def ranking(beatmapId: Optional[str] = None, limit: int = 10):
    limit = max(1, min(limit, 100))
    items: List[Dict[str, Any]] = []
    bid = to_int(beatmapId, None) if beatmapId not in (None, "") else None
    if bid is None:
        rows, _ = q(
            "SELECT a.username AS username, SUM(b.best) AS total, COUNT(*) AS maps FROM "
            "(SELECT account_id, beatmap_id, MAX(score) AS best FROM scores "
            " GROUP BY account_id, beatmap_id) b "
            "JOIN accounts a ON a.id=b.account_id GROUP BY b.account_id "
            "ORDER BY total DESC LIMIT ?",
            (limit,),
        )
        for i, r in enumerate(rows, 1):
            items.append({
                "rank": i, "username": r["username"],
                "score": int(r["total"]), "totalBestScores": int(r["total"]),
                "maps": r["maps"],
            })
    else:
        rows, _ = q(
            "SELECT a.username AS username, MAX(s.score) AS score, "
            "s.accuracy, s.max_combo, s.mods, s.created FROM scores s "
            "JOIN accounts a ON a.id=s.account_id WHERE s.beatmap_id=? "
            "GROUP BY s.account_id ORDER BY score DESC LIMIT ?",
            (bid, limit),
        )
        for i, r in enumerate(rows, 1):
            items.append({
                "rank": i, "username": r["username"], "score": int(r["score"]),
                "accuracy": round(r["accuracy"], 2), "maxCombo": r["max_combo"],
                "mods": r["mods"], "time": r["created"],
            })
    # o APK lê "scores" (ranking) e "players"; as outras chaves são só por compatibilidade
    return {
        "success": True, "scores": items, "players": items,
        "leaderboard": items, "ranking": items, "data": items,
    }


# ----- beatmaps
def beatmap_row(r) -> Dict[str, Any]:
    return {
        "id": r["id"], "beatmapId": r["beatmap_id"], "beatmapSetId": r["beatmap_set_id"],
        "title": r["title"], "artist": r["artist"], "creator": r["creator"],
        "difficulty": r["difficulty"], "bpm": r["bpm"],
        "downloadUrl": r["download_url"], "uploader": r["uploader"], "time": r["created"],
    }


@app.get("/api/beatmaps")
@app.get("/api/maps")
@app.get("/beatmaps")
async def beatmaps(search: str = "", limit: int = 100):
    limit = max(1, min(limit, 500))
    like = f"%{search.strip()}%"
    rows, _ = q(
        "SELECT * FROM beatmaps WHERE title LIKE ? OR artist LIKE ? OR creator LIKE ? "
        "ORDER BY id DESC LIMIT ?",
        (like, like, like, limit),
    )
    items = [beatmap_row(r) for r in rows]
    return {"success": True, "beatmaps": items, "maps": items, "data": items}


@app.post("/api/beatmaps")
@app.post("/api/maps")
@app.post("/beatmaps")
async def add_beatmap(request: Request):
    acc = require_account(request)
    b = await read_body(request)
    title = to_str(b.get("title"))
    if not title:
        err(400, "Informe o título do beatmap.", "invalid_beatmap")
    _, nid = q(
        "INSERT INTO beatmaps(beatmap_id,beatmap_set_id,title,artist,creator,difficulty,bpm,"
        "download_url,uploader,created) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            to_int(b.get("beatmapId") or b.get("beatmap_id")),
            to_int(b.get("beatmapSetId") or b.get("beatmap_set_id")),
            title, to_str(b.get("artist")), to_str(b.get("creator")),
            to_str(b.get("difficulty")), to_float(b.get("bpm")),
            to_str(b.get("downloadUrl") or b.get("download_url"), 500),
            acc["username"], time.time(),
        ),
        commit=True,
    )
    row = q("SELECT * FROM beatmaps WHERE id=?", (nid,))[0][0]
    return {"success": True, "message": "Beatmap cadastrado.", "beatmap": beatmap_row(row)}
