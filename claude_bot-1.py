"""
Claude Bot - Telegram Mini App (FastAPI + Anthropic API), Claude-style dizayn.

O'rnatish:
    pip install fastapi uvicorn httpx anthropic

Muhit o'zgaruvchilari (env):
    BOT_TOKEN          - @BotFather'dan olingan token
    ANTHROPIC_API_KEY  - console.anthropic.com dagi API kalit
    ADMIN_ID           - sizning Telegram ID'ingiz (raqam)
    WEBAPP_URL         - HTTPS manzil, masalan https://sizning-domen.uz
    BUDGET_USD         - ixtiyoriy, standart 100 (limitga yetganda bot to'xtaydi)
    PORT               - ixtiyoriy, standart 8000
    DB_PATH            - baza fayli yo'li. Serverda DOIMIY diskka ko'rsating,
                         masalan /var/data/claude_bot.db (aks holda har deploy'da tarix o'chadi)

Ishga tushirish:
    python claude_bot.py

Admin buyruqlari (botda): /allow <id>, /deny <id>, /users, /backup
"""
import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl

import httpx
import uvicorn
from anthropic import AsyncAnthropic
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("claude_bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
WEBAPP_URL = os.environ.get("WEBAPP_URL", "").rstrip("/")
BUDGET = float(os.environ.get("BUDGET_USD", "100"))
PORT = int(os.environ.get("PORT", "8000"))
DB_PATH = os.environ.get("DB_PATH", "claude_bot.db")

SYSTEM = os.environ.get(
    "SYSTEM_PROMPT",
    "Siz Claude'siz. Foydalanuvchi qaysi tilda yozsa, shu tilda aniq va qisqa javob bering.",
)

MODELS = {
    "claude-haiku-5-5": {"name": "Haiku 5.5", "desc": "Eng tez va arzon"},
    "claude-sonnet-5-5": {"name": "Sonnet 5.5", "desc": "Kundalik ishlar uchun muvozanatli"},
    "claude-opus-5-5": {"name": "Opus 5.5", "desc": "Eng kuchli, qiyin vazifalar uchun"},
}
DEFAULT_MODEL = "claude-haiku-5-5"


def cost(model: str, tin: int, tout: int) -> float:
    """Taxminiy narx (USD), 1M token uchun."""
    if model == "claude-haiku-5-5":
        pin, pout = (0.10, 0.50) if tin <= 100_000 else (0.50, 2.50)
    elif model == "claude-sonnet-5-5":
        pin, pout = 2.0, 10.0
    else:
        pin, pout = 4.0, 20.0
    return (tin * pin + tout * pout) / 1_000_000


# ---------- baza ----------
os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
lock = threading.Lock()
db.executescript(
    """
create table if not exists users(id integer primary key, name text, allowed integer default 0);
create table if not exists chats(id integer primary key autoincrement, uid integer, title text, ts real);
create table if not exists msgs(id integer primary key autoincrement, chat_id integer, role text, content text, ts real);
create table if not exists usage(id integer primary key autoincrement, uid integer, model text, tin integer, tout integer, usd real, ts real);
create index if not exists i_chats_uid on chats(uid, ts);
create index if not exists i_msgs_chat on msgs(chat_id, id);
create index if not exists i_usage_uid on usage(uid);
"""
)


def run(sql, args=()):
    with lock:
        cur = db.execute(sql, args)
        db.commit()
        return cur.lastrowid


def get(sql, args=()):
    with lock:
        return db.execute(sql, args).fetchall()


def spent() -> float:
    return get("select coalesce(sum(usd),0) from usage")[0][0]


def stats(uid: int) -> dict:
    """Byudjet va token hisobi: umumiy va shu foydalanuvchi uchun."""
    mine = get("select coalesce(sum(tin),0), coalesce(sum(tout),0), coalesce(sum(usd),0) from usage where uid=?", (uid,))[0]
    total = get("select coalesce(sum(tin),0), coalesce(sum(tout),0), coalesce(sum(usd),0) from usage")[0]
    return {
        "spent": total[2],
        "budget": BUDGET,
        "left": max(0.0, BUDGET - total[2]),
        "my_in": mine[0],
        "my_out": mine[1],
        "my_usd": mine[2],
        "all_in": total[0],
        "all_out": total[1],
    }


def parse_id(s: str):
    """Faqat oddiy 0-9 raqamlar (str.isdigit() '²' kabi belgilarni ham o'tkazib yuboradi)."""
    return int(s) if re.fullmatch(r"[0-9]{1,15}", s) else None


def like(s: str) -> str:
    return "%" + s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def make_backup() -> str:
    path = DB_PATH + ".bak"
    with lock:
        dst = sqlite3.connect(path)
        db.backup(dst)
        dst.close()
    return path


# ---------- Telegram ----------
http = httpx.AsyncClient(timeout=45)


async def tg(method: str, **params):
    try:
        r = await http.post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", json=params)
        return r.json()
    except Exception as e:
        log.warning("tg %s: %s", method, e)
        return {}


def open_button():
    return {"inline_keyboard": [[{"text": "Claude'ni ochish", "web_app": {"url": WEBAPP_URL}}]]}


def is_allowed(uid: int) -> bool:
    if uid == ADMIN_ID:
        return True
    row = get("select allowed from users where id=?", (uid,))
    return bool(row and row[0]["allowed"])


async def handle_message(m: dict):
    uid = m["from"]["id"]
    name = m["from"].get("first_name", "")
    text = (m.get("text") or "").strip()
    run(
        "insert into users(id,name,allowed) values(?,?,0) on conflict(id) do update set name=excluded.name",
        (uid, name),
    )
    if uid == ADMIN_ID and text.startswith("/allow"):
        parts = text.split()
        target = parse_id(parts[1]) if len(parts) == 2 else None
        if target is not None:
            run("insert into users(id,name,allowed) values(?,?,1) on conflict(id) do update set allowed=1", (target, ""))
            await tg("sendMessage", chat_id=uid, text=f"{target} ruxsat oldi.")
            await tg("sendMessage", chat_id=target, text="Sizga ruxsat berildi. /start bosing.")
        else:
            await tg("sendMessage", chat_id=uid, text="Format: /allow 123456789")
        return
    if uid == ADMIN_ID and text.startswith("/deny"):
        parts = text.split()
        target = parse_id(parts[1]) if len(parts) == 2 else None
        if target is not None:
            run("update users set allowed=0 where id=?", (target,))
            await tg("sendMessage", chat_id=uid, text=f"{target} ruxsati olib tashlandi.")
        return
    if uid == ADMIN_ID and text.startswith("/users"):
        rows = get("select id,name,allowed from users order by allowed desc")
        lines = [f"{'✅' if r['allowed'] else '⏳'} {r['id']} {r['name']}" for r in rows]
        await tg("sendMessage", chat_id=uid, text="\n".join(lines) or "Foydalanuvchi yo'q")
        return
    if uid == ADMIN_ID and text.startswith("/backup"):
        try:
            path = await asyncio.to_thread(make_backup)
            with open(path, "rb") as f:
                data = f.read()
            os.remove(path)
            await http.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument",
                data={"chat_id": str(uid), "caption": "Baza nusxasi (claude_bot.db)"},
                files={"document": ("claude_bot.db", data)},
                timeout=120,
            )
        except Exception as e:
            log.exception("backup")
            await tg("sendMessage", chat_id=uid, text=f"Backup xatosi: {type(e).__name__}")
        return
    if is_allowed(uid):
        await tg(
            "sendMessage",
            chat_id=uid,
            text=f"Salom, {name}. Claude tayyor. Ilovani ochib yozishni boshlang.",
            reply_markup=open_button(),
        )
    else:
        await tg("sendMessage", chat_id=uid, text="Ruxsat so'rovi adminga yuborildi. Tasdiqlangach xabar keladi.")
        if ADMIN_ID:
            await tg(
                "sendMessage",
                chat_id=ADMIN_ID,
                text=f"Yangi so'rov: {name} (ID {uid})\nRuxsat berish: /allow {uid}",
            )


async def safe_handle(m: dict):
    try:
        await handle_message(m)
    except Exception:
        log.exception("handle_message")


async def poll():
    await tg("deleteWebhook")
    if WEBAPP_URL:
        await tg("setChatMenuButton", menu_button={"type": "web_app", "text": "Claude", "web_app": {"url": WEBAPP_URL}})
    offset = 0
    fails = 0
    while True:
        try:
            r = await http.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
                json={"offset": offset, "timeout": 30, "allowed_updates": ["message"]},
                timeout=40,
            )
            data = r.json()
            if not data.get("ok"):
                # masalan 409 (boshqa nusxa ishlayapti) yoki 429 - bo'sh siklda aylanib qolmaslik uchun
                raise RuntimeError(f"getUpdates {r.status_code}: {data.get('description')}")
            fails = 0
            for u in data.get("result", []):
                offset = u["update_id"] + 1  # xabar bir marta olinadi, xatoda qayta kelmaydi
                if "message" in u and "from" in u["message"]:
                    asyncio.create_task(safe_handle(u["message"]))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            fails += 1
            log.warning("poll xato (%s): %s", fails, e)
            await asyncio.sleep(min(3 * fails, 30))


@asynccontextmanager
async def lifespan(_):
    task = asyncio.create_task(poll())
    yield
    task.cancel()
    await http.aclose()


app = FastAPI(lifespan=lifespan)
ai = AsyncAnthropic()  # ANTHROPIC_API_KEY env'dan olinadi


# ---------- autentifikatsiya ----------
def auth(init: str) -> dict:
    try:
        d = dict(parse_qsl(init, keep_blank_values=True))
        h = d.pop("hash")
        check = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(hmac.new(secret, check.encode(), hashlib.sha256).hexdigest(), h):
            raise ValueError
        if time.time() - int(d["auth_date"]) > 7 * 86400:
            raise ValueError
        user = json.loads(d["user"])
    except Exception:
        raise HTTPException(401, "Telegram orqali oching")
    if not is_allowed(user["id"]):
        raise HTTPException(403, "Sizga hali ruxsat berilmagan")
    return user


def own_chat(uid: int, chat_id: int):
    rows = get("select * from chats where id=? and uid=?", (chat_id, uid))
    if not rows:
        raise HTTPException(404, "Suhbat topilmadi")
    return rows[0]


# ---------- API ----------
@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML


@app.get("/api/me")
async def me(x_init_data: str = Header("")):
    u = auth(x_init_data)
    return {"name": u.get("first_name", ""), "models": MODELS, "default": DEFAULT_MODEL, **stats(u["id"])}


@app.get("/api/chats")
async def chats(q: str = "", x_init_data: str = Header("")):
    u = auth(x_init_data)
    sql = "select c.id,c.title,c.ts from chats c where c.uid=? and exists(select 1 from msgs m where m.chat_id=c.id)"
    args = [u["id"]]
    q = q.strip()[:100]
    if q:
        sql += " and (c.title like ? escape '\\' or exists(select 1 from msgs m where m.chat_id=c.id and m.content like ? escape '\\'))"
        args += [like(q), like(q)]
    rows = get(sql + " order by c.ts desc limit 300", args)
    return [dict(r) for r in rows]


@app.post("/api/chats")
async def new_chat(x_init_data: str = Header("")):
    u = auth(x_init_data)
    return {"id": run("insert into chats(uid,title,ts) values(?,?,?)", (u["id"], "Yangi suhbat", time.time()))}


@app.get("/api/chats/{chat_id}")
async def chat_msgs(chat_id: int, x_init_data: str = Header("")):
    u = auth(x_init_data)
    own_chat(u["id"], chat_id)
    rows = get("select role,content from msgs where chat_id=? order by id", (chat_id,))
    return [dict(r) for r in rows]


@app.delete("/api/chats/{chat_id}")
async def del_chat(chat_id: int, x_init_data: str = Header("")):
    u = auth(x_init_data)
    own_chat(u["id"], chat_id)
    run("delete from msgs where chat_id=?", (chat_id,))
    run("delete from chats where id=?", (chat_id,))
    return {"ok": True}


@app.post("/api/send")
async def send(body: dict, x_init_data: str = Header("")):
    u = auth(x_init_data)
    try:
        chat_id = int(body.get("chat_id", 0))
    except (TypeError, ValueError):
        raise HTTPException(400, "Noto'g'ri suhbat")
    chat = own_chat(u["id"], chat_id)
    text = str(body.get("text", "")).strip()[:20000]
    model = body.get("model") if body.get("model") in MODELS else DEFAULT_MODEL
    if not text:
        raise HTTPException(400, "Bo'sh xabar")
    if spent() >= BUDGET:
        raise HTTPException(402, "Byudjet tugadi. BUDGET_USD ni oshiring yoki hisobni to'ldiring.")

    run("insert into msgs(chat_id,role,content,ts) values(?,?,?,?)", (chat["id"], "user", text, time.time()))
    if chat["title"] == "Yangi suhbat":
        run("update chats set title=?, ts=? where id=?", (text[:40], time.time(), chat["id"]))
    else:
        run("update chats set ts=? where id=?", (time.time(), chat["id"]))
    rows = get("select role,content from msgs where chat_id=? order by id desc limit 30", (chat["id"],))
    history = [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]
    while history and history[0]["role"] != "user":  # 30 ta limit tufayli assistant bilan boshlanib qolmasin
        history.pop(0)

    async def gen():
        acc, tin, tout, stream = [], 0, 0, None
        try:
            async with ai.messages.stream(model=model, max_tokens=4096, system=SYSTEM, messages=history) as s:
                stream = s
                async for t in s.text_stream:
                    acc.append(t)
                    yield t
                final = await s.get_final_message()
            tin, tout = final.usage.input_tokens, final.usage.output_tokens
        except Exception as e:
            log.warning("anthropic xato: %s", e)
            yield f"\n\n⚠️ Xato: {type(e).__name__}. API kalit va balansni tekshiring."
        finally:
            # Foydalanuvchi "to'xtatish" bossa ham (yoki ulanish uzilsa) javob va sarf saqlanadi
            if not (tin or tout) and stream is not None:
                try:
                    snap = stream.current_message_snapshot.usage
                    tin, tout = snap.input_tokens or 0, snap.output_tokens or 0
                except Exception:
                    pass
            if not (tin or tout) and acc:  # taxminiy hisob
                tin = sum(len(m["content"]) for m in history) // 3
                tout = len("".join(acc)) // 3
            if acc:
                run("insert into msgs(chat_id,role,content,ts) values(?,?,?,?)", (chat["id"], "assistant", "".join(acc), time.time()))
            usd = cost(model, tin, tout)
            if tin or tout:
                run("insert into usage(uid,model,tin,tout,usd,ts) values(?,?,?,?,?,?)", (u["id"], model, tin, tout, usd, time.time()))
        yield "\x00" + json.dumps({"usd": usd, "tin": tin, "tout": tout, **stats(u["id"])})

    return StreamingResponse(gen(), media_type="text/plain; charset=utf-8", headers={"X-Accel-Buffering": "no"})


# ---------- Frontend ----------
HTML = r"""<!doctype html>
<html lang="uz">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover,user-scalable=no">
<title>Claude</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
:root{--bg:#FAF9F5;--panel:#F3F1E9;--text:#141413;--mute:#73726C;--line:#E6E4DA;--accent:#D97757;--accent-d:#C6613F;--user:#EFEDE4;--code:#F0EEE6;--serif:"Tiempos Text",Georgia,"Times New Roman",serif;--sans:-apple-system,"Segoe UI",Roboto,system-ui,sans-serif}
[data-theme=dark]{--bg:#262624;--panel:#1F1E1D;--text:#F0EEE6;--mute:#A3A198;--line:#3B3A37;--accent:#D97757;--accent-d:#E58A6B;--user:#323230;--code:#1F1E1D}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{height:100%;margin:0}
body{background:var(--bg);color:var(--text);font-family:var(--sans);display:flex;flex-direction:column;height:100dvh;overflow:hidden}
button{font:inherit;color:inherit;background:none;border:0;cursor:pointer;padding:0}
button:focus-visible,textarea:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
header{display:flex;align-items:center;justify-content:space-between;padding:10px 12px;padding-top:max(10px,env(safe-area-inset-top));border-bottom:1px solid transparent}
.ib{width:40px;height:40px;border-radius:12px;display:grid;place-items:center;color:var(--mute)}
.ib:active{background:var(--panel)}
.pill{display:flex;align-items:center;gap:6px;padding:8px 12px;border-radius:12px;font-family:var(--serif);font-size:17px}
.pill:active{background:var(--panel)}
.pill svg{color:var(--mute)}
#scroll{flex:1;overflow-y:auto;padding:8px 16px 16px;scroll-behavior:smooth}
.col{max-width:720px;margin:0 auto}
#empty{height:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;gap:14px;padding-bottom:8vh}
#empty h1{font-family:var(--serif);font-weight:400;font-size:34px;margin:0;letter-spacing:-.01em}
#empty p{margin:0;color:var(--mute);font-size:15px}
.msg{margin:18px 0}
.msg.user{display:flex;justify-content:flex-end}
.msg.user .b{background:var(--user);padding:10px 14px;border-radius:18px;max-width:86%;white-space:pre-wrap;word-break:break-word;line-height:1.5;font-size:16px}
.msg.assistant{display:flex;gap:10px;align-items:flex-start}
.msg.assistant .av{flex:0 0 22px;margin-top:4px}
.msg.assistant .b{flex:1;min-width:0;font-family:var(--serif);font-size:17px;line-height:1.65;word-break:break-word}
.b p{margin:0 0 .85em}.b p:last-child{margin-bottom:0}
.b h2,.b h3,.b h4,.b h5{font-family:var(--serif);font-weight:600;margin:1.1em 0 .4em;line-height:1.3}
.b h2{font-size:1.3em}.b h3{font-size:1.15em}.b h4,.b h5{font-size:1em}
.b ul,.b ol{margin:0 0 .85em;padding-left:1.4em}.b li{margin:.25em 0}
.b a{color:var(--accent-d)}
.b code{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:.86em;background:var(--code);padding:.15em .4em;border-radius:6px}
.b pre{background:var(--code);border:1px solid var(--line);border-radius:12px;padding:12px 14px;overflow-x:auto;margin:0 0 .85em}
.b pre code{background:none;padding:0;font-size:.82em;line-height:1.55}
.acts{display:flex;gap:4px;margin-top:6px}
.acts button{font-family:var(--sans);font-size:13px;color:var(--mute);padding:4px 8px;border-radius:8px}
.acts button:active{background:var(--panel)}
.spin{animation:sp 1.6s linear infinite}
@keyframes sp{to{transform:rotate(360deg)}}
@media (prefers-reduced-motion:reduce){.spin{animation:none}#scroll{scroll-behavior:auto}}
footer{padding:8px 12px;padding-bottom:max(12px,env(safe-area-inset-bottom))}
.box{max-width:720px;margin:0 auto;background:var(--bg);border:1px solid var(--line);border-radius:24px;padding:8px 8px 8px 16px;display:flex;align-items:flex-end;gap:8px;box-shadow:0 2px 14px rgba(20,20,19,.06)}
[data-theme=dark] .box{background:var(--user)}
textarea{flex:1;border:0;background:none;color:var(--text);font:16px/1.5 var(--sans);resize:none;max-height:160px;padding:8px 0;outline:none}
textarea::placeholder{color:var(--mute)}
#go{width:38px;height:38px;border-radius:12px;background:var(--accent);color:#fff;display:grid;place-items:center;flex:0 0 auto}
#go:active{background:var(--accent-d)}
#go:disabled{opacity:.4}
.hint{max-width:720px;margin:6px auto 0;text-align:center;font-size:12px;color:var(--mute)}
#veil{position:fixed;inset:0;background:rgba(0,0,0,.35);opacity:0;pointer-events:none;transition:opacity .2s}
#veil.on{opacity:1;pointer-events:auto}
#drawer{position:fixed;top:0;bottom:0;left:0;width:min(84vw,320px);background:var(--panel);transform:translateX(-100%);transition:transform .22s ease;display:flex;flex-direction:column;padding-top:env(safe-area-inset-top)}
#drawer.on{transform:none}
.dh{padding:14px 16px 8px;font-family:var(--serif);font-size:22px;display:flex;align-items:center;gap:8px}
#newc{margin:6px 12px;padding:10px 12px;border-radius:12px;color:var(--accent-d);text-align:left;display:flex;gap:8px;align-items:center;font-weight:500}
#newc:active{background:var(--line)}
#list{flex:1;overflow-y:auto;padding:4px 8px}
.sr{padding:4px 12px 8px}
.sr input{width:100%;border:1px solid var(--line);background:var(--bg);color:var(--text);border-radius:10px;padding:9px 12px;font:15px var(--sans);outline:none}
.sr input:focus-visible{border-color:var(--accent)}
.grp{padding:12px 10px 4px;font-size:12px;color:var(--mute);letter-spacing:.02em}
.none{padding:14px 10px;font-size:14px;color:var(--mute)}
.tk{font-size:12px;color:var(--mute);padding:4px 8px;align-self:center}
#uk,#ua{margin-top:4px;line-height:1.45}
.ci{display:flex;align-items:center;border-radius:10px}
.ci.cur{background:var(--line)}
.ci button.t{flex:1;text-align:left;padding:10px 10px;font-size:15px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ci button.x{padding:8px 10px;color:var(--mute);font-size:18px}
.us{padding:14px 16px;padding-bottom:max(14px,env(safe-area-inset-bottom));border-top:1px solid var(--line);font-size:13px;color:var(--mute)}
.bar{height:6px;background:var(--line);border-radius:3px;margin:8px 0 6px;overflow:hidden}
.bar i{display:block;height:100%;background:var(--accent);width:0}
#sheet{position:fixed;left:50%;top:calc(56px + env(safe-area-inset-top));transform:translateX(-50%);width:min(92vw,360px);background:var(--bg);border:1px solid var(--line);border-radius:16px;box-shadow:0 10px 30px rgba(20,20,19,.15);padding:6px;display:none}
#sheet.on{display:block}
#sheet button{display:block;width:100%;text-align:left;padding:10px 12px;border-radius:10px}
#sheet button:active,#sheet button.cur{background:var(--panel)}
#sheet b{font-family:var(--serif);font-weight:500;font-size:16px;display:block}
#sheet span{font-size:13px;color:var(--mute)}
</style>
</head>
<body>
<header>
  <button class="ib" id="menu" aria-label="Suhbatlar"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M4 7h16M4 12h16M4 17h10"/></svg></button>
  <button class="pill" id="mp"><span id="mn">Haiku 5.5</span><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg></button>
  <button class="ib" id="nw" aria-label="Yangi suhbat"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9M16.5 3.5a2.1 2.1 0 013 3L7 19l-4 1 1-4z"/></svg></button>
</header>
<div id="sheet"></div>
<div id="scroll"><div class="col" id="msgs"></div></div>
<footer>
  <div class="box">
    <textarea id="inp" rows="1" placeholder="Claude'ga yozing"></textarea>
    <button id="go" aria-label="Yuborish"></button>
  </div>
  <div class="hint">Claude xato qilishi mumkin. Muhim ma'lumotlarni tekshiring.</div>
</footer>
<div id="veil"></div>
<aside id="drawer">
  <div class="dh" id="dh"></div>
  <button id="newc"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>Yangi suhbat</button>
  <div class="sr"><input id="q" type="search" placeholder="Tarixdan qidirish" autocomplete="off"></div>
  <div id="list"></div>
  <div class="us"><div id="ut"></div><div class="bar"><i id="ub"></i></div><div id="uk"></div><div id="ua"></div><div id="ud"></div></div>
</aside>
<script>
const tg=window.Telegram&&Telegram.WebApp; if(tg){tg.ready();tg.expand()}
const initData=tg?tg.initData:'';
const theme=(tg&&tg.colorScheme)||(matchMedia('(prefers-color-scheme:dark)').matches?'dark':'light');
document.documentElement.dataset.theme=theme;
const $=id=>document.getElementById(id);
let me=null,chatId=null,model='claude-haiku-5-5',busy=false,ctrl=null;
try{model=localStorage.getItem('model')||model}catch(e){}

function spark(size,cls){let l='';for(let i=0;i<12;i++){const long=i%2===0;const a=i*30*Math.PI/180,r1=13,r2=long?44:33;l+=`<line x1="${(Math.cos(a)*r1).toFixed(1)}" y1="${(Math.sin(a)*r1).toFixed(1)}" x2="${(Math.cos(a)*r2).toFixed(1)}" y2="${(Math.sin(a)*r2).toFixed(1)}"/>`}
return `<svg class="${cls||''}" width="${size}" height="${size}" viewBox="-50 -50 100 100" stroke="#D97757" stroke-width="8" stroke-linecap="round">${l}</svg>`}
const SEND='<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5M5 12l7-7 7 7"/></svg>';
const STOP='<svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><rect x="5" y="5" width="14" height="14" rx="3"/></svg>';

async function api(path,opt={}){
  const r=await fetch(path,{...opt,headers:{'X-Init-Data':initData,'Content-Type':'application/json',...(opt.headers||{})}});
  if(!r.ok){let d='';try{d=(await r.json()).detail}catch(e){}throw new Error(d||r.status)}
  return r.json();
}
const esc=t=>t.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
const fmt=n=>Number(n||0).toLocaleString('en-US');
function inl(t){const codes=[];
  t=t.replace(/`([^`]+)`/g,(m,c)=>{codes.push(c);return '\u0002'+(codes.length-1)+'\u0002'});
  t=t.replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>').replace(/(^|[^*])\*([^*\n]+)\*/g,'$1<em>$2</em>').replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,'<a href="$2" target="_blank" rel="noopener">$1</a>');
  return t.replace(/\u0002(\d+)\u0002/g,(m,i)=>'<code>'+codes[i]+'</code>')}
function md(s){
  const blocks=[];
  s=s.replace(/```(\w*)\n?([\s\S]*?)(```|$)/g,(m,l,c)=>{blocks.push('<pre><code>'+esc(c.replace(/\n$/,''))+'</code></pre>');return '\n\u0001'+(blocks.length-1)+'\u0001\n'});
  s=esc(s);
  let out='',list=null,para=[];
  const fp=()=>{if(para.length){out+='<p>'+inl(para.join('<br>'))+'</p>';para=[]}};
  const fl=()=>{if(list){out+='</'+list+'>';list=null}};
  for(const ln of s.split('\n')){let m;
    if(m=ln.match(/^(#{1,4})\s+(.*)/)){fp();fl();const n=m[1].length+1;out+='<h'+n+'>'+inl(m[2])+'</h'+n+'>'}
    else if(m=ln.match(/^\s*[-*]\s+(.*)/)){fp();if(list!=='ul'){fl();out+='<ul>';list='ul'}out+='<li>'+inl(m[1])+'</li>'}
    else if(m=ln.match(/^\s*\d+[.)]\s+(.*)/)){fp();if(list!=='ol'){fl();out+='<ol>';list='ol'}out+='<li>'+inl(m[1])+'</li>'}
    else if(!ln.trim()){fp();fl()}
    else if(/^\u0001\d+\u0001$/.test(ln.trim())){fp();fl();out+=ln.trim()}
    else{fl();para.push(ln)}}
  fp();fl();
  return out.replace(/\u0001(\d+)\u0001/g,(m,i)=>blocks[i])}

function showEmpty(){
  $('msgs').innerHTML=`<div id="empty">${spark(48)}<h1>Salom, ${esc(me?me.name:'')}</h1><p>Bugun nimada yordam beray?</p></div>`}
function addMsg(role,text,live){
  if($('empty'))$('msgs').innerHTML='';
  const d=document.createElement('div');d.className='msg '+role;
  if(role==='user'){d.innerHTML='<div class="b"></div>';d.firstChild.textContent=text;$('msgs').appendChild(d);scrollDown();return {}}
  d.innerHTML=`<div class="av">${spark(22,live?'spin':'')}</div><div class="b"></div>`;
  const body=d.querySelector('.b');body.innerHTML=md(text);$('msgs').appendChild(d);scrollDown();
  return {d,body,av:d.querySelector('.av')}}
function acts(el,text,info){
  const a=document.createElement('div');a.className='acts';
  a.innerHTML='<button>Nusxalash</button>';
  a.firstChild.onclick=async e=>{try{await navigator.clipboard.writeText(text);e.target.textContent='Nusxalandi';setTimeout(()=>e.target.textContent='Nusxalash',1500)}catch(x){}};
  if(info&&(info.tin||info.tout)){const t=document.createElement('span');t.className='tk';t.textContent=fmt(info.tin+info.tout)+' token · $'+info.usd.toFixed(4);a.appendChild(t)}
  el.body.appendChild(a)}
function scrollDown(){const s=$('scroll');s.scrollTop=s.scrollHeight}

function setBtn(){$('go').innerHTML=busy?STOP:SEND}
function fit(){const t=$('inp');t.style.height='auto';t.style.height=Math.min(t.scrollHeight,160)+'px'}
function usage(u){
  const p=Math.min(100,u.spent/u.budget*100);
  $('ub').style.width=p+'%';$('ut').textContent='Sarflandi: $'+u.spent.toFixed(2)+' / $'+u.budget.toFixed(0)+' · qoldi $'+u.left.toFixed(2);
  $('uk').textContent='Sizning tokenlaringiz: '+fmt(u.my_in+u.my_out)+' (kirish '+fmt(u.my_in)+', chiqish '+fmt(u.my_out)+')';
  $('ua').textContent='Umumiy tokenlar: '+fmt(u.all_in+u.all_out);
  $('ud').textContent='Taxminiy hisob. Aniq balans Anthropic Console\'da.'}

async function send(){
  if(busy){ctrl&&ctrl.abort();return}
  const text=$('inp').value.trim();if(!text)return;
  try{if(!chatId){chatId=(await api('/api/chats',{method:'POST'})).id}}catch(e){addMsg('assistant','⚠️ '+e.message);return}
  $('inp').value='';fit();addMsg('user',text);
  const el=addMsg('assistant','',true);
  busy=true;setBtn();ctrl=new AbortController();let acc='',info=null;
  const paint=()=>{el.body.innerHTML=md(acc);scrollDown()};
  try{
    const res=await fetch('/api/send',{method:'POST',headers:{'Content-Type':'application/json','X-Init-Data':initData},body:JSON.stringify({chat_id:chatId,text,model}),signal:ctrl.signal});
    if(!res.ok){let d=res.status;try{d=(await res.json()).detail}catch(e){}acc='⚠️ '+d;paint()}
    else{
      const rd=res.body.getReader(),dec=new TextDecoder();let buf='',raf=0;
      while(true){const {done,value}=await rd.read();if(done)break;
        buf+=dec.decode(value,{stream:true});const i=buf.indexOf('\u0000');
        acc=i<0?buf:buf.slice(0,i);
        if(!raf)raf=requestAnimationFrame(()=>{raf=0;paint()});
        if(i>=0){try{info=JSON.parse(buf.slice(i+1));usage(info)}catch(e){}}}
      paint()}
  }catch(e){if(e.name!=='AbortError'){acc+='\n\n⚠️ Ulanish uzildi';paint()}}
  el.av.innerHTML=spark(22);
  if(acc&&!acc.startsWith('⚠️'))acts(el,acc,info);
  busy=false;setBtn();loadChats()}

function grpName(ts){
  const day=x=>new Date(x.getFullYear(),x.getMonth(),x.getDate()).getTime();
  const diff=Math.round((day(new Date())-day(new Date(ts*1000)))/864e5);
  return diff<=0?'Bugun':diff===1?'Kecha':diff<=7?'Oxirgi 7 kun':diff<=30?'Oxirgi 30 kun':'Avvalroq'}
let qv='';
async function loadChats(){
  try{const rows=await api('/api/chats?q='+encodeURIComponent(qv));
  const L=$('list');L.innerHTML='';
  if(!rows.length){const e=document.createElement('div');e.className='none';e.textContent=qv?'Hech narsa topilmadi':'Tarix hozircha bo\'sh';L.appendChild(e)}
  let g='';
  for(const c of rows){
    const n=grpName(c.ts);if(n!==g){g=n;const h=document.createElement('div');h.className='grp';h.textContent=n;L.appendChild(h)}
    const d=document.createElement('div');d.className='ci'+(c.id===chatId?' cur':'');
    d.innerHTML='<button class="t"></button><button class="x" aria-label="O\'chirish">×</button>';
    d.querySelector('.t').textContent=c.title;
    d.querySelector('.t').onclick=()=>{openChat(c.id);drawer(false)};
    d.querySelector('.x').onclick=async()=>{if(!confirm('Suhbat o\'chirilsinmi?'))return;await api('/api/chats/'+c.id,{method:'DELETE'});if(c.id===chatId)newChat();loadChats()};
    L.appendChild(d)}}catch(e){}}
async function openChat(id){
  chatId=id;$('msgs').innerHTML='';
  const rows=await api('/api/chats/'+id);
  if(!rows.length)showEmpty();
  for(const m of rows){const el=addMsg(m.role,m.content);if(m.role==='assistant')acts(el,m.content)}
  scrollDown();loadChats()}
function newChat(){chatId=null;showEmpty();loadChats()}
function drawer(on){$('drawer').classList.toggle('on',on);$('veil').classList.toggle('on',on)}

function buildSheet(){
  const s=$('sheet');s.innerHTML='';
  for(const [id,m] of Object.entries(me.models)){
    const b=document.createElement('button');if(id===model)b.className='cur';
    b.innerHTML='<b></b><span></span>';b.querySelector('b').textContent=m.name;b.querySelector('span').textContent=m.desc;
    b.onclick=()=>{model=id;try{localStorage.setItem('model',id)}catch(e){}$('mn').textContent=m.name;s.classList.remove('on');buildSheet()};
    s.appendChild(b)}
  $('mn').textContent=(me.models[model]||me.models[me.default]).name}

let qt=0;$('q').addEventListener('input',e=>{clearTimeout(qt);qt=setTimeout(()=>{qv=e.target.value.trim();loadChats()},250)});
$('go').onclick=send;
$('inp').addEventListener('input',fit);
$('inp').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey&&!matchMedia('(pointer:coarse)').matches){e.preventDefault();send()}});
$('menu').onclick=()=>drawer(true);$('veil').onclick=()=>{drawer(false);$('sheet').classList.remove('on')};
$('nw').onclick=newChat;$('newc').onclick=()=>{newChat();drawer(false)};
$('mp').onclick=()=>{$('sheet').classList.toggle('on')};
$('dh').innerHTML=spark(26)+'<span>Claude</span>';
setBtn();
(async()=>{
  try{me=await api('/api/me');if(!me.models[model])model=me.default;buildSheet();usage(me);showEmpty();loadChats()}
  catch(e){$('msgs').innerHTML='<div id="empty">'+spark(48)+'<h1>Kirish yopiq</h1><p>'+esc(e.message)+'</p></div>'}
})();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
