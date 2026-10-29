
# -*- coding: utf-8 -*-
import asyncio
import logging
import os
import json
import base64
import random
import requests
from datetime import datetime
from aiogram import Bot, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
import google.generativeai as genai
import content

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_REPO = os.getenv("GITHUB_REPOSITORY")
MAX_RUNTIME = 5.5 * 3600

genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
AI_MODEL_NAME = "gemini-1.5-flash"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")
bot = Bot(token=TELEGRAM_TOKEN)

PERMISSIONS = {
    "view_stats": "📊 الإحصائيات",
    "view_users": "👥 المستخدمين",
    "manage_subscription": "📢 الاشتراك",
    "manage_notifications": "🔔 الإشعارات",
    "manage_admins": "👤 الأدمن",
    "manage_menu": "🎛️ الأزرار",
    "view_audit_log": "📜 السجل",
    "broadcast": "📣 رسالة جماعية",
}
DEFAULT_ADMIN_PERMS = ["view_stats"]

chat_mode, chat_sessions, admin_state, pending_buttons = {}, {}, {}, {}
short_answers_cache = {}

users_cache, users_dirty = {}, False
progress_cache, progress_dirty = {}, False
audit_cache = {"logs": []}
audit_dirty = False


def load_config():
    try:
        r = requests.get(f"https://api.github.com/repos/{GITHUB_REPO}/contents/config.json",
                         headers={"Authorization": f"token {GITHUB_TOKEN}"}, timeout=10)
        if r.status_code == 200:
            return json.loads(base64.b64decode(r.json()["content"]).decode())
    except Exception as e:
        logging.error(f"load_config: {e}")
    return {"owner_id": 5117740235, "admins": {},
            "force_subscription": {"enabled": False, "channels": [], "message": ""},
            "notifications": {"enabled": True, "on_new_user": True,
                              "on_new_subscriber": True, "on_message": False},
            "main_menu": {"buttons_per_row": 2, "buttons": []}}


def save_config(cfg):
    try:
        r = requests.get(f"https://api.github.com/repos/{GITHUB_REPO}/contents/config.json",
                         headers={"Authorization": f"token {GITHUB_TOKEN}"}, timeout=10)
        sha = r.json().get("sha") if r.status_code == 200 else None
        data = {"message": f"config {datetime.utcnow().isoformat()}",
                "content": base64.b64encode(
                    json.dumps(cfg, ensure_ascii=False, indent=2).encode("utf-8")).decode()}
        if sha:
            data["sha"] = sha
        requests.put(f"https://api.github.com/repos/{GITHUB_REPO}/contents/config.json",
                     headers={"Authorization": f"token {GITHUB_TOKEN}"}, json=data, timeout=10)
    except Exception as e:
        logging.error(f"save_config: {e}")


def load_json(filename, default):
    try:
        r = requests.get(f"https://api.github.com/repos/{GITHUB_REPO}/contents/{filename}",
                         headers={"Authorization": f"token {GITHUB_TOKEN}"}, timeout=10)
        if r.status_code == 200:
            return json.loads(base64.b64decode(r.json()["content"]).decode())
    except Exception as e:
        logging.error(f"load {filename}: {e}")
    return default


def save_json(filename, data):
    try:
        r = requests.get(f"https://api.github.com/repos/{GITHUB_REPO}/contents/{filename}",
                         headers={"Authorization": f"token {GITHUB_TOKEN}"}, timeout=10)
        sha = r.json().get("sha") if r.status_code == 200 else None
        payload = {"message": f"update {filename} {datetime.utcnow().isoformat()}",
                   "content": base64.b64encode(
                       json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")).decode()}
        if sha:
            payload["sha"] = sha
        requests.put(f"https://api.github.com/repos/{GITHUB_REPO}/contents/{filename}",
                     headers={"Authorization": f"token {GITHUB_TOKEN}"}, json=payload, timeout=10)
    except Exception as e:
        logging.error(f"save {filename}: {e}")


CONFIG = load_config()


def get_offset():
    d = load_json("offset.json", {"offset": 0})
    return d.get("offset", 0)


def save_offset(offset):
    save_json("offset.json", {"offset": offset})


def is_owner(uid):
    return uid == CONFIG.get("owner_id")


def has_perm(uid, perm):
    if is_owner(uid):
        return True
    return perm in CONFIG.get("admins", {}).get(str(uid), {}).get("permissions", [])


def user_mention(uid, name):
    safe = name.replace("<", "&lt;").replace(">", "&gt;").replace("&", "&amp;")
    return f'<a href="tg://user?id={uid}">{safe}</a>'


def register_user(msg):
    global users_dirty
    u = msg.from_user
    uid = str(u.id)
    now = datetime.utcnow().isoformat()
    is_new = uid not in users_cache
    if is_new:
        users_cache[uid] = {"id": u.id, "first_name": u.first_name or "",
                            "last_name": u.last_name or "", "username": u.username or "",
                            "joined_at": now, "last_seen": now, "messages_count": 1}
    else:
        users_cache[uid]["last_seen"] = now
        users_cache[uid]["messages_count"] = users_cache[uid].get("messages_count", 0) + 1
    users_dirty = True
    return is_new


def get_progress(uid):
    return progress_cache.get(str(uid), {})


def get_curriculum_ar(uid):
    curriculum = get_progress(uid).get("curriculum")
    return {"sanaa": "صنعاء", "aden": "عدن"}.get(curriculum, "غير محدد")


def get_user_model(uid):
    return genai.GenerativeModel(
        model_name=AI_MODEL_NAME,
        system_instruction=content.AI_SYSTEM_PROMPT.format(
            curriculum_ar=get_curriculum_ar(uid))
    )


def update_progress(uid, **kwargs):
    global progress_dirty
    uid = str(uid)
    if uid not in progress_cache:
        progress_cache[uid] = {"onboarding_done": False, "situation": None, "curriculum": None,
                               "stream": None, "subject": None, "streak": 0, "last_session": None,
                               "exams": [], "completed_units": [], "quiz_history": []}
    progress_cache[uid].update(kwargs)
    progress_dirty = True
    return progress_cache[uid]


def calculate_streak(uid):
    p = get_progress(uid)
    last = p.get("last_session")
    streak = p.get("streak", 0)
    if not last:
        return streak
    try:
        last_date = datetime.fromisoformat(last).date()
        diff = (datetime.utcnow().date() - last_date).days
        if diff > 1:
            return 0
    except Exception:
        pass
    return streak


def update_streak(uid):
    p = get_progress(uid)
    last = p.get("last_session")
    streak = p.get("streak", 0)
    today = datetime.utcnow()
    if not last:
        streak = 1
    else:
        try:
            diff = (today.date() - datetime.fromisoformat(last).date()).days
            if diff == 1:
                streak += 1
            elif diff > 1:
                streak = 1
        except Exception:
            streak = 1
    update_progress(uid, streak=streak, last_session=today.isoformat())
    return streak


def log_action(actor, action, target="", details=""):
    global audit_dirty
    entry = {"ts": datetime.utcnow().isoformat(), "actor_id": actor.id,
             "actor_name": f"{actor.first_name or ''} {actor.last_name or ''}".strip() or f"ID:{actor.id}",
             "action": action, "target": str(target), "details": details}
    audit_cache["logs"].append(entry)
    if len(audit_cache["logs"]) > 500:
        audit_cache["logs"] = audit_cache["logs"][-500:]
    audit_dirty = True


async def notify_owner(text, parse_mode="HTML"):
    n = CONFIG.get("notifications", {})
    if not n.get("enabled", True):
        return
    owner = CONFIG.get("owner_id")
    if not owner:
        return
    try:
        await bot.send_message(owner, text, parse_mode=parse_mode, disable_web_page_preview=True)
    except Exception as e:
        logging.warning(f"notify_owner: {e}")


async def check_subscription(uid):
    fs = CONFIG.get("force_subscription", {})
    if not fs.get("enabled", False):
        return True, []
    channels = fs.get("channels", [])
    if not channels:
        return True, []
    if is_owner(uid) or str(uid) in CONFIG.get("admins", {}):
        return True, []
    missing = []
    for ch in channels:
        try:
            m = await bot.get_chat_member(chat_id=ch, user_id=uid)
            if m.status in ("left", "kicked"):
                missing.append(ch)
        except Exception:
            pass
    return len(missing) == 0, missing


def get_all_buttons():
    return CONFIG.get("main_menu", {}).get("buttons", [])


def get_buttons():
    return [b for b in get_all_buttons() if b.get("enabled", True)]


def main_menu():
    buttons = get_buttons()
    if not buttons:
        return InlineKeyboardMarkup(inline_keyboard=[])
    per_row = max(1, min(3, CONFIG.get("main_menu", {}).get("buttons_per_row", 2)))
    kb = []
    for i in range(0, len(buttons), per_row):
        row = []
        for b in buttons[i:i + per_row]:
            t, v = b.get("type", "callback"), b.get("value", "")
            if t == "url":
                row.append(InlineKeyboardButton(text=b.get("text", ""), url=v))
            elif t == "prompt":
                row.append(InlineKeyboardButton(text=b.get("text", ""), callback_data=f"prompt_{b['id']}"))
            else:
                row.append(InlineKeyboardButton(text=b.get("text", ""), callback_data=v))
        kb.append(row)
    return InlineKeyboardMarkup(inline_keyboard=kb)


def back_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 القائمة الرئيسية", callback_data="home")]
    ])


def chat_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 الخروج من الدردشة", callback_data="chat_exit")]
    ])


def onboarding_situations_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔥 عندي امتحان قريب جداً", callback_data="onb_emergency")],
        [InlineKeyboardButton(text="⏰ المنهج متراكم عليّ", callback_data="onb_backlog")],
        [InlineKeyboardButton(text="📚 بدأت الفصل للتو", callback_data="onb_start")],
        [InlineKeyboardButton(text="✅ أذاكر بانتظام", callback_data="onb_regular")],
    ])


def onboarding_curriculum_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏛️ منهج وزارة التربية (صنعاء)", callback_data="onb_curr_sanaa")],
        [InlineKeyboardButton(text="🌍 منهج وزارة التربية (عدن)", callback_data="onb_curr_aden")],
    ])


def onboarding_streams_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔬 علمي", callback_data="onb_sci")],
        [InlineKeyboardButton(text="📖 أدبي", callback_data="onb_lit")],
    ])


def onboarding_subjects_kb(stream):
    subs = content.SUBJECTS_SCIENTIFIC if stream == "scientific" else content.SUBJECTS_LITERARY
    kb = []
    for i in range(0, len(subs), 2):
        row = [InlineKeyboardButton(text=s, callback_data=f"onb_sub_{s}") for s in subs[i:i + 2]]
        kb.append(row)
    return InlineKeyboardMarkup(inline_keyboard=kb)


def emergency_subjects_kb(stream):
    subs = content.SUBJECTS_SCIENTIFIC if stream == "scientific" else content.SUBJECTS_LITERARY
    kb = []
    for i in range(0, len(subs), 2):
        row = [InlineKeyboardButton(text=s, callback_data=f"em_sub_{s}") for s in subs[i:i + 2]]
        kb.append(row)
    return InlineKeyboardMarkup(inline_keyboard=kb)


def short_answer_menu(msg_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ فهمت", callback_data=f"sa_ok_{msg_id}")],
        [InlineKeyboardButton(text="📖 تفاصيل أكثر", callback_data=f"sa_more_{msg_id}")],
        [InlineKeyboardButton(text="❓ سؤال آخر", callback_data="sa_new")],
    ])


async def chat_with_ai(message):
    uid = message.from_user.id
    try:
        await bot.send_chat_action(chat_id=message.chat.id, action="typing")
        session = chat_sessions.get(uid)
        if not session:
            session = get_user_model(uid).start_chat(history=[])
            chat_sessions[uid] = session
        response = await session.send_message_async(message.text)
        full = response.text
        lines = full.split("\n")
        short = "\n".join(lines[:4])
        if len(short) > 350:
            short = short[:347] + "..."
        try:
            sent = await bot.send_message(
                message.chat.id, short + "\n\n_... (رد مختصر)_",
                parse_mode="Markdown", reply_markup=short_answer_menu(0))
        except Exception as markdown_error:
            logging.warning(f"Markdown chat response failed; retrying as plain text: {markdown_error}")
            sent = await bot.send_message(
                message.chat.id, short + "\n\n... (رد مختصر)",
                reply_markup=short_answer_menu(0))
        short_answers_cache[sent.message_id] = full
        await bot.edit_message_reply_markup(message.chat.id, sent.message_id,
                                            reply_markup=short_answer_menu(sent.message_id))
    except Exception as e:
        logging.error(f"chat: {e}")
        await bot.send_message(message.chat.id, "⚠️ حدث خطأ. حاول مرة أخرى.", reply_markup=chat_menu())


async def show_emergency_plan(cb, subject):
    p = get_progress(cb.from_user.id)
    update_progress(cb.from_user.id, subject=subject,
                    exams=p.get("exams", []) + [{"subject": subject, "days": 3}])
    text = content.EMERGENCY_PLAN.format(subject=subject, days=3)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📖 برومبت اليوم 1", callback_data=f"em_p1_{subject}")],
        [InlineKeyboardButton(text="🎯 برومبت اليوم 2", callback_data=f"em_p2_{subject}")],
        [InlineKeyboardButton(text="🔁 برومبت اليوم 3", callback_data=f"em_p3_{subject}")],
        [InlineKeyboardButton(text="🏠 القائمة الرئيسية", callback_data="home")],
    ])
    await cb.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    await cb.answer()


async def show_progress(cb):
    uid = cb.from_user.id
    p = get_progress(uid)
    streak = calculate_streak(uid)
    situation_map = {"emergency": "🔥 امتحان قريب", "backlog": "⏰ منهج متراكم",
                     "start": "📚 بداية الفصل", "regular": "✅ منتظم"}
    stream_map = {"scientific": "🔬 علمي", "literary": "📖 أدبي"}
    curriculum_map = {"sanaa": "وزارة التربية (صنعاء)", "aden": "وزارة التربية (عدن)"}
    subject = p.get("subject") or "لم تبدأ بعد"
    situation = situation_map.get(p.get("situation"), "غير محدد")
    stream = stream_map.get(p.get("stream"), "غير محدد")
    curriculum = curriculum_map.get(p.get("curriculum"), "غير محدد")
    quizzes = p.get("quiz_history", [])
    text = (f"📊 *تقدمك يا {cb.from_user.first_name or 'صديقي'}*\n\n"
            f"*وضعك:* {situation}\n"
            f"*المنهج:* {curriculum}\n"
            f"*قسمك:* {stream}\n"
            f"*المادة الحالية:* {subject}\n"
            f"*🔥 سلسلة الدراسة:* {streak} أيام متتالية\n\n"
            f"*📝 الكويزات المنجزة:* {len(quizzes)}\n")
    if quizzes:
        avg = sum(q.get("score", 0) for q in quizzes) / len(quizzes)
        text += f"*📈 متوسط درجاتك:* {avg:.1f}\n"
    text += f"\n💪 _{random.choice(content.ENCOURAGEMENTS)}_"
    await cb.message.edit_text(text, reply_markup=back_menu(), parse_mode="Markdown")
    await cb.answer()


async def handle_message(msg):
    uid = msg.from_user.id
    text = (msg.text or "").strip()

    is_new = register_user(msg)
    if is_new:
        if CONFIG.get("notifications", {}).get("on_new_user", True):
            u = msg.from_user
            name = f"{u.first_name or ''} {u.last_name or ''}".strip() or "بدون اسم"
            uname = f"@{u.username}" if u.username else "❌ بدون معرف"
            notification = (f"🆕 <b>مستخدم جديد!</b>\n\n"
                            f"👤 {user_mention(u.id, name)}\n"
                            f"🆔 <code>{u.id}</code>\n"
                            f"🔗 {uname}\n"
                            f"👥 الإجمالي: <b>{len(users_cache)}</b>")
            asyncio.create_task(notify_owner(notification))

    ok, missing = await check_subscription(uid)
    if not ok:
        fs = CONFIG.get("force_subscription", {})
        txt = fs.get("message", "⚠️ اشترك في القنوات:") + "\n\n" + "\n".join(f"• {c}" for c in missing)
        kb = [[InlineKeyboardButton(text=f"📢 اشترك في {c}", url=f"https://t.me/{c.lstrip('@')}")] for c in missing]
        kb.append([InlineKeyboardButton(text="✅ تحقّق", callback_data="check_sub")])
        await msg.answer(txt, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="Markdown")
        return

    state = admin_state.get(uid)
    if state and await handle_admin_state(msg, state):
        return

    if text in ("/start", "/menu"):
        chat_mode[uid] = False
        chat_sessions.pop(uid, None)
        p = get_progress(uid)
        if p.get("onboarding_done") and not p.get("curriculum"):
            await msg.answer(content.ONBOARDING_CURRICULUM,
                             reply_markup=onboarding_curriculum_kb(), parse_mode="Markdown")
            return
        if not p.get("onboarding_done"):
            update_progress(uid, onboarding_done=False)
            await msg.answer(content.ONBOARDING_WELCOME, parse_mode="Markdown")
            await msg.answer(content.ONBOARDING_SITUATION,
                             reply_markup=onboarding_situations_kb(), parse_mode="Markdown")
            return
        streak = update_streak(uid)
        summary = f"• المادة: {p.get('subject', 'لم تحدد')}\n• الكويزات: {len(p.get('quiz_history', []))}"
        badge = " 🎉" if streak >= 3 else ""
        await msg.answer(content.WELCOME_BACK.format(
            name=msg.from_user.first_name or "", progress_summary=summary,
            streak=streak, streak_badge=badge,
            next_action="استخدم الأزرار للبدء 👇"),
            reply_markup=main_menu(), parse_mode="Markdown")
        return

    if text == "/help":
        await msg.answer("📖 *أوامر:*\n/start — البداية\n/chat — دردشة\n/exit — خروج\n"
                         "/progress — تقدمي\n/admin — لوحة التحكم\n/help — مساعدة",
                         parse_mode="Markdown")
        return

    if text == "/chat":
        chat_mode[uid] = True
        chat_sessions[uid] = get_user_model(uid).start_chat(history=[])
        await msg.answer(content.CHAT_WELCOME, reply_markup=chat_menu(), parse_mode="Markdown")
        return

    if text == "/exit":
        chat_mode[uid] = False
        chat_sessions.pop(uid, None)
        await msg.answer("✅ خرجت من الدردشة.\n\n" + random.choice(content.ENCOURAGEMENTS),
                         reply_markup=main_menu(), parse_mode="Markdown")
        return

    if text == "/progress":
        await msg.answer("📊 اضغط الزر لعرض تقدمك:",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                             [InlineKeyboardButton(text="📊 تقدمي", callback_data="my_progress")]]))
        return

    if text == "/admin":
        if not is_owner(uid) and str(uid) not in CONFIG.get("admins", {}):
            await msg.answer("⛔ للأدمن فقط.")
            return
        await show_admin_panel(msg)
        return

    if chat_mode.get(uid):
        await chat_with_ai(msg)
        return

    await msg.answer("👋 استخدم الأزرار للتنقل، أو /chat للدردشة.", reply_markup=main_menu())


async def handle_admin_state(msg, state):
    uid = msg.from_user.id
    if state == "awaiting_add_channel":
        if not has_perm(uid, "manage_subscription"):
            admin_state.pop(uid, None)
            return False
        ch = msg.text.strip()
        if not ch.startswith("@") and not ch.startswith("-"):
            ch = "@" + ch
        fs = CONFIG.setdefault("force_subscription", {"enabled": False, "channels": [], "message": ""})
        fs.setdefault("channels", []).append(ch)
        save_config(CONFIG)
        log_action(msg.from_user, "sub_channel_added", ch)
        await msg.answer(f"✅ تم إضافة {ch}")
        admin_state.pop(uid, None)
        return True

    if state == "awaiting_new_admin_id":
        if not has_perm(uid, "manage_admins"):
            admin_state.pop(uid, None)
            return False
        try:
            new_id = int(msg.text.strip())
        except ValueError:
            await msg.answer("⚠️ ID غير صحيح.")
            return True
        try:
            info = await bot.get_chat(new_id)
            name = info.first_name or f"ID:{new_id}"
        except Exception:
            name = f"ID:{new_id}"
        CONFIG.setdefault("admins", {})[str(new_id)] = {
            "name": name, "permissions": list(DEFAULT_ADMIN_PERMS),
            "added_at": datetime.utcnow().isoformat(), "added_by": uid}
        save_config(CONFIG)
        log_action(msg.from_user, "admin_added", str(new_id), f"الاسم: {name}")
        await msg.answer(f"✅ أضفت {name}")
        try:
            await bot.send_message(new_id, "🎉 تم تعيينك أدمن! أرسل /admin")
        except Exception:
            pass
        admin_state.pop(uid, None)
        return True

    admin_state.pop(uid, None)
    return False


async def handle_callback(cb):
    uid = cb.from_user.id
    data = cb.data
    try:
        if data in ("onb_emergency", "onb_backlog", "onb_start", "onb_regular"):
            situation = data.replace("onb_", "")
            update_progress(uid, situation=situation)
            await cb.message.edit_text(content.ONBOARDING_CURRICULUM,
                                        reply_markup=onboarding_curriculum_kb(), parse_mode="Markdown")
        elif data in ("onb_curr_sanaa", "onb_curr_aden"):
            curriculum = "sanaa" if data == "onb_curr_sanaa" else "aden"
            p = get_progress(uid)
            chat_sessions.pop(uid, None)
            update_progress(uid, curriculum=curriculum)
            if p.get("onboarding_done"):
                curriculum_name = {"sanaa": "صنعاء", "aden": "عدن"}[curriculum]
                await cb.message.edit_text(
                    f"✅ تم حفظ منهج مدرستك: *وزارة التربية ({curriculum_name})*.",
                    reply_markup=main_menu(), parse_mode="Markdown")
            else:
                await cb.message.edit_text(content.ONBOARDING_STREAM,
                                            reply_markup=onboarding_streams_kb(), parse_mode="Markdown")
        elif data in ("onb_sci", "onb_lit"):
            stream = "scientific" if data == "onb_sci" else "literary"
            update_progress(uid, stream=stream)
            await cb.message.edit_text(content.ONBOARDING_SUBJECT,
                                        reply_markup=onboarding_subjects_kb(stream),
                                        parse_mode="Markdown")
        elif data.startswith("onb_sub_"):
            subject = data.replace("onb_sub_", "")
            p = get_progress(uid)
            update_progress(uid, subject=subject, onboarding_done=True)
            situation_ar = {"emergency": "🔥 امتحان قريب", "backlog": "⏰ منهج متراكم",
                            "start": "📚 بداية", "regular": "✅ منتظم"}.get(p.get("situation"), "")
            stream_ar = "🔬 علمي" if p.get("stream") == "scientific" else "📖 أدبي"
            next_step = "اضغط 🔥 وضع الطوارئ" if p.get("situation") == "emergency" else "اضغط 📖 أفهم مادة"
            text = content.ONBOARDING_DONE.format(
                situation=situation_ar, curriculum=get_curriculum_ar(uid),
                stream=stream_ar, subject=subject, next_step=next_step)
            await cb.message.edit_text(text, reply_markup=main_menu(), parse_mode="Markdown")

        elif data == "home":
            chat_mode[uid] = False
            chat_sessions.pop(uid, None)
            await cb.message.edit_text("🏠 *القائمة الرئيسية*",
                                        reply_markup=main_menu(), parse_mode="Markdown")

        elif data == "pathway_understand":
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📚 المصادر", callback_data="sources")],
                [InlineKeyboardButton(text="🧠 برومبت الشرح", callback_data="understand_prompt")],
                [InlineKeyboardButton(text="🤖 دردشة مع AI", callback_data="chat_start")],
                [InlineKeyboardButton(text="🔙 رجوع", callback_data="home")]])
            await cb.message.edit_text(content.PATHWAY_UNDERSTAND, reply_markup=kb, parse_mode="Markdown")
        elif data == "pathway_train":
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🎯 بنك نجاح", url="https://www.e-najah.net/QsBank/")],
                [InlineKeyboardButton(text="❓ كويز DeepSeek", callback_data="quiz_prompt")],
                [InlineKeyboardButton(text="🔙 رجوع", callback_data="home")]])
            await cb.message.edit_text(content.PATHWAY_TRAIN, reply_markup=kb, parse_mode="Markdown")
        elif data == "pathway_review":
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💊 نقاط ضعفي", callback_data="weakness_prompt")],
                [InlineKeyboardButton(text="🔮 توقع أسئلة", callback_data="predict_prompt")],
                [InlineKeyboardButton(text="🔙 رجوع", callback_data="home")]])
            await cb.message.edit_text(content.PATHWAY_REVIEW, reply_markup=kb, parse_mode="Markdown")

        elif data == "sources":
            await cb.message.edit_text(content.SOURCES_TEXT, reply_markup=back_menu(),
                                        parse_mode="Markdown", disable_web_page_preview=True)
        elif data == "understand_prompt":
            p = get_progress(uid)
            text = content.UNDERSTAND_PROMPT.format(
                topic="[اكتب الموضوع]", subject=p.get("subject", "[المادة]"),
                stream="علمي" if p.get("stream") == "scientific" else "أدبي",
                curriculum_ar=get_curriculum_ar(uid))
            await cb.message.edit_text(text, reply_markup=back_menu(),
                                        parse_mode="Markdown", disable_web_page_preview=True)
        elif data == "quiz_prompt":
            p = get_progress(uid)
            text = content.QUIZ_PROMPT.format(
                topic="[اكتب الموضوع]", subject=p.get("subject", "[المادة]"),
                stream="علمي" if p.get("stream") == "scientific" else "أدبي",
                curriculum_ar=get_curriculum_ar(uid))
            await cb.message.edit_text(text, reply_markup=back_menu(),
                                        parse_mode="Markdown", disable_web_page_preview=True)
        elif data == "weakness_prompt":
            p = get_progress(uid)
            text = content.WEAKNESS_PROMPT.format(
                subject=p.get("subject", "[المادة]"), days=7,
                curriculum_ar=get_curriculum_ar(uid))
            await cb.message.edit_text(text, reply_markup=back_menu(),
                                        parse_mode="Markdown", disable_web_page_preview=True)
        elif data == "predict_prompt":
            p = get_progress(uid)
            text = content.PREDICT_PROMPT.format(
                subject=p.get("subject", "[المادة]"),
                stream="علمي" if p.get("stream") == "scientific" else "أدبي",
                curriculum_ar=get_curriculum_ar(uid))
            await cb.message.edit_text(text, reply_markup=back_menu(),
                                        parse_mode="Markdown", disable_web_page_preview=True)

        elif data == "my_progress":
            await show_progress(cb)
        elif data == "my_settings":
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 تغيير المادة", callback_data="onb_stream_reset")],
                [InlineKeyboardButton(text="🌍 تغيير المنهج", callback_data="curriculum_reset")],
                [InlineKeyboardButton(text="📊 تقدمي", callback_data="my_progress")],
                [InlineKeyboardButton(text="🔙 رجوع", callback_data="home")]])
            await cb.message.edit_text("⚙️ *إعداداتي*\n\nاختر:", reply_markup=kb, parse_mode="Markdown")
        elif data == "curriculum_reset":
            await cb.message.edit_text(content.ONBOARDING_CURRICULUM,
                                        reply_markup=onboarding_curriculum_kb(), parse_mode="Markdown")
        elif data == "onb_stream_reset":
            update_progress(uid, onboarding_done=False)
            await cb.message.edit_text(content.ONBOARDING_SITUATION,
                                        reply_markup=onboarding_situations_kb(),
                                        parse_mode="Markdown")

        elif data == "emergency":
            p = get_progress(uid)
            stream = p.get("stream", "scientific") or "scientific"
            await cb.message.edit_text(content.EMERGENCY_START,
                                        reply_markup=emergency_subjects_kb(stream),
                                        parse_mode="Markdown")

        elif data.startswith("em_sub_"):
            subject = data.replace("em_sub_", "")
            await show_emergency_plan(cb, subject)

        elif data.startswith("em_p1_"):
            subject = data.replace("em_p1_", "")
            p = get_progress(uid)
            prompt = content.EMERGENCY_DAY1_PROMPT.format(
                subject=subject, stream="علمي" if p.get("stream") == "scientific" else "أدبي",
                days=3, curriculum_ar=get_curriculum_ar(uid))
            await cb.message.answer(f"📖 *برومبت اليوم 1 — {subject}*\n\n`{prompt}`\n\n🔗 https://chat.deepseek.com",
                                    parse_mode="Markdown")
        elif data.startswith("em_p2_"):
            subject = data.replace("em_p2_", "")
            p = get_progress(uid)
            prompt = content.EMERGENCY_DAY2_PROMPT.format(
                subject=subject, stream="علمي" if p.get("stream") == "scientific" else "أدبي",
                days=3, curriculum_ar=get_curriculum_ar(uid))
            await cb.message.answer(f"🎯 *برومبت اليوم 2 — {subject}*\n\n`{prompt}`\n\n🔗 https://chat.deepseek.com",
                                    parse_mode="Markdown")
        elif data.startswith("em_p3_"):
            subject = data.replace("em_p3_", "")
            p = get_progress(uid)
            prompt = content.EMERGENCY_DAY3_PROMPT.format(
                subject=subject, stream="علمي" if p.get("stream") == "scientific" else "أدبي",
                days=3, curriculum_ar=get_curriculum_ar(uid))
            await cb.message.answer(f"🔁 *برومبت اليوم 3 — {subject}*\n\n`{prompt}`\n\n🔗 https://chat.deepseek.com",
                                    parse_mode="Markdown")

        elif data == "chat_start":
            chat_mode[uid] = True
            chat_sessions[uid] = get_user_model(uid).start_chat(history=[])
            await cb.message.edit_text(content.CHAT_WELCOME, reply_markup=chat_menu(), parse_mode="Markdown")
        elif data == "chat_exit":
            chat_mode[uid] = False
            chat_sessions.pop(uid, None)
            streak = update_streak(uid)
            await cb.message.edit_text(
                f"✅ خرجت من الدردشة.\n\n🔥 سلسلة: {streak}\n\n{random.choice(content.ENCOURAGEMENTS)}",
                reply_markup=main_menu(), parse_mode="Markdown")

        elif data.startswith("sa_more_"):
            msg_id = int(data.replace("sa_more_", ""))
            full = short_answers_cache.get(msg_id)
            if full:
                await cb.message.answer(full, parse_mode="Markdown")
            else:
                await cb.answer("الرد لم يعد متاحاً، اسأل من جديد.", show_alert=True)
        elif data.startswith("sa_ok_"):
            update_streak(uid)
            await cb.answer(f"🌟 {random.choice(content.ENCOURAGEMENTS)}", show_alert=True)
        elif data == "sa_new":
            await cb.answer("أرسل سؤالك الجديد 👇")

        elif data == "check_sub":
            ok, missing = await check_subscription(uid)
            if ok:
                await cb.message.edit_text("✅ تم التحقق! أرسل /start للبدء.")
            else:
                await cb.answer(f"❌ لم تشترك في {len(missing)} قناة.", show_alert=True)

        elif data == "admin_stats":
            if not has_perm(uid, "view_stats"):
                await cb.answer("⛔", show_alert=True)
                return
            await cb.message.answer(f"📊 المستخدمون: {len(users_cache)}")
        elif data == "admin_manage":
            if not has_perm(uid, "manage_admins"):
                await cb.answer("⛔", show_alert=True)
                return
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ إضافة أدمن", callback_data="admin_add")]])
            await cb.message.edit_text("👤 إدارة الأدمن", reply_markup=kb)
        elif data == "admin_add":
            if not has_perm(uid, "manage_admins"):
                await cb.answer("⛔", show_alert=True)
                return
            admin_state[uid] = "awaiting_new_admin_id"
            await cb.message.answer("أرسل ID الأدمن الجديد:")

        elif data.startswith("prompt_"):
            btn_id = data.replace("prompt_", "")
            for b in get_all_buttons():
                if b["id"] == btn_id and b.get("type") == "prompt":
                    await cb.message.answer(f"📋 *{b['text']}*\n\n`{b['value']}`", parse_mode="Markdown")
                    break

        await cb.answer()
    except Exception as e:
        logging.error(f"callback: {e}")
        try:
            await cb.answer("⚠️ خطأ")
        except Exception:
            pass


async def show_admin_panel(msg):
    uid = msg.from_user.id
    role = "👑 المالك" if is_owner(uid) else "👤 أدمن"
    kb = []
    if has_perm(uid, "view_stats"):
        kb.append([InlineKeyboardButton(text="📊 الإحصائيات", callback_data="admin_stats")])
    if has_perm(uid, "manage_admins"):
        kb.append([InlineKeyboardButton(text="👤 إدارة الأدمن", callback_data="admin_manage")])
    await msg.answer(f"🛠️ *لوحة التحكم*\n\nدورك: {role}",
                     reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="Markdown")


async def handle_chat_member(update):
    fs = CONFIG.get("force_subscription", {})
    if not fs.get("enabled"):
        return
    chat = update.chat
    monitored = False
    for ch in fs.get("channels", []):
        if ch == str(chat.id) or (chat.username and ch.lower() == f"@{chat.username}".lower()):
            monitored = True
            break
    if not monitored:
        return
    if (update.new_chat_member.status in ("member", "administrator")
            and update.old_chat_member.status in ("left", "kicked")):
        uid = update.new_chat_member.user.id
        if str(uid) not in users_cache:
            return
        ok, missing = await check_subscription(uid)
        if ok:
            try:
                await bot.send_message(uid, "✅ تم التحقق تلقائياً! أرسل /start",
                                        reply_markup=main_menu())
            except Exception:
                pass


async def main():
    global users_dirty, progress_dirty, audit_dirty
    users_cache.update(load_json("users.json", {"users": {}}).get("users", {}))
    progress_cache.update(load_json("progress.json", {"users": {}}).get("users", {}))
    audit_cache["logs"] = load_json("audit_log.json", {"logs": []}).get("logs", [])

    offset = get_offset()
    start_t = asyncio.get_event_loop().time()
    last_save = start_t

    while True:
        if asyncio.get_event_loop().time() - start_t > MAX_RUNTIME:
            save_offset(offset)
            break
        try:
            updates = await bot.get_updates(offset=offset, timeout=25,
                                            allowed_updates=["message", "callback_query", "chat_member"])
            for u in updates:
                offset = u.update_id + 1
                if u.message and u.message.text:
                    await handle_message(u.message)
                elif u.callback_query:
                    await handle_callback(u.callback_query)
                elif u.chat_member:
                    await handle_chat_member(u.chat_member)

            if asyncio.get_event_loop().time() - last_save > 300:
                save_offset(offset)
                if users_dirty:
                    save_json("users.json", {"users": users_cache})
                    users_dirty = False
                if progress_dirty:
                    save_json("progress.json", {"users": progress_cache})
                    progress_dirty = False
                if audit_dirty:
                    save_json("audit_log.json", audit_cache)
                    audit_dirty = False
                last_save = asyncio.get_event_loop().time()
        except Exception as e:
            logging.error(f"loop: {e}")
            await asyncio.sleep(5)

    if users_dirty:
        save_json("users.json", {"users": users_cache})
    if progress_dirty:
        save_json("progress.json", {"users": progress_cache})
    if audit_dirty:
        save_json("audit_log.json", audit_cache)
    await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())

