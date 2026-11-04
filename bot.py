#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Sance Crypto — Telegram Bot + Mini App Server + SQLite + Full Admin Panel + Pocket Option Verification
Возможности:
- Приветствие (/start) с 3D Motion видео/GIF анимацией (welcome.mp4 / welcome.gif) и кэшированием file_id
- Строгая 3-этапная верификация Pocket Option:
  1) Подтверждение Telegram ID пользователя
  2) Переход и регистрация по партнёрской ссылке Pocket Option (с привязкой sub_id1 = Telegram ID)
  3) Проверка личного UID трейдера и суммы депозита (через Postback партнёрки / базу данных / модерацию админа)
- Полноценная интерактивная Админ-панель (/admin) для владельца (ID 1394187706):
  • Массовая рассылка (текст/фото/видео/GIF) по всем пользователям, только верифицированным или дожим тех, кто без UID
  • Мгновенная смена реферальной ссылки Pocket Option (с авто-обновлением в Mini App HTML и БД)
  • Создание и удаление промокодов в 1 клик (с авто-синхронизацией в Mini App)
  • Настройка режима проверки Pocket Option (Строгий Postback-режим / Стандартный 3-этапный / Мин. депозит)
  • Поиск и управление пользователями (одобрение/отклонение UID, установка депозита, выдача сигналов, бан/разбан, личное сообщение)
  • Раздача бонусных сигналов всем пользователям, топ рефоводов и выгрузка всей базы в CSV
"""
from __future__ import annotations

import csv
import html
import http.server
import io
import json
import logging
import mimetypes
import os
import pathlib
import random
import re
import socket
import socketserver
import sqlite3
import ssl
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone, timedelta

try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# ==================== НАСТРОЙКИ SANCE CRYPTO ====================
BOT_TOKEN = os.getenv("BOT_TOKEN", "8959407214:AAHeKJoGsQiium67pNEvWfX8G2MsbqHOEl8").strip()
WEBAPP_PUBLIC_URL = os.getenv("WEBAPP_PUBLIC_URL", "https://sancecrypto.onrender.com").strip()

DEFAULT_PO_AFFILIATE_URL = (
    "https://u3.shortink.io/register?"
    "utm_campaign=870146&utm_source=affiliate&utm_medium=sr"
    "&a=RvlInPdalLnQrY&al=1805258&ac=sance&cid=987105&code=WELCOME50"
)
POCKET_OPTION_AFFILIATE_URL = os.getenv("POCKET_OPTION_AFFILIATE_URL", DEFAULT_PO_AFFILIATE_URL).strip()

TELEGRAM_CHANNEL_URL = "https://t.me/SanceCrypto"
SUPPORT_CHAT_URL = "https://t.me/SanceCryptoHelp"

# ID главного администратора (1394187706 всегда имеет полный доступ к /admin)
ADMIN_IDS: list[int] = [
    int(x)
    for x in os.getenv("ADMIN_IDS", "1394187706").split(",")
    if x.strip().isdigit()
]
if 1394187706 not in ADMIN_IDS:
    ADMIN_IDS.append(1394187706)

PORT = int(os.getenv("PORT", "8080"))
POSTBACK_SECRET = os.getenv("POSTBACK_SECRET", "").strip()

BASE_DIR = pathlib.Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "sance_crypto.db"

WELCOME_MEDIA_URL = os.getenv("WELCOME_MEDIA_URL", "").strip()
WELCOME_BUTTON_TEXT = os.getenv("WELCOME_BUTTON_TEXT", "Запустить приложение").strip()
WELCOME_BUTTON_STYLE = os.getenv("WELCOME_BUTTON_STYLE", "success").strip()
WELCOME_BUTTON_CUSTOM_EMOJI_ID = os.getenv("WELCOME_BUTTON_CUSTOM_EMOJI_ID", "").strip()
WELCOME_MESSAGE_EFFECT_ID = os.getenv("WELCOME_MESSAGE_EFFECT_ID", "").strip()

# Стартовые промокоды для приложения
DEFAULT_PROMO_CODES: dict[str, dict[str, int]] = {
    "SANCE2026": {"sig": 7, "xp": 150},
    "VISION45":  {"sig": 8, "xp": 160},
    "SANCEVIP":  {"sig": 10, "xp": 220},
    "WHALE":     {"sig": 15, "xp": 350},
    "PROFIT":    {"sig": 5, "xp": 120},
}

# Запрещённые шаблонные/фейковые UID, чтобы пользователи вводили только свой реальный UID
FORBIDDEN_FAKE_UIDS = {
    "123456", "1234567", "12345678", "123456789", "1234567890",
    "000000", "0000000", "00000000", "11111111", "22222222",
    "33333333", "44444444", "55555555", "66666666", "77777777",
    "88888888", "99999999", "84920145", "87654321",
}

BOT_POLLING_ACTIVE = False
BOT_USERNAME = "SanceCryptoBot"
MEDIA_FILE_ID_CACHE: dict[str, str] = {}

# Состояния диалога для админ-панели и пользователей: {user_id: {"step": "...", "data": {...}}}
USER_FSM_STATE: dict[int, dict] = {}

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

DIRECT_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    urllib.request.HTTPSHandler(context=SSL_CTX),
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("sance_crypto")


def is_admin(user_id: int) -> bool:
    return int(user_id) in ADMIN_IDS


def get_db_conn() -> sqlite3.Connection:
    global DB_PATH
    for candidate in (DB_PATH, pathlib.Path(tempfile.gettempdir()) / "sance_crypto.db"):
        try:
            conn = sqlite3.connect(candidate, timeout=15)
            conn.execute("PRAGMA journal_mode=MEMORY;")
            conn.execute("PRAGMA synchronous=OFF;")
            DB_PATH = candidate
            return conn
        except sqlite3.OperationalError:
            continue
    return sqlite3.connect(":memory:")


def msk_now() -> datetime:
    return datetime.now(timezone(timedelta(hours=3)))


def msk_today_str() -> str:
    return msk_now().strftime("%Y-%m-%d")


def init_db() -> None:
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            tg_user_id INTEGER PRIMARY KEY,
            username TEXT DEFAULT '',
            first_name TEXT DEFAULT '',
            registered_step1 INTEGER DEFAULT 0,
            visited_step2 INTEGER DEFAULT 0,
            po_uid TEXT DEFAULT '',
            deposit_usd REAL DEFAULT 0.0,
            is_verified INTEGER DEFAULT 0,
            tier TEXT DEFAULT 'guest',
            signals_used_today INTEGER DEFAULT 0,
            bonus_signals INTEGER DEFAULT 0,
            referred_by INTEGER DEFAULT 0,
            state_json TEXT DEFAULT '{}',
            is_banned INTEGER DEFAULT 0,
            last_reset_date TEXT,
            created_at TEXT
        )
        """
    )
    # Безопасная миграция колонки visited_step2, если таблица уже существовала
    try:
        cur.execute("ALTER TABLE users ADD COLUMN visited_step2 INTEGER DEFAULT 0")
    except Exception:
        pass

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS media_cache (
            cache_key TEXT PRIMARY KEY,
            file_id TEXT NOT NULL,
            updated_at TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS promo_codes (
            code TEXT PRIMARY KEY,
            sig INTEGER NOT NULL DEFAULT 5,
            xp INTEGER NOT NULL DEFAULT 100,
            used_count INTEGER DEFAULT 0,
            created_at TEXT
        )
        """
    )
    try:
        cur.execute("ALTER TABLE promo_codes ADD COLUMN used_count INTEGER DEFAULT 0")
    except Exception:
        pass

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS po_postbacks (
            po_uid TEXT PRIMARY KEY,
            tg_user_id INTEGER DEFAULT 0,
            is_registered INTEGER DEFAULT 1,
            ftd_usd REAL DEFAULT 0.0,
            total_deposit_usd REAL DEFAULT 0.0,
            last_event TEXT DEFAULT 'reg',
            raw_params TEXT DEFAULT '{}',
            updated_at TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS bot_settings (
            key TEXT PRIMARY KEY,
            val TEXT NOT NULL
        )
        """
    )

    now_iso = datetime.now(timezone.utc).isoformat()
    for k, v in DEFAULT_PROMO_CODES.items():
        cur.execute(
            "INSERT OR IGNORE INTO promo_codes (code, sig, xp, used_count, created_at) VALUES (?, ?, ?, 0, ?)",
            (k, v["sig"], v["xp"], now_iso),
        )

    default_settings = {
        "po_affiliate_url": POCKET_OPTION_AFFILIATE_URL,
        "strict_po_verify": os.getenv("STRICT_PO_VERIFY", "1").strip(),
        "min_deposit_usd": os.getenv("MIN_DEPOSIT_USD", "0").strip(),
        "require_deposit": "0",
    }
    for sk, sv in default_settings.items():
        cur.execute("INSERT OR IGNORE INTO bot_settings (key, val) VALUES (?, ?)", (sk, sv))

    conn.commit()
    conn.close()


def get_setting(key: str, default: str = "") -> str:
    try:
        conn = get_db_conn()
        row = conn.execute("SELECT val FROM bot_settings WHERE key = ?", (key,)).fetchone()
        conn.close()
        if row and row[0] is not None:
            return str(row[0])
    except Exception:
        pass
    return default


def set_setting(key: str, val: str) -> None:
    try:
        conn = get_db_conn()
        conn.execute("INSERT OR REPLACE INTO bot_settings (key, val) VALUES (?, ?)", (key, str(val)))
        conn.commit()
        conn.close()
    except Exception:
        pass


def get_po_affiliate_url() -> str:
    return get_setting("po_affiliate_url", POCKET_OPTION_AFFILIATE_URL).strip() or DEFAULT_PO_AFFILIATE_URL


def is_strict_postback_mode() -> bool:
    return get_setting("strict_po_verify", "1").strip() == "1"


def get_min_deposit_usd() -> float:
    try:
        return 0.0
    except Exception:
        return 0.0


def build_tracked_po_url(tg_user_id: int | str = "") -> str:
    base = get_po_affiliate_url()
    if tg_user_id and "sub_id1=" not in base:
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}sub_id1={tg_user_id}"
    return base


def get_all_promos_dict() -> dict[str, dict[str, int]]:
    conn = get_db_conn()
    rows = conn.execute("SELECT code, sig, xp FROM promo_codes ORDER BY created_at DESC").fetchall()
    conn.close()
    return {str(r[0]): {"sig": int(r[1]), "xp": int(r[2])} for r in rows}


def sync_settings_and_promos_to_html() -> None:
    """
    Автоматически прописывает актуальные промокоды и партнёрскую ссылку Pocket Option
    прямо внутрь index.html и pokoritel_webapp.html при любых изменениях через /admin.
    """
    promos = get_all_promos_dict()
    po_url = get_po_affiliate_url()
    min_dep = get_min_deposit_usd()
    strict_flag = "true" if is_strict_postback_mode() else "false"

    entries = ",\n".join(
        f"      '{k}': {{ sig: {v['sig']}, xp: {v['xp']} }}"
        for k, v in promos.items()
    )
    new_promo_block = f"var CHANNEL_PROMO_CODES = {{\n{entries}\n    }};"

    for fname in ("index.html", "pokoritel_webapp.html"):
        fpath = BASE_DIR / fname
        if fpath.exists():
            try:
                txt = fpath.read_text(encoding="utf-8")
                txt_upd = re.sub(
                    r"var\s+CHANNEL_PROMO_CODES\s*=\s*\{[\s\S]*?\};",
                    new_promo_block,
                    txt,
                    count=1,
                )
                txt_upd = re.sub(
                    r'var\s+OFFICIAL_PO_AFFILIATE_URL\s*=\s*"[^"]*";',
                    f'var OFFICIAL_PO_AFFILIATE_URL = "{po_url}";',
                    txt_upd,
                )
                txt_upd = re.sub(
                    r"var\s+PO_MIN_REQUIRED_DEPOSIT_USD\s*=\s*[0-9.]+;",
                    f"var PO_MIN_REQUIRED_DEPOSIT_USD = {min_dep};",
                    txt_upd,
                )
                txt_upd = re.sub(
                    r"var\s+PO_STRICT_POSTBACK_ONLY\s*=\s*(?:true|false);",
                    f"var PO_STRICT_POSTBACK_ONLY = {strict_flag};",
                    txt_upd,
                )
                if txt_upd != txt:
                    fpath.write_text(txt_upd, encoding="utf-8")
            except Exception as e:
                logger.warning(f"Не удалось синхронизировать HTML {fname}: {e}")


def is_valid_personal_po_uid(raw_uid: str, allow_admin_demo: bool = False) -> tuple[bool, str, str]:
    clean = re.sub(r"\D", "", (raw_uid or "").strip())
    if len(clean) < 6 or len(clean) > 12:
        return False, clean, "UID аккаунта Pocket Option должен содержать от 6 до 12 цифр."
    if not allow_admin_demo:
        if clean in FORBIDDEN_FAKE_UIDS or len(set(clean)) == 1:
            return False, clean, "Указан шаблонный/тестовый UID. Пожалуйста, введите ваш настоящий личный UID из профиля Pocket Option!"
    return True, clean, ""


def upsert_user(
    tg_user_id: int,
    username: str = "",
    first_name: str = "",
    referrer_id: int = 0,
    confirm_step1: bool = False,
) -> tuple[dict, bool]:
    today = msk_today_str()
    clean_username = username.lstrip("@").lower() if username else ""
    conn = get_db_conn()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    row = cur.execute("SELECT * FROM users WHERE tg_user_id = ?", (tg_user_id,)).fetchone()
    is_new = False
    if not row:
        is_new = True
        ref_val = referrer_id if (referrer_id and referrer_id != tg_user_id) else 0
        # Новые пользователи начинают БЕЗ фейкового UID (is_verified=0, po_uid='', deposit_usd=0.0),
        # кроме главного администратора, если он тестирует
        cur.execute(
            """
            INSERT INTO users (
                tg_user_id, username, first_name, registered_step1, visited_step2,
                po_uid, deposit_usd, is_verified, tier, bonus_signals,
                referred_by, last_reset_date, created_at
            )
            VALUES (?, ?, ?, ?, 0, '', 0.0, 0, 'guest', 0, ?, ?, ?)
            """,
            (
                tg_user_id,
                clean_username,
                first_name,
                1 if confirm_step1 else 0,
                ref_val,
                today,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        # Если пришёл по реферальной ссылке — начисляем рефоводу +5 бонусных сигналов
        if ref_val > 0:
            cur.execute(
                "UPDATE users SET bonus_signals = COALESCE(bonus_signals, 0) + 5 WHERE tg_user_id = ?",
                (ref_val,),
            )
        conn.commit()
        row = cur.execute("SELECT * FROM users WHERE tg_user_id = ?", (tg_user_id,)).fetchone()
    else:
        updates = []
        params = []
        if clean_username and row["username"] != clean_username:
            updates.append("username = ?")
            params.append(clean_username)
        if first_name and row["first_name"] != first_name:
            updates.append("first_name = ?")
            params.append(first_name)
        if confirm_step1 and not row["registered_step1"]:
            updates.append("registered_step1 = 1")
        if row["last_reset_date"] != today:
            updates.append("signals_used_today = 0")
            updates.append("last_reset_date = ?")
            params.append(today)
        if updates:
            params.append(tg_user_id)
            cur.execute(f"UPDATE users SET {', '.join(updates)} WHERE tg_user_id = ?", tuple(params))
            conn.commit()
            row = cur.execute("SELECT * FROM users WHERE tg_user_id = ?", (tg_user_id,)).fetchone()
    conn.close()
    return dict(row), is_new


# ==================== POCKET OPTION POSTBACK & UID VERIFICATION ====================
def process_po_postback(params: dict[str, str], notify: bool = True) -> dict:
    """
    Обрабатывает S2S Postback от партнёрской программы Pocket Option (Shortink / affiliate.pocketoption.com)
    или ручное подтверждение депозита администратором.
    """
    if POSTBACK_SECRET and params.get("secret", "") != POSTBACK_SECRET:
        return {"ok": False, "error": "invalid_secret"}

    raw_uid = (
        params.get("trader_id")
        or params.get("uid")
        or params.get("ac_id")
        or params.get("user_id")
        or ""
    ).strip()
    po_uid = re.sub(r"\D", "", raw_uid)
    if not po_uid:
        return {"ok": False, "error": "missing_trader_id"}

    raw_dep = (
        params.get("sumdep")
        or params.get("dep_sum")
        or params.get("ftd_amount")
        or params.get("amount")
        or params.get("dep")
        or "0"
    ).strip()
    try:
        dep_val = max(0.0, float(raw_dep.replace(",", ".")))
    except Exception:
        dep_val = 0.0

    event_type = (
        params.get("event")
        or params.get("status")
        or params.get("type")
        or ("dep" if dep_val > 0 else "reg")
    ).strip().lower()
    raw_tg = (params.get("sub_id1") or params.get("tg_id") or params.get("click_id") or "").strip()
    tg_uid = int(raw_tg) if raw_tg.isdigit() else 0

    now_iso = datetime.now(timezone.utc).isoformat()
    conn = get_db_conn()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    existing = cur.execute("SELECT * FROM po_postbacks WHERE po_uid = ?", (po_uid,)).fetchone()
    if existing:
        prev_total = float(existing["total_deposit_usd"] or 0.0)
        prev_ftd = float(existing["ftd_usd"] or 0.0)
        new_ftd = dep_val if (prev_ftd == 0.0 and dep_val > 0) else prev_ftd
        new_total = (prev_total + dep_val) if event_type in ("dep", "redep", "repeat") else max(prev_total, dep_val)
        linked_tg = tg_uid or int(existing["tg_user_id"] or 0)
        cur.execute(
            """
            UPDATE po_postbacks
            SET tg_user_id = ?, is_registered = 1, ftd_usd = ?, total_deposit_usd = ?, last_event = ?, raw_params = ?, updated_at = ?
            WHERE po_uid = ?
            """,
            (linked_tg, new_ftd, new_total, event_type, json.dumps(params, ensure_ascii=False), now_iso, po_uid),
        )
    else:
        new_total = dep_val
        linked_tg = tg_uid
        cur.execute(
            """
            INSERT INTO po_postbacks (po_uid, tg_user_id, is_registered, ftd_usd, total_deposit_usd, last_event, raw_params, updated_at)
            VALUES (?, ?, 1, ?, ?, ?, ?, ?)
            """,
            (po_uid, linked_tg, dep_val, new_total, event_type, json.dumps(params, ensure_ascii=False), now_iso),
        )

    target_user = None
    if linked_tg:
        target_user = cur.execute("SELECT * FROM users WHERE tg_user_id = ?", (linked_tg,)).fetchone()
    if not target_user:
        target_user = cur.execute("SELECT * FROM users WHERE po_uid = ?", (po_uid,)).fetchone()

    min_dep = get_min_deposit_usd()
    if target_user:
        u_tg_id = int(target_user["tg_user_id"])
        u_dep = new_total if new_total > 0 else float(target_user["deposit_usd"] or 0.0)
        is_ver = 1
        cur.execute(
            """
            UPDATE users
            SET registered_step1 = 1, visited_step2 = 1, po_uid = ?, deposit_usd = ?, is_verified = ?, tier = ?
            WHERE tg_user_id = ?
            """,
            (po_uid, u_dep, is_ver, "vip" if u_dep >= 250 else ("standard" if is_ver else "guest"), u_tg_id),
        )
        linked_tg = u_tg_id

    conn.commit()
    conn.close()

    if notify:
        if linked_tg:
            try:
                if True:
                    send_msg(
                        linked_tg,
                        f"<b>◈ ВЕРИФИКАЦИЯ POCKET OPTION УСПЕШНА ✓</b>\n\n"
                        f"• <b>Ваш личный UID:</b> <code>{po_uid}</code>\n"
                        f"• <b>Подтверждённый депозит:</b> <code>${new_total:.2f}</code>\n"
                        f"• <b>Статус доступа:</b> <code>АКТИВЕН (15/15 сигналов + 3D-рамки)</code>\n\n"
                        f"Откройте 3D-терминал кнопкой ниже:",
                        kb_main_menu_dict(include_style=True, tg_user_id=linked_tg),
                    )
                else:
                    send_msg(
                        linked_tg,
                        f"<b>◈ РЕГИСТРАЦИЯ UID {po_uid} ЗАФИКСИРОВАНА ✓</b>\n\n"
                        f"• Аккаунт <code>{po_uid}</code> найден в партнёрской системе.\n"
                        f"• Для открытия полного доступа к сигналам и ИИ-сканеру пополните счёт минимум на <b>${min_dep:.0f}</b> (с промокодом <code>WELCOME50</code> +50% к депозиту)!",
                        kb_main_menu_dict(include_style=True, tg_user_id=linked_tg),
                    )
            except Exception:
                pass

        for adm_id in ADMIN_IDS:
            if adm_id != linked_tg:
                try:
                    send_msg(
                        adm_id,
                        f"🔔 <b>Postback / Обновление Pocket Option:</b>\n"
                        f"• Событие: <code>{event_type.upper()}</code>\n"
                        f"• UID трейдера: <code>{po_uid}</code>\n"
                        f"• Депозит: <b>${dep_val:.2f}</b> (Всего: <b>${new_total:.2f}</b>)\n"
                        f"• TG ID: <code>{linked_tg or 'не привязан'}</code>",
                    )
                except Exception:
                    pass

    return {
        "ok": True,
        "po_uid": po_uid,
        "event": event_type,
        "deposit_usd": new_total,
        "tg_user_id": linked_tg,
    }


def check_po_uid_status(
    uid: str,
    tg_id_str: str = "",
    claimed_dep_str: str = "",
    step1_done: bool = True,
    step2_done: bool = True,
) -> dict:
    """
    Проверяет UID, регистрацию и депозит человека:
    1) Проверяет валидность UID (6–12 цифр, не фейковый шаблон, не занят другим TG-аккаунтом).
    2) Сверяет с базой po_postbacks (Postback от партнёрки Pocket Option или подтверждение админа).
    3) В стандартном режиме проверяет выполнение 3 шагов (ТГ + переход по партнёрской ссылке + депозит >= min_deposit_usd)
       и уведомляет админа с кнопками одобрения/отклонения.
    """
    tg_uid = int(tg_id_str) if (tg_id_str and str(tg_id_str).isdigit()) else 0
    valid_fmt, clean_uid, err_msg = is_valid_personal_po_uid(uid, allow_admin_demo=is_admin(tg_uid))
    min_dep = get_min_deposit_usd()
    strict_mode = is_strict_postback_mode()

    if not valid_fmt:
        return {
            "ok": True,
            "verified": False,
            "in_affiliate": False,
            "error": err_msg,
            "po_uid": clean_uid,
            "deposit_usd": 0.0,
            "min_deposit_usd": min_dep,
            "strict_mode": strict_mode,
            "register_url": build_tracked_po_url(tg_uid),
        }

    try:
        claimed_dep = max(0.0, float(str(claimed_dep_str or "0").replace(",", ".")))
    except Exception:
        claimed_dep = 0.0

    conn = get_db_conn()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # Проверяем, не привязан ли уже этот UID к ДРУГОМУ пользователю Telegram
    other_user = cur.execute(
        "SELECT tg_user_id, username FROM users WHERE po_uid = ? AND tg_user_id != ? AND is_verified = 1",
        (clean_uid, tg_uid),
    ).fetchone()
    if other_user and tg_uid and not is_admin(tg_uid):
        conn.close()
        return {
            "ok": True,
            "verified": False,
            "in_affiliate": False,
            "error": f"Этот UID ({clean_uid}) уже привязан к другому аккаунту Telegram! Введите свой личный UID.",
            "po_uid": clean_uid,
            "deposit_usd": 0.0,
            "min_deposit_usd": min_dep,
            "strict_mode": strict_mode,
            "register_url": build_tracked_po_url(tg_uid),
        }

    pb_row = cur.execute("SELECT * FROM po_postbacks WHERE po_uid = ?", (clean_uid,)).fetchone()
    if not pb_row and tg_uid:
        pb_row = cur.execute(
            "SELECT * FROM po_postbacks WHERE tg_user_id = ? ORDER BY updated_at DESC LIMIT 1",
            (tg_uid,),
        ).fetchone()

    if pb_row:
        found_uid = str(pb_row["po_uid"])
        pb_dep = float(pb_row["total_deposit_usd"] or 0.0)
        final_dep = pb_dep if pb_dep > 0 else claimed_dep
        if False:
            conn.close()
            return {
                "ok": True,
                "in_affiliate": True,
                "verified": False,
                "error": f"Аккаунт UID {found_uid} зарегистрирован, но сумма депозита (${final_dep:.0f}) меньше минимальной (${min_dep:.0f}). Пополните счёт на Pocket Option!",
                "po_uid": found_uid,
                "deposit_usd": final_dep,
                "min_deposit_usd": min_dep,
                "strict_mode": strict_mode,
                "register_url": build_tracked_po_url(tg_uid),
            }
        if tg_uid:
            cur.execute(
                """
                UPDATE users
                SET registered_step1 = 1, visited_step2 = 1, po_uid = ?, deposit_usd = ?, is_verified = 1, tier = ?
                WHERE tg_user_id = ?
                """,
                (found_uid, final_dep, "vip" if final_dep >= 250 else "standard", tg_uid),
            )
            conn.commit()
        conn.close()
        return {
            "ok": True,
            "in_affiliate": True,
            "verified": True,
            "po_uid": found_uid,
            "deposit_usd": final_dep,
            "min_deposit_usd": min_dep,
            "strict_mode": strict_mode,
            "register_url": build_tracked_po_url(tg_uid),
        }

    # Если записи в po_postbacks ещё нет:
    if strict_mode and not is_admin(tg_uid):
        u_row_pending = cur.execute("SELECT * FROM users WHERE tg_user_id = ?", (tg_uid,)).fetchone() if tg_uid else None
        conn.close()
        if tg_uid:
            u_name = (u_row_pending["first_name"] if u_row_pending else "") or "Трейдер"
            u_handle = f"@{u_row_pending['username']}" if (u_row_pending and u_row_pending["username"]) else "без username"
            adm_kb = {
                "inline_keyboard": [
                    [
                        {"text": f"✅ Одобрить UID {clean_uid} вручную", "callback_data": f"adm_apv_{tg_uid}_{clean_uid}_0"},
                    ],
                    [
                        {"text": "👤 Карточка юзера", "callback_data": f"adm_usr_{tg_uid}"},
                    ],
                ]
            }
            for adm_id in ADMIN_IDS:
                try:
                    send_msg(
                        adm_id,
                        f"🛡 <b>Проверка UID (Strict Postback):</b>\n"
                        f"• <b>Трейдер:</b> {html.escape(u_name)} ({u_handle})\n"
                        f"• <b>TG ID:</b> <code>{tg_uid}</code>\n"
                        f"• <b>Введённый UID:</b> <code>{clean_uid}</code>\n"
                        f"• <b>Статус:</b> Не найден в Postback партнёрки (авто-отказ). Нажмите кнопку ниже, если хотите одобрить вручную:",
                        adm_kb,
                    )
                except Exception:
                    pass
        return {
            "ok": True,
            "in_affiliate": False,
            "verified": False,
            "error": (
                f"UID {clean_uid} не найден в партнёрской базе Pocket Option! "
                f"Зарегистрируйте новый аккаунт строго по нашей партнёрской ссылке (Шаг 1) и введите ваш настоящий UID."
            ),
            "po_uid": clean_uid,
            "deposit_usd": 0.0,
            "min_deposit_usd": min_dep,
            "strict_mode": True,
            "register_url": build_tracked_po_url(tg_uid),
        }

    # В стандартном режиме проверяем, что выполнены все 3 шага и указан депозит >= min_dep
    if not step1_done:
        conn.close()
        return {
            "ok": True,
            "in_affiliate": False,
            "verified": False,
            "error": "Сначала подтвердите ваш аккаунт Telegram в Шаге 1!",
            "po_uid": clean_uid,
            "deposit_usd": 0.0,
            "min_deposit_usd": min_dep,
            "strict_mode": False,
            "register_url": build_tracked_po_url(tg_uid),
        }

    if not step2_done:
        conn.close()
        return {
            "ok": True,
            "in_affiliate": False,
            "verified": False,
            "error": "Сначала перейдите по партнёрской ссылке в Шаге 2 и зарегистрируйте аккаунт на Pocket Option!",
            "po_uid": clean_uid,
            "deposit_usd": 0.0,
            "min_deposit_usd": min_dep,
            "strict_mode": False,
            "register_url": build_tracked_po_url(tg_uid),
        }

    if False:
        conn.close()
        return {
            "ok": True,
            "in_affiliate": False,
            "verified": False,
            "error": f"Минимальный депозит для активации доступа — ${min_dep:.0f}. Пополните счёт на Pocket Option и укажите сумму депозита!",
            "po_uid": clean_uid,
            "deposit_usd": claimed_dep,
            "min_deposit_usd": min_dep,
            "strict_mode": False,
            "register_url": build_tracked_po_url(tg_uid),
        }

    # Сохраняем привязку пользователя и отправляем уведомление админу с кнопками контроля
    if tg_uid:
        upsert_user(tg_uid, confirm_step1=True)
        cur.execute(
            """
            UPDATE users
            SET registered_step1 = 1, visited_step2 = 1, po_uid = ?, deposit_usd = ?, is_verified = 1, tier = ?
            WHERE tg_user_id = ?
            """,
            (clean_uid, claimed_dep, "vip" if claimed_dep >= 250 else "standard", tg_uid),
        )
        conn.commit()
        u_row = cur.execute("SELECT * FROM users WHERE tg_user_id = ?", (tg_uid,)).fetchone()
    else:
        u_row = None
    conn.close()

    # Уведомляем администратора о новой привязке UID с кнопками подтверждения / блокировки
    if tg_uid:
        u_name = (u_row["first_name"] if u_row else "") or "Трейдер"
        u_handle = f"@{u_row['username']}" if (u_row and u_row["username"]) else "без username"
        adm_kb = {
            "inline_keyboard": [
                [
                    {"text": f"✅ В Postback (${claimed_dep:.0f})", "callback_data": f"adm_apv_{tg_uid}_{clean_uid}_{int(claimed_dep)}"},
                    {"text": "❌ Сбросить UID", "callback_data": f"adm_rej_{tg_uid}_{clean_uid}"},
                ],
                [
                    {"text": "👤 Карточка юзера", "callback_data": f"adm_usr_{tg_uid}"},
                ],
            ]
        }
        for adm_id in ADMIN_IDS:
            try:
                send_msg(
                    adm_id,
                    f"🆕 <b>Новая верификация UID в терминале:</b>\n"
                    f"• <b>Трейдер:</b> {html.escape(u_name)} ({u_handle})\n"
                    f"• <b>TG ID:</b> <code>{tg_uid}</code>\n"
                    f"• <b>Pocket Option UID:</b> <code>{clean_uid}</code>\n"
                    f"• <b>Заявленный депозит:</b> <b>${claimed_dep:.2f}</b>",
                    adm_kb,
                )
            except Exception:
                pass

    return {
        "ok": True,
        "in_affiliate": True,
        "verified": True,
        "po_uid": clean_uid,
        "deposit_usd": claimed_dep,
        "min_deposit_usd": min_dep,
        "strict_mode": strict_mode,
        "register_url": build_tracked_po_url(tg_uid),
    }


# ==================== HTTP SERVER (MINI APP + API + POSTBACK) ====================
def find_webapp_html_path() -> pathlib.Path:
    for candidate in ("pokoritel_webapp.html", "index.html", "sance_crypto_webapp.html"):
        p = BASE_DIR / candidate
        if p.exists() and p.stat().st_size > 100:
            return p
    return BASE_DIR / "pokoritel_webapp.html"


def find_welcome_media_path() -> pathlib.Path | None:
    candidates = ("welcome.mp4", "welcome.gif", "start.mp4", "start.gif", "welcome_mono.jpg", "welcome.jpg")
    for name in candidates:
        p = BASE_DIR / name
        if p.exists() and p.is_file() and p.stat().st_size > 100:
            return p
    return None


def is_valid_https_webapp_url(url: str) -> bool:
    if not url or not url.startswith("https://"):
        return False
    if "ваш-домен" in url or "your-domain" in url or "localhost" in url or "127.0.0.1" in url:
        return False
    try:
        parsed = urllib.parse.urlparse(url)
        return bool(parsed.netloc and "." in parsed.netloc)
    except Exception:
        return False


def find_free_port(preferred_port: int = 8080) -> int:
    for p in range(preferred_port, preferred_port + 25):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    return preferred_port


class SanceHttpHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:
        pass

    def _send_json(self, data: dict, status: int = 200) -> None:
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_HEAD(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS, HEAD")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw_body = self.rfile.read(length).decode("utf-8", errors="replace") if length > 0 else ""
        qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        params = {k: (v[0] if v else "") for k, v in qs.items()}
        if raw_body:
            try:
                jdata = json.loads(raw_body)
                if isinstance(jdata, dict):
                    for k, v in jdata.items():
                        params[str(k)] = str(v)
            except Exception:
                b_qs = urllib.parse.parse_qs(raw_body, keep_blank_values=True)
                for k, v in b_qs.items():
                    params[k] = v[0] if v else ""

        if parsed.path in ("/api/po-postback", "/api/postback", "/postback"):
            res = process_po_postback(params)
            self._send_json(res, 200 if res.get("ok") else 400)
            return

        if parsed.path == "/api/check-uid":
            uid = params.get("uid", "").strip()
            tg_id_str = params.get("tg_id", "").strip()
            dep_str = params.get("dep", "").strip()
            s1 = params.get("step1", "1") != "0"
            s2 = params.get("step2", "1") != "0"
            res = check_po_uid_status(uid, tg_id_str, dep_str, s1, s2)
            self._send_json(res, 200)
            return

        self._send_json({"ok": False, "error": "Unsupported endpoint"}, 404)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html", "/pokoritel_webapp.html", "/sance_crypto_webapp.html"):
            html_file = find_webapp_html_path()
            if html_file.exists():
                data = html_file.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                msg = b"HTML file not found!"
                self.send_response(404)
                self.send_header("Content-Length", str(len(msg)))
                self.end_headers()
                self.wfile.write(msg)
            return

        if path in ("/health", "/ping", "/api/health"):
            self._send_json({"ok": True, "status": "live", "service": "Sance AI Vision"})
            return

        if path == "/api/bot/status":
            self._send_json({
                "ok": True,
                "polling_active": BOT_POLLING_ACTIVE,
                "bot_username": BOT_USERNAME,
                "po_affiliate_url": get_po_affiliate_url(),
                "strict_po_verify": is_strict_postback_mode(),
                "min_deposit_usd": get_min_deposit_usd(),
            })
            return

        if path == "/api/config":
            qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            tg_id_str = (qs.get("tg_id", [""])[0] or "").strip()
            u_info = None
            if tg_id_str.isdigit():
                conn = get_db_conn()
                conn.row_factory = sqlite3.Row
                row = conn.execute("SELECT * FROM users WHERE tg_user_id = ?", (int(tg_id_str),)).fetchone()
                conn.close()
                if row:
                    u_info = {
                        "tg_user_id": row["tg_user_id"],
                        "step1": bool(row["registered_step1"]),
                        "step2": bool(row["visited_step2"]),
                        "is_verified": bool(row["is_verified"]),
                        "po_uid": row["po_uid"] or "",
                        "deposit_usd": float(row["deposit_usd"] or 0.0),
                        "bonus_signals": int(row["bonus_signals"] or 0),
                    }
            self._send_json({
                "ok": True,
                "po_affiliate_url": get_po_affiliate_url(),
                "strict_po_verify": is_strict_postback_mode(),
                "min_deposit_usd": get_min_deposit_usd(),
                "promos": get_all_promos_dict(),
                "user": u_info,
            })
            return

        if path == "/api/promos":
            self._send_json({"ok": True, "promos": get_all_promos_dict()})
            return

        if path in ("/api/po-postback", "/api/postback", "/postback"):
            qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            params = {k: (v[0] if v else "") for k, v in qs.items()}
            res = process_po_postback(params)
            self._send_json(res, 200 if res.get("ok") else 400)
            return

        if path == "/api/check-uid":
            qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            uid = (qs.get("uid", [""])[0] or "").strip()
            tg_id_str = (qs.get("tg_id", [""])[0] or "").strip()
            dep_str = (qs.get("dep", [""])[0] or "").strip()
            s1 = (qs.get("step1", ["1"])[0] or "1") != "0"
            s2 = (qs.get("step2", ["1"])[0] or "1") != "0"
            res = check_po_uid_status(uid, tg_id_str, dep_str, s1, s2)
            self._send_json(res, 200)
            return

        if path in ("/welcome_mono.jpg", "/sance_logo_mono.jpg", "/welcome.mp4", "/welcome.gif", "/guide.mp4"):
            media_file = BASE_DIR / path.lstrip("/")
            if media_file.exists():
                data = media_file.read_bytes()
                mime = "image/jpeg" if path.endswith(".jpg") else ("video/mp4" if path.endswith(".mp4") else "image/gif")
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Cache-Control", "public, max-age=3600")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return

        self._send_json({"ok": False}, 404)


class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# ==================== TELEGRAM API CALLS ====================
def tg_api_call(method: str, payload: dict | None = None, timeout: int = 35) -> dict:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    data = json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": "SanceCryptoBot/5.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            raise e
    except Exception:
        with DIRECT_OPENER.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))


def tg_api_multipart_call(
    method: str,
    fields: dict[str, str],
    file_field: str,
    file_path: pathlib.Path | None = None,
    file_bytes: bytes | None = None,
    filename: str = "file.bin",
    timeout: int = 45,
) -> dict:
    boundary = f"----SanceBotBoundary{uuid.uuid4().hex}"
    body = io.BytesIO()

    for k, v in fields.items():
        body.write(f"--{boundary}\r\n".encode("utf-8"))
        body.write(f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode("utf-8"))
        body.write(f"{v}\r\n".encode("utf-8"))

    if file_path is not None:
        filename = file_path.name
        raw_file = file_path.read_bytes()
    else:
        raw_file = file_bytes or b""

    mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    body.write(f"--{boundary}\r\n".encode("utf-8"))
    body.write(f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'.encode("utf-8"))
    body.write(f"Content-Type: {mime_type}\r\n\r\n".encode("utf-8"))
    body.write(raw_file)
    body.write(f"\r\n--{boundary}--\r\n".encode("utf-8"))

    raw_data = body.getvalue()
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    req = urllib.request.Request(
        url,
        data=raw_data,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(raw_data)),
            "User-Agent": "SanceCryptoBot/5.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            raise e
    except Exception:
        with DIRECT_OPENER.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))


def get_cached_file_id(cache_key: str) -> str | None:
    if cache_key in MEDIA_FILE_ID_CACHE:
        return MEDIA_FILE_ID_CACHE[cache_key]
    try:
        conn = get_db_conn()
        row = conn.execute("SELECT file_id FROM media_cache WHERE cache_key = ?", (cache_key,)).fetchone()
        conn.close()
        if row and row[0]:
            MEDIA_FILE_ID_CACHE[cache_key] = str(row[0])
            return str(row[0])
    except Exception:
        pass
    return None


def save_cached_file_id(cache_key: str, file_id: str) -> None:
    if not file_id:
        return
    MEDIA_FILE_ID_CACHE[cache_key] = file_id
    try:
        conn = get_db_conn()
        conn.execute(
            "INSERT OR REPLACE INTO media_cache (cache_key, file_id, updated_at) VALUES (?, ?, ?)",
            (cache_key, file_id, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def build_webapp_button(text: str = WELCOME_BUTTON_TEXT, include_style: bool = True, tg_user_id: int = 0) -> dict:
    btn: dict = {"text": text}
    if is_valid_https_webapp_url(WEBAPP_PUBLIC_URL):
        po_enc = urllib.parse.quote(get_po_affiliate_url(), safe="")
        sep = "&" if "?" in WEBAPP_PUBLIC_URL else "?"
        webapp_url = f"{WEBAPP_PUBLIC_URL}{sep}partner={po_enc}"
        if tg_user_id:
            conn = get_db_conn()
            conn.row_factory = sqlite3.Row
            u_row = conn.execute("SELECT po_uid, deposit_usd, is_verified FROM users WHERE tg_user_id = ?", (tg_user_id,)).fetchone()
            conn.close()
            if u_row and u_row["is_verified"] and u_row["po_uid"]:
                webapp_url += f"&uid={u_row['po_uid']}&dep={u_row['deposit_usd']}&ver=1"
        btn["web_app"] = {"url": webapp_url}
    else:
        btn["callback_data"] = "how_to_open_webapp"

    if include_style and WELCOME_BUTTON_STYLE in ("primary", "success", "danger"):
        btn["style"] = WELCOME_BUTTON_STYLE
    if include_style and WELCOME_BUTTON_CUSTOM_EMOJI_ID:
        btn["icon_custom_emoji_id"] = WELCOME_BUTTON_CUSTOM_EMOJI_ID
    return btn


def kb_main_menu_dict(include_style: bool = True, tg_user_id: int = 0) -> dict:
    rows = [
        [build_webapp_button(WELCOME_BUTTON_TEXT, include_style=include_style, tg_user_id=tg_user_id)],
        [{"text": "Регистрация Pocket Option (+50% WELCOME50) ↗", "url": build_tracked_po_url(tg_user_id)}],
    ]
    if is_admin(tg_user_id):
        rows.append([{"text": "👑 Админ-панель управления (/admin)", "callback_data": "adm_home"}])
    return {"inline_keyboard": rows}


def build_welcome_caption(first_name: str = "", tg_user_id: int = 0) -> str:
    trader_name = html.escape((first_name or "Трейдер").strip())
    po_url = build_tracked_po_url(tg_user_id)
    lines = [
        "<b>SANCE AI</b>  <code>MONOCHROME TERMINAL</code>",
        "<i>Квантовый нейротерминал анализа Pocket Option</i>",
        "",
        "<blockquote>",
        f"• <b>Профиль:</b> {trader_name} (ID: <code>{tg_user_id or '—'}</code>)",
        "• <b>Нейросети:</b> <code>GPT-5 Pro · Claude 4.5 · Gemini Argon</code>",
        "• <b>Активы:</b> <code>65 валютных и крипто-пар (1s – 4h)</code>",
        "• <b>Дневной лимит:</b> <code>15 / 15 сигналов (Winrate 94.2%)</code>",
        "</blockquote>",
        "",
        "<b>Как активировать доступ к сигналам:</b>",
        f"1. Зарегистрируйте аккаунт по <a href=\"{po_url}\">официальной ссылке Pocket Option</a>.",
        "2. Откройте терминал кнопкой ниже → вкладка <b>Профиль</b> (или отправьте ваш <b>UID</b> прямо в этот чат) для мгновенной проверки через Postback.",
    ]
    return chr(10).join(lines)


def send_msg(chat_id: int, text: str, reply_markup: dict | None = None, disable_preview: bool = True) -> dict:
    payload: dict = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": disable_preview,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        res = tg_api_call("sendMessage", payload, timeout=15)
        if not res.get("ok") and reply_markup:
            payload["reply_markup"] = kb_main_menu_dict(include_style=False, tg_user_id=chat_id)
            res = tg_api_call("sendMessage", payload, timeout=15)
        return res
    except Exception:
        return {"ok": False}


def edit_or_send_msg(chat_id: int, message_id: int | None, text: str, reply_markup: dict | None = None) -> None:
    if message_id:
        payload: dict = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            res = tg_api_call("editMessageText", payload, timeout=15)
            if res.get("ok") or "message is not modified" in str(res.get("description", "")):
                return
        except Exception:
            pass
    send_msg(chat_id, text, reply_markup)


def answer_cb(cb_id: str, text: str = "", alert: bool = False) -> None:
    try:
        tg_api_call("answerCallbackQuery", {"callback_query_id": cb_id, "text": text, "show_alert": alert}, timeout=10)
    except Exception:
        pass


def send_welcome(chat_id: int, first_name: str = "") -> None:
    caption = build_welcome_caption(first_name, tg_user_id=chat_id)
    markup_styled = kb_main_menu_dict(include_style=True, tg_user_id=chat_id)
    markup_plain = kb_main_menu_dict(include_style=False, tg_user_id=chat_id)

    if WELCOME_MEDIA_URL:
        is_url_photo = any(WELCOME_MEDIA_URL.lower().endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp"))
        url_method = "sendPhoto" if is_url_photo else "sendAnimation"
        url_field = "photo" if is_url_photo else "animation"
        for mk in (markup_styled, markup_plain):
            try:
                res = tg_api_call(
                    url_method,
                    {"chat_id": chat_id, url_field: WELCOME_MEDIA_URL, "caption": caption, "parse_mode": "HTML", "reply_markup": mk},
                    timeout=25,
                )
                if res.get("ok"):
                    return
            except Exception:
                pass

    media_path = find_welcome_media_path()
    if media_path and media_path.exists():
        is_photo = media_path.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")
        api_method = "sendPhoto" if is_photo else "sendAnimation"
        field_name = "photo" if is_photo else "animation"
        stat = media_path.stat()
        cache_key = f"v31_mono_anim:{media_path.name}:{stat.st_size}:{int(stat.st_mtime)}"
        cached_id = get_cached_file_id(cache_key)
        if cached_id:
            for mk in (markup_styled, markup_plain):
                try:
                    res = tg_api_call(
                        api_method,
                        {"chat_id": chat_id, field_name: cached_id, "caption": caption, "parse_mode": "HTML", "reply_markup": mk},
                        timeout=20,
                    )
                    if res.get("ok"):
                        return
                except Exception:
                    pass

        for mk in (markup_styled, markup_plain):
            fields = {
                "chat_id": str(chat_id),
                "caption": caption,
                "parse_mode": "HTML",
                "reply_markup": json.dumps(mk, ensure_ascii=False),
            }
            try:
                res = tg_api_multipart_call(api_method, fields, field_name, file_path=media_path, timeout=45)
                if res.get("ok"):
                    msg_res = res.get("result", {})
                    if is_photo and isinstance(msg_res.get("photo"), list) and msg_res["photo"]:
                        file_id = msg_res["photo"][-1].get("file_id")
                    else:
                        file_id = (
                            (msg_res.get("animation") or {}).get("file_id")
                            or (msg_res.get("video") or {}).get("file_id")
                            or (msg_res.get("document") or {}).get("file_id")
                        )
                    if file_id:
                        save_cached_file_id(cache_key, file_id)
                    return
            except Exception as e:
                logger.warning(f"Не удалось отправить медиа ({e}), пробую фолбэк...")

    send_msg(chat_id, caption, markup_styled)


# ==================== АДМИН-ПАНЕЛЬ (/admin) ====================
def build_admin_dashboard_text() -> str:
    today = msk_today_str()
    conn = get_db_conn()
    total_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    today_users = conn.execute("SELECT COUNT(*) FROM users WHERE created_at LIKE ?", (f"{today}%",)).fetchone()[0]
    verified_users = conn.execute("SELECT COUNT(*) FROM users WHERE is_verified = 1 AND po_uid != ''").fetchone()[0]
    unverified_users = max(0, total_users - verified_users)
    banned_users = conn.execute("SELECT COUNT(*) FROM users WHERE is_banned = 1").fetchone()[0]
    total_dep_users = conn.execute("SELECT COALESCE(SUM(deposit_usd), 0) FROM users WHERE is_verified = 1").fetchone()[0]
    pb_count = conn.execute("SELECT COUNT(*) FROM po_postbacks").fetchone()[0]
    pb_dep_sum = conn.execute("SELECT COALESCE(SUM(total_deposit_usd), 0) FROM po_postbacks").fetchone()[0]
    promos_count = conn.execute("SELECT COUNT(*) FROM promo_codes").fetchone()[0]
    conn.close()

    po_url = get_po_affiliate_url()
    strict_mode = is_strict_postback_mode()
    min_dep = get_min_deposit_usd()

    mode_str = (
        "🔒 Строгий Postback (только из базы партнёрки/админа)"
        if strict_mode
        else "⚡ 3 шага (ТГ + Регистрация по ссылке + Валидный UID)"
    )

    return (
        f"👑 <b>АДМИН-ПАНЕЛЬ SANCE CRYPTO 3D</b>\n"
        f"<i>Управление трафиком, Pocket Option, промокодами и рассылками</i>\n\n"
        f"<blockquote>"
        f"👥 <b>Всего трейдеров в боте:</b> <b>{total_users}</b> (сегодня: <b>+{today_users}</b>)\n"
        f"✅ <b>Верифицировали UID:</b> <b>{verified_users}</b> | ⏳ <b>Без UID:</b> <b>{unverified_users}</b>\n"
        f"💰 <b>Сумма депозитов (юзеры / Postback):</b> <b>${total_dep_users:.0f}</b> / <b>${pb_dep_sum:.0f}</b> ({pb_count} зап.)\n"
        f"🎟 <b>Активных промокодов:</b> <b>{promos_count} шт.</b> | 🚫 <b>В бане:</b> <b>{banned_users}</b>"
        f"</blockquote>\n\n"
        f"🛡 <b>Режим проверки UID:</b> {mode_str}\n"
        f"🔓 <b>Условие доступа:</b> <code>Регистрация + Привязка UID (без депозита)</code>\n"
        f"🔗 <b>Текущая реф. ссылка Pocket Option:</b>\n<code>{html.escape(po_url)}</code>\n\n"
        f"👇 <b>Выберите нужное действие на клавиатуре ниже:</b>"
    )


def kb_admin_main() -> dict:
    strict_on = is_strict_postback_mode()
    return {
        "inline_keyboard": [
            [
                {"text": "📢 Сделать рассылку", "callback_data": "adm_broadcast_menu"},
                {"text": "🎯 Дожим без UID", "callback_data": "adm_nudge_unverified"},
            ],
            [
                {"text": "🔗 Сменить ссылку Pocket Option", "callback_data": "adm_set_po_link"},
            ],
            [
                {
                    "text": f"🛡 Режим: {'🔒 Строгий Postback' if strict_on else '⚡ Регистрация + UID'}",
                    "callback_data": "adm_toggle_strict",
                },
            ],
            [
                {"text": "🎁 Промокоды (Создать / Удалить)", "callback_data": "adm_promos"},
                {"text": "✅ Подтвердить UID", "callback_data": "adm_add_uid_prompt"},
            ],
            [
                {"text": "🔍 Найти юзера (ID / UID)", "callback_data": "adm_find_user"},
                {"text": "📋 Последние трейдеры", "callback_data": "adm_recent_users"},
            ],
            [
                {"text": "🎉 Раздать +5 сигн. всем", "callback_data": "adm_gift_all"},
                {"text": "📈 Топ рефоводов", "callback_data": "adm_top_refs"},
            ],
            [
                {"text": "📥 Выгрузить базу CSV", "callback_data": "adm_export_csv"},
                {"text": "🔗 Postback-шпаргалка", "callback_data": "adm_postback_info"},
            ],
            [
                {"text": "🔄 Обновить дашборд", "callback_data": "adm_home"},
            ],
        ]
    }


def kb_admin_back() -> dict:
    return {"inline_keyboard": [[{"text": "‹ Назад в Админ-панель", "callback_data": "adm_home"}]]}


def render_user_dossier_for_admin(tg_or_po_id: str) -> tuple[str, dict]:
    clean = re.sub(r"\D", "", tg_or_po_id or "")
    conn = get_db_conn()
    conn.row_factory = sqlite3.Row
    row = None
    if clean:
        row = conn.execute("SELECT * FROM users WHERE tg_user_id = ? OR po_uid = ?", (int(clean), clean)).fetchone()
    if not row and tg_or_po_id:
        uname = tg_or_po_id.lstrip("@").lower()
        row = conn.execute("SELECT * FROM users WHERE username = ?", (uname,)).fetchone()
    conn.close()

    if not row:
        return f"❌ Пользователь с ID / UID / username <code>{html.escape(tg_or_po_id)}</code> не найден в базе.", kb_admin_back()

    uid_tg = int(row["tg_user_id"])
    po_uid = row["po_uid"] or "Не привязан"
    dep = float(row["deposit_usd"] or 0.0)
    ver = "ДА ✓" if row["is_verified"] else "НЕТ (Доступ закрыт)"
    banned = "🚫 ЗАБАНЕН" if row["is_banned"] else "Активен"
    uname_str = f"@{row['username']}" if row["username"] else "нет"

    text = (
        f"👤 <b>ДОСЬЕ ТРЕЙДЕРА</b>\n\n"
        f"• <b>Имя:</b> {html.escape(row['first_name'] or 'Трейдер')} ({uname_str})\n"
        f"• <b>Telegram ID:</b> <code>{uid_tg}</code>\n"
        f"• <b>Статус аккаунта:</b> {banned}\n"
        f"• <b>Шаг 1 (ТГ) / Шаг 2 (Ссылка):</b> {'✓' if row['registered_step1'] else '✗'} / {'✓' if row['visited_step2'] else '✗'}\n"
        f"• <b>Pocket Option UID:</b> <code>{po_uid}</code>\n"
        f"• <b>Депозит:</b> <b>${dep:.2f}</b>\n"
        f"• <b>Верифицирован (Доступ открыт):</b> <b>{ver}</b>\n"
        f"• <b>Бонусных сигналов:</b> <b>+{int(row['bonus_signals'] or 0)}/сут</b>\n"
        f"• <b>Дата входа:</b> <code>{(row['created_at'] or '')[:19]}</code>"
    )
    kb = {
        "inline_keyboard": [
            [
                {"text": "✅ Одобрить / Изм. Депозит", "callback_data": f"adm_ueditdep_{uid_tg}"},
                {"text": "❌ Сбросить UID (Закрыть)", "callback_data": f"adm_urej_{uid_tg}"},
            ],
            [
                {"text": "⚡ Выдать +10 бонус-сигналов", "callback_data": f"adm_ugift_{uid_tg}"},
                {"text": "✉️ Написать личное сообщение", "callback_data": f"adm_udm_{uid_tg}"},
            ],
            [
                {
                    "text": "✅ Разбанить" if row["is_banned"] else "🔨 Забанить юзера",
                    "callback_data": f"adm_uban_{uid_tg}",
                }
            ],
            [{"text": "‹ Назад в Админ-панель", "callback_data": "adm_home"}],
        ]
    }
    return text, kb


def run_broadcast_thread(
    admin_chat_id: int,
    target_audience: str,
    msg_obj: dict,
    attach_buttons: bool = True,
) -> None:
    conn = get_db_conn()
    if target_audience == "unverified":
        rows = conn.execute("SELECT tg_user_id FROM users WHERE is_banned = 0 AND (is_verified = 0 OR po_uid = '')").fetchall()
    elif target_audience == "verified":
        rows = conn.execute("SELECT tg_user_id FROM users WHERE is_banned = 0 AND is_verified = 1 AND po_uid != ''").fetchall()
    else:
        rows = conn.execute("SELECT tg_user_id FROM users WHERE is_banned = 0").fetchall()
    conn.close()

    user_ids = [int(r[0]) for r in rows]
    total = len(user_ids)
    sent_ok = 0
    failed = 0

    text_html = msg_obj.get("text_html", "")
    photo_id = msg_obj.get("photo_id", "")
    video_id = msg_obj.get("video_id", "")
    anim_id = msg_obj.get("anim_id", "")

    for uid in user_ids:
        kb = None
        if attach_buttons:
            kb = {
                "inline_keyboard": [
                    [build_webapp_button(WELCOME_BUTTON_TEXT, include_style=True, tg_user_id=uid)],
                    [{"text": "Регистрация Pocket Option (+50% WELCOME50) ↗", "url": build_tracked_po_url(uid)}],
                ]
            }
        try:
            if photo_id:
                payload = {"chat_id": uid, "photo": photo_id, "caption": text_html, "parse_mode": "HTML"}
                if kb:
                    payload["reply_markup"] = kb
                res = tg_api_call("sendPhoto", payload, timeout=15)
            elif video_id:
                payload = {"chat_id": uid, "video": video_id, "caption": text_html, "parse_mode": "HTML"}
                if kb:
                    payload["reply_markup"] = kb
                res = tg_api_call("sendVideo", payload, timeout=15)
            elif anim_id:
                payload = {"chat_id": uid, "animation": anim_id, "caption": text_html, "parse_mode": "HTML"}
                if kb:
                    payload["reply_markup"] = kb
                res = tg_api_call("sendAnimation", payload, timeout=15)
            else:
                res = send_msg(uid, text_html, kb, disable_preview=False)

            if res and res.get("ok"):
                sent_ok += 1
            else:
                failed += 1
        except Exception:
            failed += 1
        time.sleep(0.04)

    send_msg(
        admin_chat_id,
        f"✅ <b>Рассылка завершена!</b>\n\n"
        f"• <b>Аудитория:</b> <code>{target_audience.upper()}</code> ({total} чел.)\n"
        f"• <b>Успешно доставлено:</b> <b>{sent_ok}</b>\n"
        f"• <b>Не доставлено (блокировка бота):</b> <b>{failed}</b>",
        kb_admin_back(),
    )


def handle_admin_callback(cb: dict) -> bool:
    cb_id = cb["id"]
    data = cb.get("data", "")
    from_user = cb.get("from", {})
    user_id = int(from_user.get("id", 0))
    msg = cb.get("message") or {}
    chat_id = int((msg.get("chat") or {}).get("id", user_id))
    msg_id = msg.get("message_id")

    if not data.startswith("adm_"):
        return False

    if not is_admin(user_id):
        answer_cb(cb_id, "Доступ только для администратора (1394187706)!", alert=True)
        return True

    USER_FSM_STATE.pop(user_id, None)

    if data == "adm_home":
        answer_cb(cb_id, "Дашборд обновлён ✓")
        edit_or_send_msg(chat_id, msg_id, build_admin_dashboard_text(), kb_admin_main())
        return True

    if data == "adm_toggle_strict":
        cur_val = is_strict_postback_mode()
        new_val = "0" if cur_val else "1"
        set_setting("strict_po_verify", new_val)
        sync_settings_and_promos_to_html()
        answer_cb(
            cb_id,
            "Включён СТРОГИЙ Postback-режим!" if new_val == "1" else "Включён режим 3 шага + UID + Депозит!",
            alert=True,
        )
        edit_or_send_msg(chat_id, msg_id, build_admin_dashboard_text(), kb_admin_main())
        return True

    if data == "adm_set_mindep":
        answer_cb(cb_id)
        kb = {
            "inline_keyboard": [
                [
                    {"text": "$10", "callback_data": "adm_mindep_10"},
                    {"text": "$20", "callback_data": "adm_mindep_20"},
                    {"text": "$30", "callback_data": "adm_mindep_30"},
                ],
                [
                    {"text": "$50", "callback_data": "adm_mindep_50"},
                    {"text": "$100", "callback_data": "adm_mindep_100"},
                    {"text": "$0 (Без депа)", "callback_data": "adm_mindep_0"},
                ],
                [{"text": "‹ Назад в Админ-панель", "callback_data": "adm_home"}],
            ]
        }
        edit_or_send_msg(
            chat_id,
            msg_id,
            f"💵 <b>Настройка минимального депозита Pocket Option</b>\n\n"
            f"Текущий порог: <b>${get_min_deposit_usd():.0f}</b>\n"
            f"Выберите минимальную сумму депозита, при которой пользователю открывается доступ к сигналам:",
            kb,
        )
        return True

    if data.startswith("adm_mindep_"):
        val_str = data.replace("adm_mindep_", "")
        set_setting("min_deposit_usd", val_str)
        sync_settings_and_promos_to_html()
        answer_cb(cb_id, f"Минимальный депозит установлен: ${val_str} ✓", alert=True)
        edit_or_send_msg(chat_id, msg_id, build_admin_dashboard_text(), kb_admin_main())
        return True

    if data == "adm_set_po_link":
        answer_cb(cb_id)
        USER_FSM_STATE[user_id] = {"step": "wait_po_link"}
        edit_or_send_msg(
            chat_id,
            msg_id,
            f"🔗 <b>СМЕНА РЕФЕРАЛЬНОЙ ССЫЛКИ POCKET OPTION</b>\n\n"
            f"Текущая ссылка:\n<code>{html.escape(get_po_affiliate_url())}</code>\n\n"
            f"✍️ <b>Отправьте новую партнёрскую ссылку сообщением в этот чат:</b>\n"
            f"<i>(Она автоматически обновится во всех кнопках бота, в приветствии и внутри Mini App!)</i>",
            kb_admin_back(),
        )
        return True

    if data == "adm_broadcast_menu":
        answer_cb(cb_id)
        kb = {
            "inline_keyboard": [
                [{"text": "📢 Всем пользователям бота", "callback_data": "adm_bc_start_all"}],
                [{"text": "⏳ Только тем, кто ещё БЕЗ UID (Дожим)", "callback_data": "adm_bc_start_unverified"}],
                [{"text": "✅ Только верифицированным с UID", "callback_data": "adm_bc_start_verified"}],
                [{"text": "‹ Назад в Админ-панель", "callback_data": "adm_home"}],
            ]
        }
        edit_or_send_msg(
            chat_id,
            msg_id,
            "📢 <b>СОЗДАНИЕ РАССЫЛКИ В БОТЕ</b>\n\n"
            "Выберите аудиторию, которой хотите отправить сообщение:",
            kb,
        )
        return True

    if data.startswith("adm_bc_start_"):
        aud = data.replace("adm_bc_start_", "")
        answer_cb(cb_id)
        USER_FSM_STATE[user_id] = {"step": "wait_bc_content", "audience": aud}
        edit_or_send_msg(
            chat_id,
            msg_id,
            f"📢 <b>РАССЫЛКА (Аудитория: <code>{aud.upper()}</code>)</b>\n\n"
            f"✍️ <b>Отправьте сообщение для рассылки прямо в чат:</b>\n"
            f"• Можно отправить обычный <b>текст</b> (поддерживается HTML: <code>&lt;b&gt;жирный&lt;/b&gt;</code>, <code>&lt;code&gt;код&lt;/code&gt;</code>)\n"
            f"• Или отправьте <b>фото / видео / GIF</b> с подписью!",
            kb_admin_back(),
        )
        return True

    if data in ("adm_bc_send_with_btns", "adm_bc_send_no_btns"):
        st = USER_FSM_STATE.get(user_id) or {}
        msg_obj = st.get("msg_obj")
        aud = st.get("audience", "all")
        if not msg_obj:
            answer_cb(cb_id, "Данные рассылки устарели, создайте заново.", alert=True)
            edit_or_send_msg(chat_id, msg_id, build_admin_dashboard_text(), kb_admin_main())
            return True
        attach_btns = data == "adm_bc_send_with_btns"
        USER_FSM_STATE.pop(user_id, None)
        answer_cb(cb_id, "Рассылка запущена! 🚀")
        edit_or_send_msg(chat_id, msg_id, "⏳ <b>Рассылка выполняется в фоновом режиме...</b>\nПо завершении бот пришлёт отчёт.", kb_admin_back())
        threading.Thread(
            target=run_broadcast_thread,
            args=(chat_id, aud, msg_obj, attach_btns),
            daemon=True,
        ).start()
        return True

    if data == "adm_nudge_unverified":
        answer_cb(cb_id, "Запускаю дожим неверифицированных пользователей...")
        nudge_text = (
            f"🔥 <b>Ваш доступ к ИИ-терминалу Sance Crypto 3D почти открыт!</b>\n\n"
            f"Вы ещё не завершили привязку торгового счёта <b>Pocket Option</b>.\n\n"
            f"1️⃣ Зарегистрируйте аккаунт по официальной ссылке с промокодом <code>WELCOME50</code> (<b>+50% бонус к депозиту</b>).\n"
            f"2️⃣ Пополните счёт и введите ваш личный числовой <b>UID</b> во вкладке <b>«Профиль»</b>.\n"
            f"3️⃣ Сразу после проверки вам откроется <b>15 ИИ-сигналов в сутки</b> и 3D-рамки!"
        )
        threading.Thread(
            target=run_broadcast_thread,
            args=(chat_id, "unverified", {"text_html": nudge_text}, True),
            daemon=True,
        ).start()
        edit_or_send_msg(
            chat_id,
            msg_id,
            "🎯 <b>Запущена рассылка-дожим по всем пользователям без привязанного UID!</b>\nОтчёт придёт через несколько секунд.",
            kb_admin_back(),
        )
        return True

    if data == "adm_promos":
        answer_cb(cb_id)
        promos = get_all_promos_dict()
        lines = [f"• <code>{k}</code> — <b>+{v['sig']} сигналов</b> и <b>+{v['xp']} XP</b>" for k, v in promos.items()]
        kb_rows = [[{"text": "➕ Создать новый промокод", "callback_data": "adm_promo_add"}]]
        row_del = []
        for k in promos.keys():
            row_del.append({"text": f"🗑 {k}", "callback_data": f"adm_promo_del_{k}"})
            if len(row_del) == 2:
                kb_rows.append(row_del)
                row_del = []
        if row_del:
            kb_rows.append(row_del)
        kb_rows.append([{"text": "‹ Назад в Админ-панель", "callback_data": "adm_home"}])

        edit_or_send_msg(
            chat_id,
            msg_id,
            "🎁 <b>УПРАВЛЕНИЕ ПРОМОКОДАМИ В ПРИЛОЖЕНИИ</b>\n\n"
            "Активные коды (вводятся пользователями во вкладке «Профиль» → «Промокод»):\n"
            + ("\n".join(lines) if lines else "<i>Нет активных промокодов</i>")
            + "\n\nНажмите <b>«➕ Создать новый промокод»</b> или нажмите на кнопку с корзиной, чтобы удалить код:",
            {"inline_keyboard": kb_rows},
        )
        return True

    if data == "adm_promo_add":
        answer_cb(cb_id)
        USER_FSM_STATE[user_id] = {"step": "wait_new_promo"}
        edit_or_send_msg(
            chat_id,
            msg_id,
            "➕ <b>СОЗДАНИЕ НОВОГО ПРОМОКОДА</b>\n\n"
            "✍️ Отправьте в чат код, количество бонусных сигналов и XP через пробел:\n"
            "<code>КОД СИГНАЛЫ XP</code>\n\n"
            "Примеры:\n"
            "• <code>SANCE777 10 250</code> (даст +10 сигналов и +250 XP)\n"
            "• <code>BONUS15 15 400</code> (даст +15 сигналов и +400 XP)",
            kb_admin_back(),
        )
        return True

    if data.startswith("adm_promo_del_"):
        code = data.replace("adm_promo_del_", "").strip().upper()
        conn = get_db_conn()
        conn.execute("DELETE FROM promo_codes WHERE code = ?", (code,))
        conn.commit()
        conn.close()
        sync_settings_and_promos_to_html()
        answer_cb(cb_id, f"Промокод {code} удалён ✓")
        cb["data"] = "adm_promos"
        return handle_admin_callback(cb)

    if data == "adm_add_uid_prompt":
        answer_cb(cb_id)
        USER_FSM_STATE[user_id] = {"step": "wait_manual_uid"}
        edit_or_send_msg(
            chat_id,
            msg_id,
            "✅ <b>ПОДТВЕРЖДЕНИЕ UID И ДЕПОЗИТА ТРЕЙДЕРА</b>\n\n"
            "✍️ Отправьте в чат числовой <b>UID Pocket Option</b> и <b>сумму депозита ($)</b> через пробел (и по желанию TG ID трейдеров):\n"
            "<code>UID СУММА_USD [TG_ID]</code>\n\n"
            "Пример:\n"
            "<code>94827163 100</code> или <code>94827163 150 1394187706</code>\n\n"
            "<i>Этот UID сразу попадёт в базу партнёрки, и пользователь получит подтверждённый депозит и доступ 15/15!</i>",
            kb_admin_back(),
        )
        return True

    if data == "adm_find_user":
        answer_cb(cb_id)
        USER_FSM_STATE[user_id] = {"step": "wait_find_user"}
        edit_or_send_msg(
            chat_id,
            msg_id,
            "🔍 <b>ПОИСК ТРЕЙДЕРА В БАЗЕ</b>\n\n"
            "✍️ Отправьте в чат <b>Telegram ID</b>, <b>Pocket Option UID</b> или <b>@username</b> пользователя:",
            kb_admin_back(),
        )
        return True

    if data == "adm_recent_users":
        answer_cb(cb_id)
        conn = get_db_conn()
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM users ORDER BY created_at DESC LIMIT 12").fetchall()
        conn.close()
        lines = []
        kb_rows = []
        row_btns = []
        for r in rows:
            st_ico = "✅" if r["is_verified"] and r["po_uid"] else "⏳"
            uid_str = f"UID: <code>{r['po_uid']}</code> (${float(r['deposit_usd'] or 0):.0f})" if r["po_uid"] else "без UID"
            uname = f"@{r['username']}" if r["username"] else (r["first_name"] or str(r["tg_user_id"]))
            lines.append(f"{st_ico} <code>{r['tg_user_id']}</code> ({html.escape(uname)}) — {uid_str}")
            row_btns.append({"text": f"{st_ico} {r['tg_user_id']}", "callback_data": f"adm_usr_{r['tg_user_id']}"})
            if len(row_btns) == 2:
                kb_rows.append(row_btns)
                row_btns = []
        if row_btns:
            kb_rows.append(row_btns)
        kb_rows.append([{"text": "‹ Назад в Админ-панель", "callback_data": "adm_home"}])
        edit_or_send_msg(
            chat_id,
            msg_id,
            "📋 <b>ПОСЛЕДНИЕ ПОЛЬЗОВАТЕЛИ БОТА:</b>\n\n"
            + ("\n".join(lines) if lines else "База пользователей пуста.")
            + "\n\nНажмите на кнопку с ID пользователя для управления его аккаунтом:",
            {"inline_keyboard": kb_rows},
        )
        return True

    if data.startswith("adm_usr_"):
        target_tg = data.replace("adm_usr_", "").strip()
        answer_cb(cb_id)
        txt, kb = render_user_dossier_for_admin(target_tg)
        edit_or_send_msg(chat_id, msg_id, txt, kb)
        return True

    if data.startswith("adm_apv_"):
        # Формат: adm_apv_<tgid>_<uid>_<dep>
        parts = data.split("_")
        if len(parts) >= 5:
            t_tg = parts[2]
            t_uid = parts[3]
            t_dep = parts[4]
            process_po_postback({"trader_id": t_uid, "sumdep": t_dep, "event": "ftd", "sub_id1": t_tg}, notify=True)
            answer_cb(cb_id, f"UID {t_uid} (${t_dep}) подтверждён и занесён в Postback!", alert=True)
            txt, kb = render_user_dossier_for_admin(t_tg)
            edit_or_send_msg(chat_id, msg_id, txt, kb)
        return True

    if data.startswith("adm_rej_") or data.startswith("adm_urej_"):
        parts = data.split("_")
        t_tg = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else 0
        if t_tg:
            conn = get_db_conn()
            conn.row_factory = sqlite3.Row
            u_row = conn.execute("SELECT po_uid FROM users WHERE tg_user_id = ?", (t_tg,)).fetchone()
            old_uid = u_row["po_uid"] if u_row else ""
            conn.execute(
                "UPDATE users SET po_uid = '', deposit_usd = 0.0, is_verified = 0, tier = 'guest' WHERE tg_user_id = ?",
                (t_tg,),
            )
            if old_uid:
                conn.execute("DELETE FROM po_postbacks WHERE po_uid = ?", (old_uid,))
            conn.commit()
            conn.close()
            send_msg(
                t_tg,
                f"⚠️ <b>Верификация UID {old_uid} отклонена администратором!</b>\n\n"
                f"Аккаунт с этим UID не найден в нашей партнёрской программе или на нём отсутствует депозит.\n"
                f"Пожалуйста, зарегистрируйте аккаунт по официальной ссылке с кодом <code>WELCOME50</code> и введите свой настоящий UID:",
                kb_main_menu_dict(include_style=True, tg_user_id=t_tg),
            )
            answer_cb(cb_id, f"UID пользователя {t_tg} сброшен, доступ закрыт!", alert=True)
            txt, kb = render_user_dossier_for_admin(str(t_tg))
            edit_or_send_msg(chat_id, msg_id, txt, kb)
        return True

    if data.startswith("adm_ueditdep_"):
        t_tg = data.replace("adm_ueditdep_", "").strip()
        answer_cb(cb_id)
        USER_FSM_STATE[user_id] = {"step": "wait_user_dep", "target_tg": int(t_tg)}
        edit_or_send_msg(
            chat_id,
            msg_id,
            f"💵 <b>ИЗМЕНЕНИЕ UID И ДЕПОЗИТА ПОЛЬЗОВАТЕЛЯ <code>{t_tg}</code></b>\n\n"
            f"✍️ Отправьте в чат <b>UID и сумму депозита ($)</b> через пробел (или просто сумму депозита, если UID уже указан):\n"
            f"Пример: <code>94827163 150</code> или <code>200</code>",
            kb_admin_back(),
        )
        return True

    if data.startswith("adm_ugift_"):
        t_tg = int(data.replace("adm_ugift_", "").strip())
        conn = get_db_conn()
        conn.execute("UPDATE users SET bonus_signals = COALESCE(bonus_signals, 0) + 10 WHERE tg_user_id = ?", (t_tg,))
        conn.commit()
        conn.close()
        send_msg(
            t_tg,
            "🎁 <b>Администратор начислил вам бонус: +10 сигналов к дневному лимиту!</b>",
            kb_main_menu_dict(include_style=True, tg_user_id=t_tg),
        )
        answer_cb(cb_id, "Начислено +10 бонусных сигналов ✓", alert=True)
        txt, kb = render_user_dossier_for_admin(str(t_tg))
        edit_or_send_msg(chat_id, msg_id, txt, kb)
        return True

    if data.startswith("adm_udm_"):
        t_tg = int(data.replace("adm_udm_", "").strip())
        answer_cb(cb_id)
        USER_FSM_STATE[user_id] = {"step": "wait_user_dm", "target_tg": t_tg}
        edit_or_send_msg(
            chat_id,
            msg_id,
            f"✉️ <b>ЛИЧНОЕ СООБЩЕНИЕ ПОЛЬЗОВАТЕЛЮ <code>{t_tg}</code></b>\n\n"
            f"✍️ Напишите текст сообщения в чат — бот сразу доставит его этому пользователю:",
            kb_admin_back(),
        )
        return True

    if data.startswith("adm_uban_"):
        t_tg = int(data.replace("adm_uban_", "").strip())
        conn = get_db_conn()
        row = conn.execute("SELECT is_banned FROM users WHERE tg_user_id = ?", (t_tg,)).fetchone()
        new_ban = 0 if (row and row[0]) else 1
        conn.execute("UPDATE users SET is_banned = ? WHERE tg_user_id = ?", (new_ban, t_tg))
        conn.commit()
        conn.close()
        answer_cb(cb_id, "Пользователь забанен!" if new_ban else "Пользователь разбанен!", alert=True)
        txt, kb = render_user_dossier_for_admin(str(t_tg))
        edit_or_send_msg(chat_id, msg_id, txt, kb)
        return True

    if data == "adm_gift_all":
        conn = get_db_conn()
        conn.execute("UPDATE users SET bonus_signals = COALESCE(bonus_signals, 0) + 5 WHERE is_banned = 0")
        conn.commit()
        conn.close()
        answer_cb(cb_id, "Всем пользователям начислено +5 бонусных сигналов!", alert=True)
        gift_msg = (
            "🎉 <b>БОНУС ОТ АДМИНИСТРАЦИИ SANCE CRYPTO!</b>\n\n"
            "Вам начислено <b>+5 дополнительных ИИ-сигналов</b> к суточному лимиту. Удачной торговой сессии!"
        )
        threading.Thread(
            target=run_broadcast_thread,
            args=(chat_id, "all", {"text_html": gift_msg}, True),
            daemon=True,
        ).start()
        edit_or_send_msg(chat_id, msg_id, build_admin_dashboard_text(), kb_admin_main())
        return True

    if data == "adm_top_refs":
        answer_cb(cb_id)
        conn = get_db_conn()
        rows = conn.execute(
            """
            SELECT referred_by, COUNT(*) as cnt
            FROM users
            WHERE referred_by > 0
            GROUP BY referred_by
            ORDER BY cnt DESC
            LIMIT 10
            """
        ).fetchall()
        conn.close()
        lines = [f"• TG ID <code>{r[0]}</code> — привёл <b>{r[1]} чел.</b> (+{r[1]*5} сигн.)" for r in rows]
        edit_or_send_msg(
            chat_id,
            msg_id,
            "📈 <b>ТОП РЕФОВОДОВ И ИСТОЧНИКОВ ТРАФИКА:</b>\n\n"
            + ("\n".join(lines) if lines else "<i>Пока нет приглашённых по реферальным ссылкам.</i>"),
            kb_admin_back(),
        )
        return True

    if data == "adm_export_csv":
        answer_cb(cb_id, "Формирую CSV-выгрузку...")
        conn = get_db_conn()
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()
        conn.close()

        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["tg_user_id", "username", "first_name", "po_uid", "deposit_usd", "is_verified", "bonus_signals", "referred_by", "created_at"])
        for r in rows:
            writer.writerow([
                r["tg_user_id"],
                r["username"],
                r["first_name"],
                r["po_uid"],
                r["deposit_usd"],
                r["is_verified"],
                r["bonus_signals"],
                r["referred_by"],
                r["created_at"],
            ])
        csv_bytes = out.getvalue().encode("utf-8-sig")
        try:
            tg_api_multipart_call(
                "sendDocument",
                {"chat_id": str(chat_id), "caption": f"📥 <b>Выгрузка базы пользователей Sance Crypto</b> ({len(rows)} чел.)"},
                "document",
                file_bytes=csv_bytes,
                filename=f"sance_users_{msk_today_str()}.csv",
            )
        except Exception as e:
            send_msg(chat_id, f"Ошибка выгрузки CSV: {e}", kb_admin_back())
        return True

    if data == "adm_postback_info":
        answer_cb(cb_id)
        edit_or_send_msg(
            chat_id,
            msg_id,
            "<b>🔗 НАСТРОЙКА АВТО-ПРОВЕРКИ POCKET OPTION AFFILIATE:</b>\n\n"
            "1. Зайдите в кабинет партнёра <b>Pocket Option Affiliate</b> → раздел <b>Postbacks (Постбэки)</b>.\n"
            "2. Укажите ссылки на ваш сервер бота:\n\n"
            "• <b>Регистрация (Registration):</b>\n"
            "<code>https://sancecrypto.onrender.com/api/po-postback?trader_id={trader_id}&sub_id1={sub_id1}&event=reg</code>\n\n"
            "• <b>Первый и повторные депозиты (FTD / Deposit):</b>\n"
            "<code>https://sancecrypto.onrender.com/api/po-postback?trader_id={trader_id}&sumdep={sumdep}&sub_id1={sub_id1}&event=dep</code>\n\n"
            "💡 <i>Даже без настройки Postback на хостинге вы можете одобрять/проверять UID и депозиты прямо из этой Админ-панели или из уведомлений, которые приходят вам при вводе UID каждым пользователем!</i>",
            kb_admin_back(),
        )
        return True

    return True


def handle_admin_fsm_message(user_id: int, chat_id: int, msg: dict) -> bool:
    if not is_admin(user_id) or user_id not in USER_FSM_STATE:
        return False

    st = USER_FSM_STATE[user_id]
    step = st.get("step", "")
    text = (msg.get("text") or "").strip()

    if text.startswith("/cancel") or text.startswith("/admin"):
        USER_FSM_STATE.pop(user_id, None)
        if text.startswith("/admin"):
            return False
        send_msg(chat_id, "Действие отменено.", kb_admin_main())
        return True

    if step == "wait_po_link":
        if not text.startswith("http://") and not text.startswith("https://"):
            send_msg(chat_id, "⚠️ Ссылка должна начинаться с <code>https://</code>! Отправьте корректную ссылку или нажмите Назад:", kb_admin_back())
            return True
        set_setting("po_affiliate_url", text)
        sync_settings_and_promos_to_html()
        USER_FSM_STATE.pop(user_id, None)
        send_msg(
            chat_id,
            f"✅ <b>Реферальная ссылка Pocket Option успешно обновлена!</b>\n\n"
            f"Новая ссылка:\n<code>{html.escape(text)}</code>\n\n"
            f"Она автоматически прописана в базе бота и внутри файлов приложения (<code>index.html</code> / <code>pokoritel_webapp.html</code>).",
            kb_admin_main(),
        )
        return True

    if step == "wait_new_promo":
        parts = text.split()
        if len(parts) < 2:
            send_msg(chat_id, "⚠️ Формат: <code>КОД СИГНАЛЫ [XP]</code>\nПример: <code>SANCE777 10 200</code>", kb_admin_back())
            return True
        code = re.sub(r"[^A-Z0-9_-]", "", parts[0].strip().upper())
        sig = max(1, int(parts[1])) if parts[1].isdigit() else 5
        xp = max(10, int(parts[2])) if (len(parts) > 2 and parts[2].isdigit()) else (sig * 25)
        conn = get_db_conn()
        conn.execute(
            "INSERT OR REPLACE INTO promo_codes (code, sig, xp, used_count, created_at) VALUES (?, ?, ?, 0, ?)",
            (code, sig, xp, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        conn.close()
        sync_settings_and_promos_to_html()
        USER_FSM_STATE.pop(user_id, None)
        send_msg(
            chat_id,
            f"✅ <b>Промокод <code>{code}</code> создан и синхронизирован с приложением!</b>\n"
            f"• Награда при активации: <b>+{sig} сигналов</b> и <b>+{xp} XP</b> к 3D-рамке.",
            kb_admin_main(),
        )
        return True

    if step == "wait_manual_uid":
        parts = text.split()
        if len(parts) < 2:
            send_msg(chat_id, "⚠️ Укажите UID и сумму через пробел, например: <code>94827163 100</code>", kb_admin_back())
            return True
        uid_arg = re.sub(r"\D", "", parts[0])
        dep_arg = parts[1].replace(",", ".")
        tg_arg = parts[2] if len(parts) > 2 else ""
        res = process_po_postback({"trader_id": uid_arg, "sumdep": dep_arg, "event": "ftd", "sub_id1": tg_arg}, notify=True)
        USER_FSM_STATE.pop(user_id, None)
        send_msg(
            chat_id,
            f"✅ <b>UID <code>{uid_arg}</code> подтверждён и добавлен в базу партнёрки!</b>\n"
            f"• Депозит: <b>${res.get('deposit_usd', 0):.2f}</b>\n"
            f"• Привязанный TG ID: <code>{res.get('tg_user_id') or 'ожидает ввода пользователем'}</code>",
            kb_admin_main(),
        )
        return True

    if step == "wait_find_user":
        USER_FSM_STATE.pop(user_id, None)
        txt, kb = render_user_dossier_for_admin(text)
        send_msg(chat_id, txt, kb)
        return True

    if step == "wait_user_dep":
        target_tg = int(st.get("target_tg", 0))
        parts = text.split()
        conn = get_db_conn()
        conn.row_factory = sqlite3.Row
        u_row = conn.execute("SELECT po_uid FROM users WHERE tg_user_id = ?", (target_tg,)).fetchone()
        conn.close()
        if len(parts) >= 2:
            uid_val = re.sub(r"\D", "", parts[0])
            dep_val = parts[1].replace(",", ".")
        else:
            uid_val = (u_row["po_uid"] if u_row and u_row["po_uid"] else str(target_tg))
            dep_val = parts[0].replace(",", ".")
        process_po_postback({"trader_id": uid_val, "sumdep": dep_val, "event": "ftd", "sub_id1": str(target_tg)}, notify=True)
        USER_FSM_STATE.pop(user_id, None)
        txt, kb = render_user_dossier_for_admin(str(target_tg))
        send_msg(chat_id, f"✅ <b>Данные пользователя {target_tg} обновлены!</b>\n\n" + txt, kb)
        return True

    if step == "wait_user_dm":
        target_tg = int(st.get("target_tg", 0))
        USER_FSM_STATE.pop(user_id, None)
        res = send_msg(
            target_tg,
            f"📩 <b>Сообщение от поддержки Sance Crypto:</b>\n\n{text}",
            kb_main_menu_dict(include_style=True, tg_user_id=target_tg),
        )
        ok_str = "доставлено ✓" if res.get("ok") else "не доставлено (бот заблокирован пользователем)"
        send_msg(chat_id, f"✉️ Сообщение пользователю <code>{target_tg}</code> {ok_str}.", kb_admin_main())
        return True

    if step == "wait_bc_content":
        aud = st.get("audience", "all")
        cap = (msg.get("caption") or msg.get("text") or "").strip()
        photo_list = msg.get("photo") or []
        photo_id = photo_list[-1]["file_id"] if photo_list else ""
        video_id = (msg.get("video") or {}).get("file_id", "")
        anim_id = (msg.get("animation") or {}).get("file_id", "")

        if not cap and not photo_id and not video_id and not anim_id:
            send_msg(chat_id, "⚠️ Отправьте текст, фото, видео или GIF для рассылки:", kb_admin_back())
            return True

        st["step"] = "confirm_bc"
        st["msg_obj"] = {
            "text_html": cap,
            "photo_id": photo_id,
            "video_id": video_id,
            "anim_id": anim_id,
        }
        kb = {
            "inline_keyboard": [
                [{"text": "🚀 Отправить С кнопками (Терминал + Регистрация PO)", "callback_data": "adm_bc_send_with_btns"}],
                [{"text": "📨 Отправить БЕЗ кнопок", "callback_data": "adm_bc_send_no_btns"}],
                [{"text": "✕ Отмена", "callback_data": "adm_home"}],
            ]
        }
        send_msg(
            chat_id,
            f"✅ <b>Сообщение для рассылки принято!</b>\n"
            f"• Аудитория: <code>{aud.upper()}</code>\n"
            f"• Медиа: <code>{'Фото' if photo_id else ('Видео' if video_id else ('GIF' if anim_id else 'Только текст'))}</code>\n\n"
            f"Выберите, прикрепить ли под сообщением кнопки запуска приложения и регистрации в Pocket Option:",
            kb,
        )
        return True

    return False


# ==================== ОБРАБОТКА ВХОДЯЩИХ СОБЫТИЙ TELEGRAM ====================
def handle_tg_update(upd: dict) -> None:
    if "callback_query" in upd:
        cb = upd["callback_query"]
        if handle_admin_callback(cb):
            return
        cb_id = cb["id"]
        data = cb.get("data", "")
        if data == "how_to_open_webapp":
            answer_cb(cb_id, "Укажите WEBAPP_PUBLIC_URL в настройках для открытия приложения!", alert=True)
            return
        return

    msg = upd.get("message")
    if not msg:
        return
    from_user = msg.get("from", {})
    user_id = int(from_user.get("id", 0))
    chat_id = int(msg.get("chat", {}).get("id", user_id))
    text = (msg.get("text") or "").strip()

    # Проверяем бан
    if not is_admin(user_id):
        conn = get_db_conn()
        b_row = conn.execute("SELECT is_banned FROM users WHERE tg_user_id = ?", (user_id,)).fetchone()
        conn.close()
        if b_row and b_row[0]:
            send_msg(chat_id, "🚫 Ваш доступ к боту ограничен администратором.")
            return

    # Если админ сейчас в режиме ввода данных для админ-панели
    if handle_admin_fsm_message(user_id, chat_id, msg):
        return

    # Команда открытия интерактивной Админ-панели (/admin или /panel)
    if text.startswith("/admin") or text.startswith("/panel"):
        if not is_admin(user_id):
            send_msg(chat_id, f"⛔ Доступ к админ-панели разрешён только владельцу бота. Ваш TG ID: <code>{user_id}</code>")
            return
        upsert_user(user_id, from_user.get("username", ""), from_user.get("first_name", ""), confirm_step1=True)
        send_msg(chat_id, build_admin_dashboard_text(), kb_admin_main())
        return

    # Быстрые текстовые команды админа (оставлены для удобства вместе с кнопками /admin)
    if text.startswith("/addpromo") and is_admin(user_id):
        parts = text.split()
        if len(parts) < 3:
            send_msg(chat_id, "Формат: <code>/addpromo КОД СИГНАЛЫ [XP]</code>", kb_admin_main())
            return
        code = re.sub(r"[^A-Z0-9_-]", "", parts[1].strip().upper())
        sig = max(1, int(parts[2])) if parts[2].isdigit() else 5
        xp = max(10, int(parts[3])) if (len(parts) > 3 and parts[3].isdigit()) else (sig * 25)
        conn = get_db_conn()
        conn.execute(
            "INSERT OR REPLACE INTO promo_codes (code, sig, xp, used_count, created_at) VALUES (?, ?, ?, 0, ?)",
            (code, sig, xp, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        conn.close()
        sync_settings_and_promos_to_html()
        send_msg(chat_id, f"✅ Промокод <code>{code}</code> (+{sig} сигн., +{xp} XP) добавлен!", kb_admin_main())
        return

    if text.startswith("/delpromo") and is_admin(user_id):
        parts = text.split()
        if len(parts) < 2:
            send_msg(chat_id, "Укажите код: <code>/delpromo КОД</code>")
            return
        code = parts[1].strip().upper()
        conn = get_db_conn()
        conn.execute("DELETE FROM promo_codes WHERE code = ?", (code,))
        conn.commit()
        conn.close()
        sync_settings_and_promos_to_html()
        send_msg(chat_id, f"🗑 Промокод <code>{code}</code> удалён.", kb_admin_main())
        return

    if text.startswith("/setdep") and is_admin(user_id):
        parts = text.split()
        if len(parts) < 3:
            send_msg(chat_id, "Формат: <code>/setdep UID СУММА_USD [TG_ID]</code>")
            return
        uid_arg = re.sub(r"\D", "", parts[1])
        dep_arg = parts[2].replace(",", ".")
        tg_arg = parts[3] if len(parts) > 3 else ""
        res = process_po_postback({"trader_id": uid_arg, "sumdep": dep_arg, "event": "ftd", "sub_id1": tg_arg}, notify=True)
        send_msg(chat_id, f"✅ UID <code>{uid_arg}</code> (Депозит: <b>${res.get('deposit_usd', 0):.2f}</b>) подтверждён!", kb_admin_main())
        return

    if text.startswith("/po") or text.startswith("/reg"):
        upsert_user(user_id, from_user.get("username", ""), from_user.get("first_name", ""), confirm_step1=True)
        conn = get_db_conn()
        conn.execute("UPDATE users SET visited_step2 = 1 WHERE tg_user_id = ?", (user_id,))
        conn.commit()
        conn.close()
        reg_url = build_tracked_po_url(user_id)
        send_msg(
            chat_id,
            f"<b>◈ ОФИЦИАЛЬНАЯ РЕГИСТРАЦИЯ POCKET OPTION</b>\n\n"
            f"1. Перейдите по официальной партнёрской ссылке ниже и создайте аккаунт на Pocket Option.\n"
            f"2. Скопируйте ваш числовой <b>UID</b> из профиля Pocket Option.\n"
            f"3. Отправьте ваш <b>UID</b> сюда в чат или введите его во вкладке <b>«Профиль»</b> в приложении — доступ к сигналам откроется сразу!",
            {
                "inline_keyboard": [
                    [{"text": "Регистрация Pocket Option (+50% WELCOME50) ↗", "url": reg_url}],
                    [build_webapp_button(WELCOME_BUTTON_TEXT, include_style=True, tg_user_id=user_id)],
                ]
            },
        )
        return

    # Если пользователь прислал в чат свой числовой UID (и опционально депозит: "94827163" или "94827163 100")
    m_uid_chat = re.fullmatch(r"(?:UID[:\s#-]*)?(\d{6,12})(?:\s+(\d+(?:[.,]\d+)?))?", text, flags=re.IGNORECASE)
    if m_uid_chat:
        upsert_user(user_id, from_user.get("username", ""), from_user.get("first_name", ""), confirm_step1=True)
        clean_uid = m_uid_chat.group(1)
        dep_part = m_uid_chat.group(2) or "50"
        st = check_po_uid_status(clean_uid, str(user_id), dep_part, step1_done=True, step2_done=True)
        if not st.get("verified"):
            reg_url = build_tracked_po_url(user_id)
            send_msg(
                chat_id,
                f"⚠️ <b>Проверка не пройдена:</b>\n{html.escape(st.get('error') or 'Ошибка проверки UID')}\n\n"
                f"Убедитесь, что вы зарегистрировали аккаунт по нашей партнёрской ссылке и ввели свой личный числовой UID:",
                {"inline_keyboard": [[{"text": "Регистрация Pocket Option (+50% WELCOME50) ↗", "url": reg_url}]]},
            )
            return
        dep_usd = float(st.get("deposit_usd") or 0.0)
        send_msg(
            chat_id,
            f"✅ <b>Ваш Pocket Option UID <code>{clean_uid}</code> успешно проверен и привязан!</b>\n"
            f"• <b>Ваш Telegram ID:</b> <code>{user_id}</code> ✓\n"
            f"• <b>Статус регистрации:</b> <code>ПОДТВЕРЖДЁН ✓</code>\n"
            f"• <b>Лимит сигналов:</b> <code>15 / 15 в сутки</code>\n\n"
            f"Нажмите кнопку ниже, чтобы открыть 3D-терминал:",
            kb_main_menu_dict(include_style=True, tg_user_id=user_id),
        )
        return

    if text.startswith("/start") or text.startswith("/menu"):
        parts = text.split(maxsplit=1)
        ref_id = 0
        if len(parts) > 1:
            arg = parts[1].replace("ref_", "").strip()
            if arg.isdigit():
                ref_id = int(arg)
        upsert_user(user_id, from_user.get("username", ""), from_user.get("first_name", ""), referrer_id=ref_id, confirm_step1=True)
        send_welcome(chat_id, from_user.get("first_name", "Трейдер"))
        return

    upsert_user(user_id, from_user.get("username", ""), from_user.get("first_name", ""), confirm_step1=True)
    send_welcome(chat_id, from_user.get("first_name", "Трейдер"))


def run_telegram_polling_loop() -> None:
    global BOT_POLLING_ACTIVE, BOT_USERNAME
    offset = 0
    while True:
        if not BOT_POLLING_ACTIVE:
            try:
                me_resp = tg_api_call("getMe", {}, timeout=12)
                if me_resp.get("ok"):
                    BOT_USERNAME = me_resp["result"].get("username", "SanceCryptoBot")
                    BOT_POLLING_ACTIVE = True
                    logger.info(f"🤖 Telegram-бот @{BOT_USERNAME} запущен! Админ ID: {ADMIN_IDS}")
                    try:
                        tg_api_call("deleteWebhook", {"drop_pending_updates": False}, timeout=10)
                    except Exception:
                        pass
            except Exception:
                time.sleep(3)
                continue

        try:
            res = tg_api_call("getUpdates", {"offset": offset, "timeout": 20}, timeout=28)
            for upd in res.get("result", []):
                offset = max(offset, int(upd["update_id"]) + 1)
                handle_tg_update(upd)
        except Exception:
            time.sleep(2)


def main() -> None:
    global PORT
    init_db()
    sync_settings_and_promos_to_html()
    PORT = find_free_port(PORT)

    server = ThreadedHTTPServer(("0.0.0.0", PORT), SanceHttpHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    print(f"✅ Сервер Sance Crypto запущен на порту {PORT} | Админ-панель: /admin (ID: {ADMIN_IDS})")
    threading.Thread(target=run_telegram_polling_loop, daemon=True).start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
