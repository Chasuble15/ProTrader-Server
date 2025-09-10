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

    # Auto mode: start script automatically if enabled
    if load_auto_mode_from_db():
        try:
            items = get_selected_items()
            args = {
                "item_ids": [it["id"] for it in items],
                "items": [
                    {
                        "id": it["id"],
                        "name_fr": it["name_fr"],
                        "slug_fr": it["slug_fr"],
                        "level": it["level"],
                        "order": idx,
                        "img_blob": it["img_blob"],
                    }
                    for idx, it in enumerate(items)
                ],
            }
            cmd = {
                "type": "command",
                "command_id": int(time.time()*1000),
                "cmd": "start_script",
                "args": args,
            }
            await send_to_agent(cmd)
            await broadcast_ui({"type": "command_sent", "command_id": cmd["command_id"], "cmd": cmd["cmd"]})
        except Exception as e:
            print("[backend] auto start_script failed:", e)

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


# --- Settings: auto mode ------------------------------------------------------

def ensure_settings_schema(conn: sqlite3.Connection):
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )
    conn.commit()


def load_auto_mode_from_db() -> bool:
    conn = get_db()
    try:
        ensure_settings_schema(conn)
        cur = conn.cursor()
        cur.execute("SELECT value FROM settings WHERE key='auto_mode'")
        row = cur.fetchone()
        return bool(row and row["value"] == "1")
    finally:
        conn.close()


def save_auto_mode_to_db(auto: bool) -> None:
    conn = get_db()
    try:
        ensure_settings_schema(conn)
        conn.execute(
            "REPLACE INTO settings (key, value) VALUES ('auto_mode', ?)",
            ("1" if auto else "0",),
        )
        conn.commit()
    finally:
        conn.close()


def get_selected_slugs() -> List[str]:
    conn = get_db()
    try:
        ensure_selection_schema(conn)
        cur = conn.cursor()
        cur.execute(
            """
            SELECT i.slug_fr
            FROM selection_items s
            JOIN items i ON i.id = s.item_id
            ORDER BY s.position ASC
            """
        )
        rows = cur.fetchall()
        return [r["slug_fr"] for r in rows]
    finally:
        conn.close()


def get_selected_items() -> List[Dict[str, Any]]:
    conn = get_db()
    try:
        ensure_selection_schema(conn)
        cur = conn.cursor()
        cur.execute(
            """
            SELECT i.id, i.name_fr, i.slug_fr, i.level, i.img_blob
            FROM selection_items s
            JOIN items i ON i.id = s.item_id
            ORDER BY s.position ASC
            """
        )
        rows = cur.fetchall()
        return [row_to_item(r) for r in rows]
    finally:
        conn.close()

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


@app.get("/api/auto_mode")
def get_auto_mode_endpoint():
    return {"auto": load_auto_mode_from_db()}


@app.post("/api/auto_mode")
def save_auto_mode_endpoint(body: Dict[str, Any] = Body(...)):
    auto = bool(body.get("auto"))
    save_auto_mode_to_db(auto)
    return {"ok": True}


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
    # index existant
    cur.execute("CREATE INDEX IF NOT EXISTS idx_hdv_prices_slug_dt ON hdv_prices(slug, datetime)")
    # index couvrant supplémentaire pour les filtres fréquents
    cur.execute("CREATE INDEX IF NOT EXISTS idx_hdv_prices_slug_qty_dt ON hdv_prices(slug, qty, datetime)")
    conn.commit()



from typing import Iterable, Tuple

# Parsing ISO8601 très permissif (YYYY-MM-DD ou YYYY-MM-DDTHH:MM:SSZ)
def _parse_iso_to_utc_bounds(date_from: str | None, date_to: str | None) -> Tuple[str | None, str | None]:
    def _norm(s: str) -> str:
        s = s.strip()
        if len(s) == 10 and s[4] == "-" and s[7] == "-":
            # date seule -> début/fin de journée en UTC (on gérera ça côté SQL)
            return s
        # Accepte ...Z ou sans Z; on normalise en 'Z' si besoin
        if s.endswith("Z"):
            return s
        # si 'T' absent, on suppose jour entier (même cas 10 chars déjà géré)
        if "T" not in s:
            return s + "T00:00:00Z"
        return s + "Z" if not s.endswith("Z") else s
    return (_norm(date_from) if date_from else None,
            _norm(date_to) if date_to else None)

def _split_csv_ints(s: str | None) -> list[int]:
    if not s: return []
    out = []
    for x in s.split(","):
        x = x.strip()
        if x:
            out.append(int(x))
    return out

def _split_csv_strs(s: str | None) -> list[str]:
    if not s: return []
    return [x.strip() for x in s.split(",") if x.strip()]

def _bucket_expr(bucket: str) -> str | None:
    """
    Retourne une expression SQLite qui regroupe 'datetime' (ISO Z) par seau.
    """
    # datetime est au format 'YYYY-MM-DDTHH:MM:SSZ'
    if bucket == "minute":
        return "substr(datetime,1,16) || ':00Z'"
    if bucket == "hour":
        return "substr(datetime,1,13) || ':00:00Z'"
    if bucket == "day":
        return "substr(datetime,1,10) || 'T00:00:00Z'"
    return None  # 'raw'



@app.get("/api/hdv/resources")
def list_hdv_resources(qty: str | None = Query(default=None, description="Filtrer sur une ou plusieurs qty, ex: x1,x10,x100,x1000"),
                       limit: int = Query(default=1000, ge=1, le=10000)):
    conn = get_db()
    ensure_price_schema(conn)
    cur = conn.cursor()

    qtys = _split_csv_strs(qty)
    where = []
    params: list[Any] = []

    if qtys:
        qmarks = ",".join(["?"] * len(qtys))
        where.append(f"qty IN ({qmarks})")
        params.extend(qtys)

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    # Pour chaque slug : count total, last_seen, et dernier prix par qty
    # 1) stats de base
    cur.execute(f"""
        WITH base AS (
            SELECT slug,
                   COUNT(*) AS points,
                   MAX(datetime) AS last_seen
            FROM hdv_prices
            {where_sql}
            GROUP BY slug
            ORDER BY last_seen DESC
            LIMIT ?
        )
        SELECT b.slug, b.points, b.last_seen
        FROM base b
        ORDER BY b.last_seen DESC
    """, (*params, limit))
    rows = cur.fetchall()
    slugs = [dict(r) for r in rows]

    if not slugs:
        conn.close()
        return {"resources": []}

    # 2) derniers prix par qty pour chacun des slugs retenus
    #    on ramène un dict {slug: {qty: {price, datetime}}}
    slug_list = [r["slug"] for r in slugs]
    qmarks = ",".join(["?"] * len(slug_list))
    cur.execute(f"""
        SELECT p.slug, p.qty, p.price, p.datetime
        FROM hdv_prices p
        JOIN (
            SELECT slug, qty, MAX(datetime) AS max_dt
            FROM hdv_prices
            WHERE slug IN ({qmarks})
            GROUP BY slug, qty
        ) x
        ON p.slug = x.slug AND p.qty = x.qty AND p.datetime = x.max_dt
    """, (*slug_list,))
    last_price_rows = cur.fetchall()

    by_slug: Dict[str, Dict[str, Any]] = {r["slug"]: {} for r in slugs}
    for r in last_price_rows:
        by_slug[r["slug"]][r["qty"]] = {"price": r["price"], "datetime": r["datetime"]}

    # fusion
    out = []
    for r in slugs:
        out.append({
            "slug": r["slug"],
            "points": r["points"],
            "last_seen": r["last_seen"],
            "last_prices": by_slug.get(r["slug"], {})  # dict par qty
        })

    conn.close()
    return {"resources": out}


@app.get("/api/hdv/timeseries")
def get_hdv_timeseries(
    slugs: str = Query(..., description="CSV de slugs à inclure (obligatoire)"),
    qty: str | None = Query(default=None, description="CSV de quantités: x1,x10,x100,x1000"),
    date_from: str | None = Query(default=None, description="ISO8601, ex: 2025-08-01 ou 2025-08-01T12:00:00Z"),
    date_to: str | None = Query(default=None, description="ISO8601"),
    bucket: str = Query(default="raw", pattern="^(raw|minute|hour|day)$"),
    agg: str = Query(default="avg", pattern="^(avg|min|max)$"),
    limit_per_series: int = Query(default=10000, ge=1, le=200000)
):
    conn = get_db()
    ensure_price_schema(conn)
    cur = conn.cursor()

    slug_list = _split_csv_strs(slugs)
    if not slug_list:
        conn.close()
        return {"series": []}

    qtys = _split_csv_strs(qty)
    dt_from, dt_to = _parse_iso_to_utc_bounds(date_from, date_to)

    where = ["slug IN (" + ",".join(["?"] * len(slug_list)) + ")"]
    params: list[Any] = [*slug_list]

    if qtys:
        where.append("qty IN (" + ",".join(["?"] * len(qtys)) + ")")
        params.extend(qtys)

    # Gestion des bornes de temps : si la valeur fait 10 chars (YYYY-MM-DD), on étend à journée UTC
    if dt_from:
        if len(dt_from) == 10:
            where.append("datetime >= ?")
            params.append(dt_from + "T00:00:00Z")
        else:
            where.append("datetime >= ?")
            params.append(dt_from)
    if dt_to:
        if len(dt_to) == 10:
            # exclusif fin de journée +1
            where.append("datetime < ?")
            params.append(dt_to + "T23:59:59Z")
        else:
            where.append("datetime <= ?")
            params.append(dt_to)

    where_sql = "WHERE " + " AND ".join(where)

    bucket_sql = _bucket_expr(bucket)
    series = []

    if bucket_sql is None:
        # --- RAW POINTS ---
        # On sépare par (slug, qty) pour structurer la sortie
        cur.execute(f"""
            SELECT slug, qty, price, datetime
            FROM hdv_prices
            {where_sql}
            ORDER BY slug, qty, datetime ASC
            LIMIT ?
        """, (*params, limit_per_series * max(1, len(slug_list)) ))
        rows = cur.fetchall()

        # regrouper
        by_key: Dict[Tuple[str,str], list[dict]] = {}
        for r in rows:
            key = (r["slug"], r["qty"])
            by_key.setdefault(key, []).append({"t": r["datetime"], "price": r["price"]})

        for (slug, q), pts in by_key.items():
            # limite par série
            if len(pts) > limit_per_series:
                pts = pts[-limit_per_series:]
            series.append({
                "slug": slug,
                "qty": q,
                "bucket": "raw",
                "agg": None,
                "points": pts
            })
    else:
        # --- BUCKETED / AGGREGATED ---
        # On groupe par slug, qty, bucket_ts; agreg = avg|min|max
        agg_sql = {"avg": "AVG(price)", "min": "MIN(price)", "max": "MAX(price)"}[agg]
        cur.execute(f"""
            SELECT slug, qty, {bucket_sql} AS bucket_ts, {agg_sql} AS value
            FROM hdv_prices
            {where_sql}
            GROUP BY slug, qty, bucket_ts
            ORDER BY slug, qty, bucket_ts ASC
        """, (*params,))
        rows = cur.fetchall()

        by_key: Dict[Tuple[str,str], list[dict]] = {}
        for r in rows:
            key = (r["slug"], r["qty"])
            by_key.setdefault(key, []).append({"t": r["bucket_ts"], "value": r["value"]})

        for (slug, q), pts in by_key.items():
            if len(pts) > limit_per_series:
                pts = pts[-limit_per_series:]
            series.append({
                "slug": slug,
                "qty": q,
                "bucket": bucket,
                "agg": agg,
                "points": pts
            })

    conn.close()
    return {"series": series}
