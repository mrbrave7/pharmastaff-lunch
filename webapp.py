"""FastAPI routes for the ordering Mini App.

Auth: every request carries Telegram's WebApp `initData` in the
`X-Telegram-Init-Data` header; we verify its HMAC against the bot token. Admin
endpoints additionally require the user's id to be in config.ADMIN_IDS.

Menu photos are saved under static/uploads (served publicly). Payment screenshots
are saved under data/screenshots (NOT served) and only sent to admins via Telegram.
"""
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from urllib.parse import parse_qsl

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

import bot as botmod
import config
import db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")

_MAX_AUTH_AGE = 24 * 60 * 60
_IMG_EXTS = (".jpg", ".jpeg", ".png", ".webp")


# ── Auth ─────────────────────────────────────────────────────────────────────
def validate_init_data(init_data: str, max_age: int = _MAX_AUTH_AGE) -> dict | None:
    if not init_data:
        return None
    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        return None
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", config.BOT_TOKEN.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        return None
    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError:
        return None
    if max_age and auth_date and (time.time() - auth_date) > max_age:
        return None
    try:
        return json.loads(pairs["user"])
    except (KeyError, json.JSONDecodeError):
        return None


def _display_name(user: dict) -> str:
    name = (user.get("first_name") or "").strip()
    if user.get("last_name"):
        name = f"{name} {user['last_name']}".strip()
    return name or str(user["id"])


def _is_admin(user: dict) -> bool:
    return int(user["id"]) in config.ADMIN_IDS


async def _require_user(request: Request) -> dict:
    user = validate_init_data(request.headers.get("X-Telegram-Init-Data", ""))
    if not user or "id" not in user:
        raise HTTPException(status_code=401, detail="Invalid or missing Telegram auth")
    await db.add_subscriber(int(user["id"]), _display_name(user), user.get("username"))
    return user


async def _require_admin(request: Request) -> dict:
    user = await _require_user(request)
    if not _is_admin(user):
        raise HTTPException(status_code=403, detail="Admin only")
    return user


def _bot(request: Request):
    return request.app.state.application.bot


# ── Serializers ──────────────────────────────────────────────────────────────
def _menu_url(image_path: str | None) -> str | None:
    return f"{config.MENU_IMAGE_URL_PREFIX}/{image_path}" if image_path else None


def _menu_out(item: dict) -> dict:
    return {
        "id": item["id"],
        "name": item["name"],
        "price": item["price"],
        "image_url": _menu_url(item["image_path"]),
    }


def _order_out(order: dict | None) -> dict | None:
    if not order:
        return None
    return {
        "id": order["id"],
        "status": order["status"],
        "total": order["total"],
        "items": [
            {"name": it["name"], "qty": it["qty"], "unit_price": it["unit_price"]}
            for it in order["items"]
        ],
        # Confirmed orders are locked; cancel is only allowed before confirmation
        # and before the daily cutoff hour.
        "can_cancel": order["status"] in db.CANCELLABLE_STATUSES
        and config.cancellation_allowed(),
    }


# ── File helpers ─────────────────────────────────────────────────────────────
def _ext(filename: str | None) -> str:
    ext = os.path.splitext(filename or "")[1].lower()
    return ext if ext in _IMG_EXTS else ".jpg"


async def _save(upload: UploadFile, directory: str, prefix: str = "") -> str:
    data = await upload.read()
    if len(data) > 12 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Image too large (max 12MB)")
    fname = f"{prefix}{uuid.uuid4().hex}{_ext(upload.filename)}"
    dest = os.path.join(directory, fname)
    with open(dest, "wb") as f:
        f.write(data)
    return fname


# ── Public / customer ────────────────────────────────────────────────────────
@router.get("/config")
async def get_config(request: Request):
    user = await _require_user(request)
    return {
        "is_admin": _is_admin(user),
        "ordering_open": await db.is_ordering_open(),
        "card_number": await db.get_setting("card_number", config.CARD_NUMBER),
        "card_holder": await db.get_setting("card_holder", config.CARD_HOLDER),
        "currency": config.CURRENCY,
        "cutoff_hour": config.ORDER_CUTOFF_HOUR,
        "cancellation_allowed": config.cancellation_allowed(),
    }


@router.get("/menu")
async def get_menu(request: Request):
    await _require_user(request)
    return [_menu_out(i) for i in await db.get_menu_items()]


@router.get("/my-order")
async def my_order(request: Request):
    user = await _require_user(request)
    return _order_out(await db.get_active_order(int(user["id"])))


@router.post("/order")
async def create_order(request: Request):
    user = await _require_user(request)
    if not await db.is_ordering_open():
        raise HTTPException(status_code=403, detail="ordering_closed")
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    items = body.get("items")
    if not isinstance(items, list) or not items:
        raise HTTPException(status_code=400, detail="empty_cart")
    requested = []
    for it in items:
        try:
            requested.append((int(it["id"]), int(it["qty"])))
        except (KeyError, TypeError, ValueError):
            raise HTTPException(status_code=400, detail="bad_item")
    try:
        order = await db.create_order(int(user["id"]), requested)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return _order_out(order)


@router.post("/order/{order_id}/payment")
async def upload_payment(order_id: int, request: Request, file: UploadFile = File(...)):
    user = await _require_user(request)
    order = await db.get_order(order_id)
    if not order or order["user_id"] != int(user["id"]):
        raise HTTPException(status_code=404, detail="order_not_found")

    path = os.path.join(config.SCREENSHOT_DIR, await _save(file, config.SCREENSHOT_DIR, f"{order_id}_"))
    if not await db.set_order_screenshot(order_id, int(user["id"]), path):
        raise HTTPException(status_code=409, detail="cannot_attach")

    fresh = await db.get_order(order_id)
    await botmod.notify_admins_new_payment(_bot(request), fresh)
    return _order_out(fresh)


@router.post("/order/{order_id}/cancel")
async def cancel_order(order_id: int, request: Request):
    user = await _require_user(request)
    if not config.cancellation_allowed():
        raise HTTPException(status_code=403, detail="cutoff_passed")
    if not await db.cancel_order(order_id, int(user["id"])):
        raise HTTPException(status_code=409, detail="cannot_cancel")
    return {"ok": True}


# ── Admin ────────────────────────────────────────────────────────────────────
@router.post("/admin/menu")
async def admin_add_menu(
    request: Request,
    name: str = Form(...),
    price: int = Form(...),
    file: UploadFile = File(...),
):
    await _require_admin(request)
    name = name.strip()
    if not name or price < 0:
        raise HTTPException(status_code=400, detail="bad_input")
    fname = await _save(file, config.MENU_IMAGE_DIR)
    item_id = await db.add_menu_item(name, price, fname)
    return _menu_out(await db.get_menu_item(item_id))


@router.put("/admin/menu/{item_id}")
async def admin_edit_menu(item_id: int, request: Request):
    await _require_admin(request)
    body = await request.json()
    name = str(body.get("name", "")).strip()
    try:
        price = int(body.get("price"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="bad_price")
    if not name or price < 0:
        raise HTTPException(status_code=400, detail="bad_input")
    if not await db.get_menu_item(item_id):
        raise HTTPException(status_code=404, detail="not_found")
    await db.update_menu_item(item_id, name, price)
    return _menu_out(await db.get_menu_item(item_id))


@router.delete("/admin/menu/{item_id}")
async def admin_delete_menu(item_id: int, request: Request):
    await _require_admin(request)
    if not await db.get_menu_item(item_id):
        raise HTTPException(status_code=404, detail="not_found")
    if await db.menu_item_has_orders(item_id):
        raise HTTPException(status_code=409, detail="has_orders")
    fname = await db.delete_menu_item(item_id)
    if fname:
        _safe_unlink(os.path.join(config.MENU_IMAGE_DIR, fname))
    return {"ok": True}


@router.post("/admin/ordering")
async def admin_set_ordering(request: Request):
    await _require_admin(request)
    body = await request.json()
    await db.set_ordering_open(bool(body.get("open")))
    return {"ordering_open": await db.is_ordering_open()}


@router.post("/admin/card")
async def admin_set_card(request: Request):
    await _require_admin(request)
    body = await request.json()
    card = str(body.get("card_number", "")).strip()
    holder = str(body.get("card_holder", "")).strip()
    if not card:
        raise HTTPException(status_code=400, detail="empty_card")
    await db.set_setting("card_number", card)
    await db.set_setting("card_holder", holder)
    return {"card_number": card, "card_holder": holder}


@router.get("/admin/orders")
async def admin_orders(request: Request):
    await _require_admin(request)
    out = []
    for o in await db.get_all_orders():
        out.append({
            "id": o["id"],
            "name": o["name"],
            "status": o["status"],
            "total": o["total"],
            "items": [{"name": it["name"], "qty": it["qty"]} for it in o["items"]],
        })
    return out


@router.get("/admin/report")
async def admin_report(request: Request):
    """Confirmed-orders summary (per dish + totals) — same data as the 3 PM report."""
    await _require_admin(request)
    return await db.report_summary()


@router.post("/admin/order/{order_id}/review")
async def admin_review(order_id: int, request: Request):
    admin = await _require_admin(request)
    body = await request.json()
    accept = bool(body.get("accept"))
    order = await db.review_order(order_id, int(admin["id"]), accept)
    if order is None:
        raise HTTPException(status_code=409, detail="not_reviewable")
    await botmod.notify_user_reviewed(_bot(request), order["user_id"], accept)
    return {"ok": True, "status": order["status"]}


@router.post("/admin/reset")
async def admin_reset(request: Request):
    await _require_admin(request)
    for path in await db.reset_orders():
        _safe_unlink(path)
    for fname in await db.clear_menu():
        _safe_unlink(os.path.join(config.MENU_IMAGE_DIR, fname))
    await db.set_ordering_open(False)
    return {"ok": True}


def _safe_unlink(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass
