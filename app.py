# backend/app.py
# pip install fastapi "uvicorn[standard]" websockets
import asyncio, json, time
from typing import Optional, Dict, Any, List
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Body
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

# CORS (utile en dev; resserre en prod)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

agent_ws: Optional[WebSocket] = None
pending_cmds: List[Dict[str, Any]] = []
ui_clients: List[WebSocket] = []

async def broadcast_ui(message: Dict[str, Any]):
    for ws in ui_clients[:]:
        try:
            await ws.send_json(message)
        except Exception:
            try: await ws.close()
            except: pass
            ui_clients.remove(ws)

async def send_to_agent(msg: Dict[str, Any]):
    if agent_ws is None:
        raise RuntimeError("Agent offline")
    await agent_ws.send_text(json.dumps(msg))

@app.websocket("/ws/ui")
async def ws_ui(ws: WebSocket):
    await ws.accept()
    ui_clients.append(ws)
    await ws.send_json({"type":"agent_status","connected": agent_ws is not None})
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        if ws in ui_clients: ui_clients.remove(ws)

@app.websocket("/ws/agent")
async def ws_agent(ws: WebSocket):
    global agent_ws
    await ws.accept()
    agent_ws = ws
    print("[backend] agent connected")
    await broadcast_ui({"type":"agent_status","connected": True})

    while pending_cmds:
        try:
            cmd = pending_cmds.pop(0)
            await send_to_agent(cmd)
        except Exception as e:
            print("[backend] send pending failed:", e)
            break

    try:
        while True:
            text = await ws.receive_text()
            msg = json.loads(text)
            # Persistance auto des prix OCR envoyés par l’agent
            if msg.get("type") == "hdv_price":
                try:
                    d = msg.get("data", {}) or {}
                    save_price_row(
                        slug=d.get("slug", ""),
                        qty=d.get("qty", ""),
                        price=int(d.get("price", 0)),
                        ts=msg.get("ts")
                    )
                except Exception as e:
                    print("[backend] save_price failed:", e)

                    print("[backend] from agent:", msg)
            await broadcast_ui(msg)
    except WebSocketDisconnect:
        pass
    finally:
        agent_ws = None
        print("[backend] agent disconnected")
        await broadcast_ui({"type":"agent_status","connected": False})

@app.post("/api/cmd")
async def post_cmd(body: Dict[str, Any] = Body(...)):
    cmd = {
        "type": "command",
        "command_id": int(time.time()*1000),
        "cmd": body.get("cmd", ""),
        "args": body.get("args", {}) or {}
    }
    if agent_ws is None:
        pending_cmds.append(cmd)
        return {"status":"queued", "command_id": cmd["command_id"]}
    try:
        await send_to_agent(cmd)
        await broadcast_ui({"type":"command_sent","command_id": cmd["command_id"], "cmd": cmd["cmd"]})
        return {"status":"sent", "command_id": cmd["command_id"]}
    except Exception as e:
        pending_cmds.append(cmd)
        raise HTTPException(503, f"Agent offline, queued: {e}")

@app.get("/healthz")
def healthz():
    return {"ok": True, "agent_connected": agent_ws is not None}


# --- SQLite Items API ---------------------------------------------------------
import sqlite3, base64
from fastapi import Query
from pydantic import BaseModel


DB_PATH = "dofus_items.db"  # adapte le chemin si besoin

def get_db():
  conn = sqlite3.connect(DB_PATH)
  conn.row_factory = sqlite3.Row
  return conn


# Crée la table si besoin
def ensure_selection_schema(conn: sqlite3.Connection):
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS selection_items (
            item_id INTEGER NOT NULL PRIMARY KEY,
            position INTEGER NOT NULL
            -- on pourrait ajouter created_at/updated_at si besoin
        )
    """)
    conn.commit()


class SaveSelectionBody(BaseModel):
    ids: List[int]


def row_to_item(row: sqlite3.Row):
  # img_blob est un BLOB (bytes) -> base64 (str)
  blob = row["img_blob"]
  if blob is not None and isinstance(blob, (bytes, bytearray)):
    img_b64 = base64.b64encode(blob).decode("ascii")
  else:
    img_b64 = ""
  return {
    "id": row["id"],
    "name_fr": row["name_fr"],
    "slug_fr": row["slug_fr"],
    "level": row["level"],
    "img_blob": img_b64,
  }


@app.get("/api/selection")
def get_selection():
    conn = get_db()
    ensure_selection_schema(conn)
    cur = conn.cursor()

    # on récupère les items dans l'ordre de selection_items.position
    cur.execute("""
        SELECT i.id, i.name_fr, i.slug_fr, i.level, i.img_blob
        FROM selection_items s
        JOIN items i ON i.id = s.item_id
        ORDER BY s.position ASC
    """)
    rows = cur.fetchall()
    items = [row_to_item(r) for r in rows]
    conn.close()
    return {"items": items}


@app.post("/api/selection")
def save_selection(body: SaveSelectionBody):
    conn = get_db()
    ensure_selection_schema(conn)
    cur = conn.cursor()
    try:
        cur.execute("BEGIN")
        # stratégie simple : on remplace toute la sélection
        cur.execute("DELETE FROM selection_items")
        for pos, item_id in enumerate(body.ids):
            cur.execute(
                "INSERT INTO selection_items (item_id, position) VALUES (?, ?)",
                (item_id, pos)
            )
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, f"Failed to save selection: {e}")
    finally:
        conn.close()
    return {"ok": True, "count": len(body.ids)}


@app.get("/api/items")
def get_items(
  query: str | None = Query(default=None, description="Recherche sur name_fr/slug_fr"),
  limit: int = Query(default=20, ge=1, le=200),
  ids: str | None = Query(default=None, description="Liste d'ids séparés par des virgules")
):
  conn = get_db()
  cur = conn.cursor()

  if ids:
    try:
      id_list = [int(x) for x in ids.split(",") if x.strip()]
    except ValueError:
      raise HTTPException(400, "ids must be comma-separated integers")
    if not id_list:
      return {"items": []}
    qmarks = ",".join(["?"] * len(id_list))
    cur.execute(f"""
      SELECT id, name_fr, slug_fr, level, img_blob
      FROM items
      WHERE id IN ({qmarks})
      LIMIT ?
    """, (*id_list, limit))
  elif query:
    like = f"%{query.strip()}%"
    cur.execute("""
      SELECT id, name_fr, slug_fr, level, img_blob
      FROM items
      WHERE name_fr LIKE ? OR slug_fr LIKE ?
      ORDER BY level DESC, name_fr ASC
      LIMIT ?
    """, (like, like, limit))
  else:
    cur.execute("""
      SELECT id, name_fr, slug_fr, level, img_blob
      FROM items
      ORDER BY level DESC, name_fr ASC
      LIMIT ?
    """, (limit,))

  rows = cur.fetchall()
  items = [row_to_item(r) for r in rows]
  conn.close()
  return {"items": items}


# --- PRICES: persistance OCR HDV ---------------------------------------------
from datetime import datetime, timezone


def ensure_price_schema(conn: sqlite3.Connection):
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS hdv_prices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT NOT NULL,
            qty  TEXT NOT NULL CHECK(qty IN ('x1','x10','x100','x1000')),
            price INTEGER NOT NULL,
            datetime TEXT NOT NULL
                DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_hdv_prices_slug_dt ON hdv_prices(slug, datetime)")
    conn.commit()

def save_price_row(slug: str, qty: str, price: int, ts: int | None):
    if not slug or not qty:
        raise ValueError("slug/qty manquants")
    # ts de l'agent = secondes epoch → ISO UTC
    iso = datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ') if ts else \
          datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    conn = get_db()
    try:
        ensure_price_schema(conn)
        conn.execute(
            "INSERT INTO hdv_prices (slug, qty, price, datetime) VALUES (?,?,?,?)",
            (slug, qty, int(price), iso)
        )
        conn.commit()
    finally:
        conn.close()
