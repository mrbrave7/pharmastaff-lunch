"""SQLite data layer (async, via aiosqlite) for the ordering model.

Tables
------
meta(key, value)                        # 'ordering_open' -> '0' | '1'
subscribers(user_id, name, username)    # everyone who used the bot / app
menu_items(id, name, price, image_path, position)
orders(id, user_id, status, total, screenshot_path, reviewed_by, created_at, updated_at)
order_items(order_id, menu_item_id, name, unit_price, qty)   # price snapshot

Order status:  pending_payment → awaiting_review → confirmed
               (reject sends it back to pending_payment; cancel/expire is terminal)
"""
import asyncio

import aiosqlite

import config

_conn: aiosqlite.Connection | None = None
_write_lock = asyncio.Lock()

ACTIVE_STATUSES = ("pending_payment", "awaiting_review", "confirmed")
# Cancel is allowed only before payment is sent. Once the user uploads the
# screenshot (awaiting_review) or it's confirmed, the order is locked.
CANCELLABLE_STATUSES = ("pending_payment",)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS subscribers (
    user_id  INTEGER PRIMARY KEY,
    name     TEXT,
    username TEXT
);
CREATE TABLE IF NOT EXISTS menu_items (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    price      INTEGER NOT NULL,
    image_path TEXT,
    position   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL,
    status          TEXT NOT NULL,
    total           INTEGER NOT NULL DEFAULT 0,
    screenshot_path TEXT,
    reviewed_by     INTEGER,
    created_at      TEXT,
    updated_at      TEXT
);
CREATE TABLE IF NOT EXISTS order_items (
    order_id      INTEGER NOT NULL,
    menu_item_id  INTEGER,
    name          TEXT NOT NULL,
    unit_price    INTEGER NOT NULL,
    qty           INTEGER NOT NULL
);
"""


def _now() -> str:
    return config.now_local().isoformat(timespec="seconds")


async def init() -> None:
    global _conn
    _conn = await aiosqlite.connect(config.DB_FILE)
    _conn.row_factory = aiosqlite.Row
    await _conn.execute("PRAGMA journal_mode=WAL;")
    await _conn.execute("PRAGMA foreign_keys=ON;")
    await _conn.executescript(_SCHEMA)
    await _conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES ('ordering_open', '0')"
    )
    # Seed the editable payment card from .env on first run; admins can change it
    # later from the panel (the DB value then wins on restarts).
    await _conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES ('card_number', ?)",
        (config.CARD_NUMBER,),
    )
    await _conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES ('card_holder', ?)",
        (config.CARD_HOLDER,),
    )
    await _conn.commit()


async def close() -> None:
    if _conn is not None:
        await _conn.close()


# ── Subscribers ──────────────────────────────────────────────────────────────
async def add_subscriber(user_id: int, name: str, username: str | None) -> None:
    async with _write_lock:
        await _conn.execute(
            "INSERT INTO subscribers(user_id, name, username) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET name=excluded.name, "
            "username=excluded.username",
            (user_id, name, username),
        )
        await _conn.commit()


async def get_subscriber_ids() -> list[int]:
    cur = await _conn.execute("SELECT user_id FROM subscribers")
    return [r["user_id"] for r in await cur.fetchall()]


async def count_subscribers() -> int:
    cur = await _conn.execute("SELECT COUNT(*) AS c FROM subscribers")
    return (await cur.fetchone())["c"]


async def get_name(user_id: int) -> str:
    cur = await _conn.execute("SELECT name FROM subscribers WHERE user_id=?", (user_id,))
    r = await cur.fetchone()
    return r["name"] if r and r["name"] else str(user_id)


async def get_username(user_id: int) -> str | None:
    cur = await _conn.execute("SELECT username FROM subscribers WHERE user_id=?", (user_id,))
    r = await cur.fetchone()
    return r["username"] if r else None


# ── Ordering state ───────────────────────────────────────────────────────────
async def is_ordering_open() -> bool:
    cur = await _conn.execute("SELECT value FROM meta WHERE key='ordering_open'")
    r = await cur.fetchone()
    return bool(r) and r["value"] == "1"


async def set_ordering_open(value: bool) -> None:
    async with _write_lock:
        await _conn.execute(
            "UPDATE meta SET value=? WHERE key='ordering_open'",
            ("1" if value else "0",),
        )
        await _conn.commit()


async def get_setting(key: str, default: str = "") -> str:
    cur = await _conn.execute("SELECT value FROM meta WHERE key=?", (key,))
    r = await cur.fetchone()
    return r["value"] if r and r["value"] is not None else default


async def set_setting(key: str, value: str) -> None:
    async with _write_lock:
        await _conn.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        await _conn.commit()


# ── Menu ─────────────────────────────────────────────────────────────────────
async def add_menu_item(name: str, price: int, image_path: str | None) -> int:
    async with _write_lock:
        cur = await _conn.execute("SELECT COALESCE(MAX(position), 0) + 1 AS p FROM menu_items")
        pos = (await cur.fetchone())["p"]
        cur = await _conn.execute(
            "INSERT INTO menu_items(name, price, image_path, position) VALUES (?, ?, ?, ?)",
            (name, price, image_path, pos),
        )
        await _conn.commit()
        return cur.lastrowid


async def update_menu_item(item_id: int, name: str, price: int) -> None:
    async with _write_lock:
        await _conn.execute(
            "UPDATE menu_items SET name=?, price=? WHERE id=?", (name, price, item_id)
        )
        await _conn.commit()


async def delete_menu_item(item_id: int) -> str | None:
    """Delete and return the image_path (so the caller can remove the file)."""
    async with _write_lock:
        cur = await _conn.execute("SELECT image_path FROM menu_items WHERE id=?", (item_id,))
        r = await cur.fetchone()
        await _conn.execute("DELETE FROM menu_items WHERE id=?", (item_id,))
        await _conn.commit()
        return r["image_path"] if r else None


async def get_menu_items() -> list[dict]:
    cur = await _conn.execute(
        "SELECT id, name, price, image_path FROM menu_items ORDER BY position, id"
    )
    return [dict(r) for r in await cur.fetchall()]


async def get_menu_item(item_id: int) -> dict | None:
    cur = await _conn.execute(
        "SELECT id, name, price, image_path FROM menu_items WHERE id=?", (item_id,)
    )
    r = await cur.fetchone()
    return dict(r) if r else None


async def menu_item_has_orders(item_id: int) -> bool:
    """True if this dish appears in any non-cancelled/non-rejected order."""
    placeholders = ",".join("?" * len(ACTIVE_STATUSES))
    cur = await _conn.execute(
        f"SELECT 1 FROM order_items oi JOIN orders o ON o.id = oi.order_id "
        f"WHERE oi.menu_item_id=? AND o.status IN ({placeholders}) LIMIT 1",
        (item_id, *ACTIVE_STATUSES),
    )
    return (await cur.fetchone()) is not None


async def clear_menu() -> list[str]:
    """Remove all dishes; return their image paths for file cleanup."""
    async with _write_lock:
        cur = await _conn.execute(
            "SELECT image_path FROM menu_items WHERE image_path IS NOT NULL"
        )
        paths = [r["image_path"] for r in await cur.fetchall()]
        await _conn.execute("DELETE FROM menu_items")
        await _conn.commit()
        return paths


# ── Orders ───────────────────────────────────────────────────────────────────
async def _attach_items(order: dict) -> dict:
    cur = await _conn.execute(
        "SELECT menu_item_id, name, unit_price, qty FROM order_items WHERE order_id=?",
        (order["id"],),
    )
    order["items"] = [dict(r) for r in await cur.fetchall()]
    return order


async def get_active_order(user_id: int) -> dict | None:
    placeholders = ",".join("?" * len(ACTIVE_STATUSES))
    cur = await _conn.execute(
        f"SELECT * FROM orders WHERE user_id=? AND status IN ({placeholders}) "
        "ORDER BY id DESC LIMIT 1",
        (user_id, *ACTIVE_STATUSES),
    )
    r = await cur.fetchone()
    return await _attach_items(dict(r)) if r else None


async def get_order(order_id: int) -> dict | None:
    cur = await _conn.execute("SELECT * FROM orders WHERE id=?", (order_id,))
    r = await cur.fetchone()
    return await _attach_items(dict(r)) if r else None


async def create_order(user_id: int, requested: list[tuple[int, int]]) -> dict:
    """requested = [(menu_item_id, qty), ...] with qty > 0. Snapshots name/price.

    Raises ValueError on: existing active order, empty cart, unknown/zero items.
    """
    async with _write_lock:
        placeholders = ",".join("?" * len(ACTIVE_STATUSES))
        cur = await _conn.execute(
            f"SELECT id FROM orders WHERE user_id=? AND status IN ({placeholders})",
            (user_id, *ACTIVE_STATUSES),
        )
        if await cur.fetchone():
            raise ValueError("active_order_exists")

        lines = []
        total = 0
        for item_id, qty in requested:
            if qty <= 0:
                continue
            cur = await _conn.execute(
                "SELECT name, price FROM menu_items WHERE id=?", (item_id,)
            )
            row = await cur.fetchone()
            if not row:
                raise ValueError("unknown_item")
            lines.append((item_id, row["name"], row["price"], qty))
            total += row["price"] * qty

        if not lines:
            raise ValueError("empty_cart")

        now = _now()
        cur = await _conn.execute(
            "INSERT INTO orders(user_id, status, total, created_at, updated_at) "
            "VALUES (?, 'pending_payment', ?, ?, ?)",
            (user_id, total, now, now),
        )
        order_id = cur.lastrowid
        await _conn.executemany(
            "INSERT INTO order_items(order_id, menu_item_id, name, unit_price, qty) "
            "VALUES (?, ?, ?, ?, ?)",
            [(order_id, iid, name, price, qty) for iid, name, price, qty in lines],
        )
        await _conn.commit()
    return await get_order(order_id)


async def set_order_screenshot(order_id: int, user_id: int, path: str) -> bool:
    """Attach the payment screenshot and move to awaiting_review. Owner-checked."""
    async with _write_lock:
        cur = await _conn.execute(
            "UPDATE orders SET screenshot_path=?, status='awaiting_review', updated_at=? "
            "WHERE id=? AND user_id=? AND status IN ('pending_payment','awaiting_review')",
            (path, _now(), order_id, user_id),
        )
        await _conn.commit()
        return cur.rowcount > 0


async def review_order(order_id: int, admin_id: int, accept: bool) -> dict | None:
    """Accept (→confirmed) or reject (→pending_payment) an awaiting_review order.

    Returns the updated order, or None if it wasn't awaiting review (already handled).
    """
    async with _write_lock:
        cur = await _conn.execute("SELECT status FROM orders WHERE id=?", (order_id,))
        r = await cur.fetchone()
        if not r or r["status"] != "awaiting_review":
            return None
        new_status = "confirmed" if accept else "pending_payment"
        await _conn.execute(
            "UPDATE orders SET status=?, reviewed_by=?, updated_at=? WHERE id=?",
            (new_status, admin_id, _now(), order_id),
        )
        await _conn.commit()
    return await get_order(order_id)


async def cancel_order(order_id: int, user_id: int) -> bool:
    """User cancels their own still-active order. Caller enforces the time cutoff."""
    async with _write_lock:
        placeholders = ",".join("?" * len(CANCELLABLE_STATUSES))
        cur = await _conn.execute(
            f"UPDATE orders SET status='cancelled', updated_at=? "
            f"WHERE id=? AND user_id=? AND status IN ({placeholders})",
            (_now(), order_id, user_id, *CANCELLABLE_STATUSES),
        )
        await _conn.commit()
        return cur.rowcount > 0


async def get_all_orders() -> list[dict]:
    cur = await _conn.execute("SELECT * FROM orders ORDER BY id DESC")
    orders = [await _attach_items(dict(r)) for r in await cur.fetchall()]
    for o in orders:
        o["name"] = await get_name(o["user_id"])
    return orders


async def report_summary() -> dict:
    """Aggregate confirmed orders per dish for the daily admin report."""
    cur = await _conn.execute(
        "SELECT oi.name AS name, SUM(oi.qty) AS qty, SUM(oi.qty * oi.unit_price) AS subtotal "
        "FROM order_items oi JOIN orders o ON o.id = oi.order_id "
        "WHERE o.status='confirmed' GROUP BY oi.name ORDER BY qty DESC"
    )
    dishes = [dict(r) for r in await cur.fetchall()]
    cur = await _conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(total), 0) AS money "
        "FROM orders WHERE status='confirmed'"
    )
    totals = await cur.fetchone()
    cur = await _conn.execute(
        "SELECT COUNT(*) AS n FROM orders WHERE status IN ('pending_payment','awaiting_review')"
    )
    unpaid = (await cur.fetchone())["n"]
    return {
        "dishes": dishes,
        "order_count": totals["n"],
        "money": totals["money"],
        "unpaid": unpaid,
    }


async def reset_orders() -> list[str]:
    """Clear all orders (new day). Returns screenshot paths for file cleanup."""
    async with _write_lock:
        cur = await _conn.execute(
            "SELECT screenshot_path FROM orders WHERE screenshot_path IS NOT NULL"
        )
        paths = [r["screenshot_path"] for r in await cur.fetchall()]
        await _conn.execute("DELETE FROM order_items")
        await _conn.execute("DELETE FROM orders")
        # Restart order numbering (#1, #2, …) for the new day / refreshed menu.
        await _conn.execute("DELETE FROM sqlite_sequence WHERE name='orders'")
        await _conn.commit()
        return paths
