import os
import re
import json
import base64
import logging
import uuid
import random
import secrets
import subprocess
import asyncio
import requests
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from openai import OpenAI
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)
from telegram.request import HTTPXRequest
from PIL import Image, ImageDraw, ImageFont, ImageOps
import arabic_reshaper
from bidi.algorithm import get_display

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("BOT_TOKEN", "8724705061:AAFJo-SKACFZZLGvPcYC1PHfN091mpReYWE")

GAPGPT_BASE_URL = "https://api.gapgpt.app/v1"

# ===== کلید Z Image =====
GAPGPT_API_KEY = os.environ.get("GAPGPT_API_KEY", "sk-cxIUVwwLYXWPaW47noZw4xzTG5b4lPboQh3HTjZSC7fSG9aG")
ai_client = OpenAI(base_url=GAPGPT_BASE_URL, api_key=GAPGPT_API_KEY)

# ===== کلید GPT Image 2 =====
GPTIMAGE2_API_KEY = os.environ.get("GPTIMAGE2_API_KEY", "sk-0CfpL2Qb9VF4PibbWrYPGV2DjSvDMPFYxH66GGXD7ueQfA7u")
gpt2_client = OpenAI(base_url=GAPGPT_BASE_URL, api_key=GPTIMAGE2_API_KEY)

ADMIN_CONTACT = "@Behrad12123"

# محدودیت‌ها و قیمت‌ها
ZIMAGE_DAILY_LIMIT = 3
ZIMAGE_PRICE = 130
GPTIMAGE2_DAILY_LIMIT = 2
GPTIMAGE2_PRICE = 200
SUB_DURATION_DAYS = 30

SECRET_IMAGE_KEY = None       # کد مخفی یک‌بارمصرف Z Image؛ با دستور ادمین "k" ساخته میشه
SECRET_GPTIMAGE2_KEY = None   # کد مخفی یک‌بارمصرف GPT Image 2؛ با دستور ادمین "ch" ساخته میشه

BOT_ENABLED = True
MAINTENANCE_MESSAGE = "⛔ ربات در حال حاضر در دسترس نیست (خاموش برای آپدیت/تعمیرات). لطفاً بعداً امتحان کنید."

ADMIN_COMMAND_RE = re.compile(r"^(?:(\d+)|@(\w+))\s+([cbnmags])$")
ADMIN_PENDING_TARGET = None

BANNED_USERS = set()
RESTRICTED_SUPPORT = set()

FONT_MODE = "new"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FONT_PATH = os.path.join(BASE_DIR, "Vazirmatn-Bold.ttf")
EMOJI_FONT_PATH = os.path.join(BASE_DIR, "NotoEmoji.ttf")
WATERMARK_TEXT = "@Hapha44p_bot"
CHANNEL_ID = "@iyffggtjtvj3ieieieehewysy"
CHANNEL_USERNAME = "iyffggtjtvj3ieieieehewysy"
MAX_VIDEO_SECONDS = 30
STICKER_MAX_SECONDS = 3
STICKER_SIZE = 512
TARGET_WIDTH = 900
CHANNEL_POST_DELAY = 30

TMP_DIR = os.path.join(BASE_DIR, "tmp_bot")
os.makedirs(TMP_DIR, exist_ok=True)

# ===== فایل ذخیره‌سازی دائمی اشتراک‌ها (بعد از ریستارت هم می‌مونه) =====
DATA_FILE = os.path.join(BASE_DIR, "users_data.json")


def load_user_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"❌ load_user_data failed: {e}")
            return {}
    return {}


USER_DATA = load_user_data()


def save_user_data():
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(USER_DATA, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"❌ save_user_data failed: {e}")


def get_user_entry(user_id):
    uid = str(user_id)
    if uid not in USER_DATA:
        USER_DATA[uid] = {}
    return USER_DATA[uid]


def ensure_plan(entry, plan):
    if plan not in entry:
        entry[plan] = {"active": False, "expires": None, "used_today": 0, "last_reset": None}
    return entry[plan]


def is_plan_active(plan_data):
    if not plan_data.get("active"):
        return False
    expires = plan_data.get("expires")
    if not expires:
        return False
    try:
        exp_dt = datetime.fromisoformat(expires)
    except Exception:
        return False
    if datetime.now() > exp_dt:
        plan_data["active"] = False
        return False
    return True


def reset_daily_if_needed(plan_data):
    today = datetime.now().strftime("%Y-%m-%d")
    if plan_data.get("last_reset") != today:
        plan_data["used_today"] = 0
        plan_data["last_reset"] = today


def activate_subscription(user_id, plan):
    entry = get_user_entry(user_id)
    plan_data = ensure_plan(entry, plan)
    plan_data["active"] = True
    plan_data["expires"] = (datetime.now() + timedelta(days=SUB_DURATION_DAYS)).isoformat()
    plan_data["used_today"] = 0
    plan_data["last_reset"] = datetime.now().strftime("%Y-%m-%d")
    save_user_data()


def deactivate_subscriptions(user_id):
    entry = get_user_entry(user_id)
    for plan in ("zimage", "gptimage2"):
        plan_data = ensure_plan(entry, plan)
        plan_data["active"] = False
    save_user_data()


def check_and_consume_quota(user_id, plan):
    entry = get_user_entry(user_id)
    plan_data = ensure_plan(entry, plan)
    if not is_plan_active(plan_data):
        save_user_data()
        return False, "اشتراک شما فعال نیست یا منقضی شده. برای خرید با پشتیبانی تماس بگیرید."
    reset_daily_if_needed(plan_data)
    limit = ZIMAGE_DAILY_LIMIT if plan == "zimage" else GPTIMAGE2_DAILY_LIMIT
    if plan_data["used_today"] >= limit:
        save_user_data()
        return False, f"محدودیت روزانه شما ({limit} تصویر) تمام شده، فردا دوباره امتحان کنید."
    plan_data["used_today"] += 1
    save_user_data()
    return True, None


BUILD_QUEUE = asyncio.Queue()
CURRENTLY_BUILDING = False
BUILD_EXECUTOR = ThreadPoolExecutor(max_workers=1)

SUPPORT_MAP = {}

RANDOM_EMOJIS = ["😂", "🔥", "❤️", "😎", "✨", "😅", "👀", "💀", "🥲", "😹"]
STICKER_EMOJIS = RANDOM_EMOJIS + ["👍", "🎉"]

COLORS = {
    "c1": {"label": "⚪ سفید با حاشیه مشکی", "fill": (255, 255, 255), "outline": (0, 0, 0)},
    "c2": {"label": "⚫ مشکی با حاشیه سفید", "fill": (0, 0, 0), "outline": (255, 255, 255)},
    "c3": {"label": "⚪ فقط سفید", "fill": (255, 255, 255), "outline": None},
    "c4": {"label": "⚫ فقط مشکی", "fill": (0, 0, 0), "outline": None},
    "c5": {"label": "🟡 زرد با حاشیه مشکی", "fill": (255, 215, 0), "outline": (0, 0, 0)},
}

EFFECTS = {
    "colorful": {"label": "🎨 رنگی", "filter": None},
    "no_color": {"label": "🏁 بی‌رنگ", "filter": "hue=s=0"},
    "negative": {"label": "💧 نگاتیو", "filter": "negate"},
    "start_black": {"label": "⬛ شروع با مشکی", "filter": "fade=t=in:st=0:d=0.6:color=black"},
    "start_white": {"label": "⬜ شروع با سفید", "filter": "fade=t=in:st=0:d=0.6:color=white"},
    "start_bw": {"label": "🎬 شروع با سیاه و سفید", "filter": "hue=s=0,fade=t=in:st=0:d=0.6:color=black"},
}

# ===== افکت‌های جدا و چندتایی (چندتا رو هم‌زمان میشه انتخاب کرد) =====
EXTRA_EFFECTS = {
    "shift": {"label": "🔀 جابه‌جایی", "filter": "rgbashift=rh=8:bh=-8"},
    "fisheye": {"label": "🐟 چشم ماهی", "filter": "lenscorrection=k1=-0.6:k2=-0.3"},
    "rgb_split": {"label": "🌈 RGB", "filter": "rgbashift=rh=15:gh=0:bh=-15"},
    "shake": {"label": "📳 لرزش", "filter": "crop=w=iw-20:h=ih-20:x=10+8*sin(2*PI*t*6):y=10+8*cos(2*PI*t*6)"},
    "flash": {"label": "⚡ فلشر", "filter": "eq=eval=frame:brightness='0.25*sin(2*PI*t*8)'"},
    "blur": {"label": "🌫 تار", "filter": "gblur=sigma=6"},
    "warp": {"label": "🎡 تاب", "filter": "rotate=a='0.12*sin(2*PI*t*2)':c=black@0"},
}

DEFAULT_CHAT_DATA = {
    "position": "auto",
    "color": "c1",
    "effect": "colorful",
    "reverse": False,
    "mirror": False,
    "wide": False,
    "speed": 1.0,
}

SPEED_STEPS = [0.5, 0.75, 1.0, 1.25, 1.5, 2.0]


def is_admin_chat(chat):
    if chat is None:
        return False
    if chat.username and chat.username.lower() == CHANNEL_USERNAME.lower():
        return True
    return False


def reshape_fa(text):
    reshaped = arabic_reshaper.reshape(text)
    return get_display(reshaped)


def get_duration(path):
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrapped=1:nokey=1", path],
            capture_output=True, text=True, check=True
        )
        return float(result.stdout.strip())
    except Exception:
        return None


def resolve_position(position, w, h):
    if position != "auto":
        return position
    return "bottom_center"


def resize_to_target_width(img):
    w, h = img.size
    if w == TARGET_WIDTH:
        return img
    ratio = TARGET_WIDTH / w
    new_h = int(h * ratio)
    return img.resize((TARGET_WIDTH, new_h), Image.LANCZOS)


def calc_text_layout(w, h, text, color_key):
    dummy = Image.new("RGB", (w, h))
    draw = ImageDraw.Draw(dummy)
    color = COLORS[color_key]

    display_text = reshape_fa(text) if text else ""

    if FONT_MODE == "new":
        base_dim = w
        max_font_size = int(base_dim / 6.5)
        min_font_size = int(base_dim / 20)
        stroke_ratio = 6
        max_width = w * 0.92
        max_height = h * 0.30
    else:
        base_dim = min(w, h)
        max_font_size = int(base_dim / 9.5)
        min_font_size = int(base_dim / 26)
        stroke_ratio = 8
        max_width = w * 0.90
        max_height = h * 0.24

    if not display_text:
        font = ImageFont.truetype(FONT_PATH, max_font_size)
        return {"font": font, "stroke_w": 0, "lines": [], "line_heights": [], "total_height": 0, "color": color}

    for font_size in range(max_font_size, min_font_size, -1):
        font = ImageFont.truetype(FONT_PATH, font_size)
        stroke_w = max(3, font_size // stroke_ratio) if color["outline"] else 0
        bbox = draw.textbbox((0, 0), display_text, font=font, stroke_width=stroke_w)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        if tw <= max_width and th <= max_height:
            return {
                "font": font,
                "stroke_w": stroke_w,
                "lines": [display_text],
                "line_heights": [th],
                "total_height": th + 8,
                "color": color,
            }

    font_size = min_font_size
    font = ImageFont.truetype(FONT_PATH, font_size)
    stroke_w = max(3, font_size // stroke_ratio) if color["outline"] else 0
    words = display_text.split(" ")
    lines, line = [], ""
    for word in words:
        test = (line + " " + word).strip()
        bbox = draw.textbbox((0, 0), test, font=font, stroke_width=stroke_w)
        if bbox[2] - bbox[0] <= max_width:
            line = test
        else:
            if line:
                lines.append(line)
            line = word
    if line:
        lines.append(line)

    line_heights, total_height = [], 0
    for l in lines:
        bbox = draw.textbbox((0, 0), l, font=font, stroke_width=stroke_w)
        lh = bbox[3] - bbox[1]
        line_heights.append(lh)
        total_height += lh + 8

    return {
        "font": font,
        "stroke_w": stroke_w,
        "lines": lines,
        "line_heights": line_heights,
        "total_height": total_height,
        "color": color,
    }


def render_text(frame, layout, position, w, h):
    if not layout["lines"]:
        return
    draw = ImageDraw.Draw(frame)
    resolved_pos = resolve_position(position, w, h)
    v_part = resolved_pos.split("_")[0]

    if v_part == "top":
        y = int(h * 0.03)
    elif v_part == "middle":
        y = int((h - layout["total_height"]) / 2)
    else:
        y = int(h * 0.98) - layout["total_height"]

    font = layout["font"]
    stroke_w = layout["stroke_w"]
    color = layout["color"]

    for i, l in enumerate(layout["lines"]):
        bbox = draw.textbbox((0, 0), l, font=font, stroke_width=stroke_w)
        lw = bbox[2] - bbox[0]
        x = (w - lw) / 2
        draw.text(
            (x, y), l, font=font, fill=color["fill"],
            stroke_width=stroke_w, stroke_fill=color["outline"] if color["outline"] else None
        )
        y += layout["line_heights"][i] + 8


def build_watermark_overlay(h, emoji):
    font_size = int(h / 24)
    try:
        font = ImageFont.truetype(FONT_PATH, font_size)
    except Exception:
        font = ImageFont.load_default()
    try:
        emoji_font = ImageFont.truetype(EMOJI_FONT_PATH, font_size)
    except Exception:
        emoji_font = font

    dummy = Image.new("RGBA", (10, 10))
    dd = ImageDraw.Draw(dummy)
    tb = dd.textbbox((0, 0), WATERMARK_TEXT, font=font)
    tw, th = tb[2] - tb[0], tb[3] - tb[1]
    eb = dd.textbbox((0, 0), emoji, font=emoji_font)
    ew, eh = eb[2] - eb[0], eb[3] - eb[1]

    pad = 8
    total_w = tw + pad + ew
    total_h = max(th, eh) + 6

    text_img = Image.new("RGBA", (total_w, total_h), (0, 0, 0, 0))
    td = ImageDraw.Draw(text_img)
    td.text((0, 0), WATERMARK_TEXT, font=font, fill=(255, 255, 255, 150))
    td.text((tw + pad, 0), emoji, font=emoji_font, fill=(255, 255, 255, 190))

    return text_img.rotate(90, expand=True)


def apply_mirror(frame):
    return ImageOps.mirror(frame)


def apply_wide(frame):
    w, h = frame.size
    return frame.resize((int(w * 1.3), h))


def _save_image_from_response(response):
    """آدرس یا base64 عکس رو از پاسخ API میگیره و ذخیره میکنه."""
    item = response.data[0]
    path = os.path.join(TMP_DIR, f"{uuid.uuid4()}.png")
    url = getattr(item, "url", None)
    if url:
        r = requests.get(url, timeout=60)
        r.raise_for_status()
        with open(path, "wb") as f:
            f.write(r.content)
    else:
        b64 = getattr(item, "b64_json", None)
        if not b64:
            raise ValueError("پاسخ API عکسی نداشت")
        with open(path, "wb") as f:
            f.write(base64.b64decode(b64))
    return path


def generate_ai_image(prompt):
    """با GapGPT (مدل Z Image) یه تصویر از رو متن می‌سازه و دانلودش می‌کنه."""
    try:
        response = ai_client.images.generate(
            model="gapgpt/z-image",
            prompt=prompt,
            size="1024x1024",
        )
        return _save_image_from_response(response)
    except Exception as e:
        logger.error(f"❌ generate_ai_image (zimage) failed: {e}")
        return None


def generate_ai_image_gpt2(prompt):
    """با GapGPT (مدل GPT Image 2) یه تصویر از رو متن می‌سازه."""
    try:
        response = gpt2_client.images.generate(
            model="gpt-image-2",
            prompt=prompt,
            size="1024x1024",
        )
        return _save_image_from_response(response)
    except Exception as e:
        logger.error(f"❌ generate_ai_image_gpt2 failed: {e}")
        return None


def edit_ai_image(source_path, prompt):
    """با GapGPT (مدل GPT Image 2) عکس ورودی رو ویرایش می‌کنه."""
    png_path = None
    try:
        # API برای ویرایش، PNG واقعی می‌خواد - حتی اگه کاربر jpg فرستاده باشه تبدیلش می‌کنیم
        if source_path.lower().endswith(".png"):
            png_path = source_path
        else:
            png_path = os.path.splitext(source_path)[0] + "_conv.png"
            with Image.open(source_path) as im:
                im.convert("RGBA").save(png_path)

        with open(png_path, "rb") as img_file:
            img_file.name = "image.png"
            response = gpt2_client.images.edit(
                model="gpt-image-2",
                image=img_file,
                prompt=prompt,
                size="1024x1024",
            )
        return _save_image_from_response(response)
    except Exception as e:
        logger.error(f"❌ edit_ai_image failed: {e}")
        return None
    finally:
        for p in (source_path, png_path):
            try:
                if p and os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass


def extra_effects_label(chat_data):
    n = len(chat_data.get("active_effects", []))
    return f"🎭 افکت‌ها ({n} انتخاب‌شده)" if n else "🎭 افکت‌ها"


def main_menu_keyboard(chat_data):
    speed_label = f"{chat_data.get('speed', 1.0)}x"
    mirror_label = "✅" if chat_data.get("mirror") else "❌"
    reverse_label = "✅" if chat_data.get("reverse") else "❌"
    wide_label = "✅" if chat_data.get("wide") else "❌"
    color_label = COLORS.get(chat_data.get("color", "c1"))["label"]
    effect_label = EFFECTS.get(chat_data.get("effect", "colorful"))["label"]
    keyboard = [
        [InlineKeyboardButton("📍 موقعیت متن", callback_data="menu_position")],
        [InlineKeyboardButton(f"🎨 رنگ متن ({color_label})", callback_data="menu_color")],
        [InlineKeyboardButton(f"🎞 جلوه‌ها ({effect_label})", callback_data="menu_effect")],
        [InlineKeyboardButton(extra_effects_label(chat_data), callback_data="menu_extra_effects")],
        [InlineKeyboardButton(f"🔁 معکوس: {reverse_label}", callback_data="toggle_reverse"),
         InlineKeyboardButton(f"🪞 آینه: {mirror_label}", callback_data="toggle_mirror")],
        [InlineKeyboardButton(f"↔️ پهن: {wide_label}", callback_data="toggle_wide"),
         InlineKeyboardButton(f"🏃 سرعت: {speed_label}", callback_data="cycle_speed")],
        [InlineKeyboardButton("🚀 ساخت خروجی", callback_data="menu_output")],
        [InlineKeyboardButton("❌ لغو", callback_data="cancel")],
    ]
    return InlineKeyboardMarkup(keyboard)


def position_keyboard():
    order = [
        ("top_left", "↖"), ("top_center", "⬆"), ("top_right", "↗"),
        ("middle_left", "⬅"), ("middle_center", "⏹"), ("middle_right", "➡"),
        ("bottom_left", "↙"), ("bottom_center", "⬇"), ("bottom_right", "↘"),
    ]
    rows = []
    for i in range(0, 9, 3):
        rows.append([InlineKeyboardButton(lbl, callback_data=f"pos_{key}") for key, lbl in order[i:i + 3]])
    rows.append([InlineKeyboardButton("🔄 خودکار", callback_data="pos_auto")])
    rows.append([InlineKeyboardButton("🔙 برگشت", callback_data="back_main"),
                 InlineKeyboardButton("❌ لغو", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)


def color_keyboard():
    rows, row = [], []
    for key, info in COLORS.items():
        row.append(InlineKeyboardButton(info["label"], callback_data=f"color_{key}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("🔙 برگشت", callback_data="back_main"),
                 InlineKeyboardButton("❌ لغو", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)


def effect_keyboard():
    rows = []
    for key, info in EFFECTS.items():
        rows.append([InlineKeyboardButton(info["label"], callback_data=f"effect_{key}")])
    rows.append([InlineKeyboardButton("🔙 برگشت", callback_data="back_main"),
                 InlineKeyboardButton("❌ لغو", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)


def extra_effects_keyboard(chat_data):
    active = set(chat_data.get("active_effects", []))
    rows = []
    for key, info in EXTRA_EFFECTS.items():
        mark = "✅" if key in active else "⬜"
        rows.append([InlineKeyboardButton(f"{mark} {info['label']}", callback_data=f"toggle_fx_{key}")])
    rows.append([InlineKeyboardButton("🔙 برگشت", callback_data="back_main"),
                 InlineKeyboardButton("❌ لغو", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)


def output_keyboard():
    rows = [
        [InlineKeyboardButton("🎞 گیف", callback_data="output_gif")],
        [InlineKeyboardButton("🎯 استیکر", callback_data="output_sticker")],
        [InlineKeyboardButton("🔙 برگشت", callback_data="back_main"),
         InlineKeyboardButton("❌ لغو", callback_data="cancel")],
    ]
    return InlineKeyboardMarkup(rows)


def emoji_keyboard():
    rows, row = [], []
    for e in STICKER_EMOJIS:
        row.append(InlineKeyboardButton(e, callback_data=f"emoji_{e}"))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("🔙 برگشت", callback_data="back_output"),
                 InlineKeyboardButton("❌ لغو", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)


def ask_text_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚫 بدون متن، تبدیل به گیف کن", callback_data="skip_text")],
        [InlineKeyboardButton("🖼 ساخت تصویر هوشمند", callback_data="show_ai_plans")],
        [InlineKeyboardButton("📩 پیام به پشتیبانی", callback_data="support_msg")],
    ])


def support_only_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📩 پیام به پشتیبانی", callback_data="support_msg")],
    ])


def ai_text_choice_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ بله", callback_data="aitext_yes"),
        InlineKeyboardButton("❌ نه", callback_data="aitext_no"),
    ]])


def ai_plans_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("💳 خرید", callback_data="buy_plan")]])


def user_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📩 پیام به پشتیبانی", callback_data="support_msg")],
        [InlineKeyboardButton("🖼 ساخت تصویر", callback_data="ai_menu_start")],
    ])


def gpt2_mode_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🖼 ساخت تصویر", callback_data="aimode_generate"),
        InlineKeyboardButton("✏️ ویرایش تصویر", callback_data="aimode_edit"),
    ]])


async def notify_channel(bot, message):
    try:
        sent = await bot.send_message(CHANNEL_ID, message)
        return sent
    except Exception as e:
        logger.error(f"❌ channel notify FAILED: {type(e).__name__}: {e}")
        return None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not BOT_ENABLED:
        await update.message.reply_text(MAINTENANCE_MESSAGE)
        return
    user = update.effective_user
    if user.id in BANNED_USERS:
        await update.message.reply_text("⛔ شما از استفاده از این ربات محروم شده‌اید.")
        return
    name = user.first_name or "کاربر"
    username_tag = f"(@{user.username})" if user.username else ""
    text = (f"سلام {name} عزیز {username_tag} به ربات ساخت گیف خوش آمدید 🎉\n"
            f"لطفا تصویر یا عکس یا گیف‌تون رو بفرستید.")
    await update.message.reply_text(text)
    await notify_channel(context.bot, f"👤 {name} {username_tag or 'ندارد'} بات رو استارت کرد")


async def delayed_channel_post(bot, output_path, output_type, emoji, uname, text_used, media_path):
    await asyncio.sleep(CHANNEL_POST_DELAY)
    caption = f"🎬 {uname} یک گیف ساخت.\nمتن: {text_used}"
    try:
        with open(output_path, "rb") as f:
            if output_type == "sticker":
                await bot.send_sticker(CHANNEL_ID, sticker=f, emoji=emoji)
                await notify_channel(bot, caption)
            else:
                await bot.send_animation(CHANNEL_ID, f, caption=caption)
    except Exception as e:
        logger.error(f"❌ delayed_channel_post failed: {e}")
    finally:
        try:
            if media_path:
                os.remove(media_path)
        except Exception:
            pass
        try:
            os.remove(output_path)
        except Exception:
            pass


async def gif_worker(application):
    global CURRENTLY_BUILDING
    bot = application.bot
    while True:
        job = await BUILD_QUEUE.get()
        CURRENTLY_BUILDING = True
        chat_id = job["chat_id"]
        settings_msg_id = job.get("settings_msg_id")
        ask_msg_id = job.get("ask_msg_id")
        data = job["data"]

        try:
            if settings_msg_id:
                await bot.edit_message_text(chat_id=chat_id, message_id=settings_msg_id,
                                             text="⏳ در حال ساخت هستیم، لطفا کمی صبر کنید...")
            else:
                await bot.send_message(chat_id, "⏳ در حال ساخت هستیم، لطفا کمی صبر کنید...")

            if data.get("media_type") == "video":
                await bot.send_message(chat_id, "⏳ این کار ممکن است چند ثانیه تا چند دقیقه طول بکشد، لطفاً صبر کنید...")

            loop = asyncio.get_running_loop()
            output_path, emoji = await loop.run_in_executor(BUILD_EXECUTOR, process_media, data)

            for mid in (ask_msg_id, settings_msg_id):
                if not mid:
                    continue
                try:
                    await bot.delete_message(chat_id, mid)
                except Exception:
                    pass

            with open(output_path, "rb") as f:
                if data.get("output_type") == "sticker":
                    await bot.send_sticker(chat_id, sticker=f, emoji=emoji)
                else:
                    await bot.send_animation(chat_id, f)

            text_used = data.get("text") or "بدون متن"
            uname = job.get("user_name", "کاربر")
            asyncio.create_task(delayed_channel_post(
                bot, output_path, data.get("output_type"), emoji, uname, text_used, data.get("media_path")
            ))

        except Exception as e:
            logger.error(f"❌ gif_worker job failed: {e}")
            try:
                await bot.send_message(chat_id, "❌ متاسفانه تو ساخت گیف مشکلی پیش اومد، دوباره امتحان کنید.")
            except Exception:
                pass
        finally:
            try:
                application.chat_data.get(chat_id, {}).clear()
            except Exception:
                pass
            CURRENTLY_BUILDING = False
            BUILD_QUEUE.task_done()


async def post_init(application):
    asyncio.create_task(gif_worker(application))
    logger.info("🧵 gif queue worker started")


async def extract_media(msg):
    if msg.photo:
        return await msg.photo[-1].get_file(), "photo", "jpg"
    if msg.video:
        return await msg.video.get_file(), "video", "mp4"
    if msg.animation:
        return await msg.animation.get_file(), "animation", "mp4"
    if msg.sticker:
        st = msg.sticker
        if st.is_animated:
            return None, "unsupported_tgs", None
        if st.is_video:
            return await st.get_file(), "video", "webm"
        return await st.get_file(), "photo", "webp"
    return None, None, None


async def begin_flow(chat_id, context, local_path, media_type, owner_id, user_name):
    chat_data = context.chat_data
    chat_data.clear()
    chat_data.update(DEFAULT_CHAT_DATA)
    chat_data["active_effects"] = []
    chat_data["media_path"] = local_path
    chat_data["media_type"] = media_type
    chat_data["owner_id"] = owner_id
    chat_data["awaiting_text"] = True
    chat_data["chat_id"] = chat_id
    chat_data["user_name"] = user_name

    msg1 = await context.bot.send_message(chat_id, "📝 متن خودتون رو بفرستید:", reply_markup=ask_text_keyboard())
    msg2 = await context.bot.send_message(chat_id, "⚙️ تنظیمات (بعد از ارسال متن، اینجا فعال می‌شود)")
    chat_data["ask_msg_id"] = msg1.message_id
    chat_data["settings_msg_id"] = msg2.message_id


async def reject_long_video(chat_id, context, local_path):
    await context.bot.send_message(chat_id, f"⛔ ویدیو باید کمتر از {MAX_VIDEO_SECONDS} ثانیه باشه.")
    try:
        os.remove(local_path)
    except Exception:
        pass


async def handle_media_private(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not BOT_ENABLED:
        await update.message.reply_text(MAINTENANCE_MESSAGE)
        return
    if update.effective_user.id in BANNED_USERS:
        return

    msg = update.message
    chat_id = update.effective_chat.id
    owner_id = update.effective_user.id
    user_name = update.effective_user.first_name or "کاربر"
    chat_data = context.chat_data

    # ===== عکس مرجع برای ویرایش با GPT Image 2 =====
    if chat_data.get("awaiting_edit_photo"):
        if owner_id != chat_data.get("owner_id"):
            return
        if not msg.photo:
            await msg.reply_text("⛔ لطفا یک عکس بفرستید.")
            return
        chat_data["awaiting_edit_photo"] = False
        file = await msg.photo[-1].get_file()
        local_path = os.path.join(TMP_DIR, f"{uuid.uuid4()}.jpg")
        await file.download_to_drive(local_path)
        chat_data["edit_source_path"] = local_path
        chat_data["awaiting_edit_prompt"] = True
        await msg.reply_text("✍️ توضیح بدید چه تغییری روی عکس اعمال بشه:")
        return

    file, media_type, ext = await extract_media(msg)

    if media_type == "unsupported_tgs":
        await msg.reply_text("⛔ این نوع استیکر (انیمیشن Lottie) پشتیبانی نمیشه. استیکر ویدیویی، عکس یا ویدیو بفرستید.")
        return
    if not media_type:
        return

    local_path = os.path.join(TMP_DIR, f"{uuid.uuid4()}.{ext}")
    await file.download_to_drive(local_path)

    if media_type == "video":
        duration = get_duration(local_path)
        if duration is not None and duration > MAX_VIDEO_SECONDS:
            await reject_long_video(chat_id, context, local_path)
            return

    await begin_flow(chat_id, context, local_path, media_type, owner_id, user_name)


async def handle_group_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not BOT_ENABLED:
        await update.message.reply_text(MAINTENANCE_MESSAGE)
        return
    if update.effective_user.id in BANNED_USERS:
        return

    msg = update.message
    target = msg.reply_to_message
    if not target:
        return
    chat_id = update.effective_chat.id
    owner_id = update.effective_user.id
    user_name = update.effective_user.first_name or "کاربر"

    file, media_type, ext = await extract_media(target)

    if media_type == "unsupported_tgs":
        await msg.reply_text("⛔ این نوع استیکر پشتیبانی نمیشه.")
        return
    if not media_type:
        return

    local_path = os.path.join(TMP_DIR, f"{uuid.uuid4()}.{ext}")
    await file.download_to_drive(local_path)

    if media_type == "video":
        duration = get_duration(local_path)
        if duration is not None and duration > MAX_VIDEO_SECONDS:
            await reject_long_video(chat_id, context, local_path)
            return

    await begin_flow(chat_id, context, local_path, media_type, owner_id, user_name)


async def finalize_ai_image(chat_id, context, chat_data, text):
    job_data = dict(DEFAULT_CHAT_DATA)
    job_data["media_path"] = chat_data.get("ai_image_path")
    job_data["media_type"] = "photo"
    job_data["text"] = text
    job_data["output_type"] = "gif"
    job = {
        "chat_id": chat_id,
        "ask_msg_id": None,
        "settings_msg_id": None,
        "user_name": chat_data.get("user_name", "کاربر"),
        "data": job_data,
    }

    if CURRENTLY_BUILDING or BUILD_QUEUE.qsize() > 0:
        position = BUILD_QUEUE.qsize() + 1
        await context.bot.send_message(chat_id, f"🕐 شما در صف ساخت گیف قرار دارید (موقعیت {position})، لطفا صبر کنید...")

    await BUILD_QUEUE.put(job)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global ADMIN_PENDING_TARGET, FONT_MODE, SECRET_IMAGE_KEY, SECRET_GPTIMAGE2_KEY, BOT_ENABLED
    chat_data = context.chat_data
    user_id = update.effective_user.id
    text_raw = update.message.text or ""
    stripped = text_raw.strip()

    if is_admin_chat(update.effective_chat):
        if stripped == "off":
            BOT_ENABLED = False
            await update.message.reply_text("🔴 ربات خاموش شد؛ تا وقتی «on» بزنید به همه پیام «در دسترس نیست» میده.")
            return
        if stripped == "on":
            BOT_ENABLED = True
            await update.message.reply_text("🟢 ربات روشن شد.")
            return
        if stripped == "m":
            FONT_MODE = "old"
            await update.message.reply_text("🔤 حالت فونت روی قدیمی تنظیم شد.")
            return
        if stripped == "o":
            FONT_MODE = "new"
            await update.message.reply_text("🔤 حالت فونت روی جدید (درشت) تنظیم شد.")
            return
        if stripped == "k":
            SECRET_IMAGE_KEY = secrets.token_hex(4)
            await update.message.reply_text(
                f"🔑 کد مخفی Z Image ساخته شد:\n{SECRET_IMAGE_KEY}\n\n(فقط یک‌بار قابل استفاده‌ست، بعدش باید دوباره بسازی)"
            )
            return
        if stripped == "ch":
            SECRET_GPTIMAGE2_KEY = secrets.token_hex(4)
            await update.message.reply_text(
                f"🔑 کد مخفی GPT Image 2 ساخته شد:\n{SECRET_GPTIMAGE2_KEY}\n\n(فقط یک‌بار قابل استفاده‌ست، بعدش باید دوباره بسازی)"
            )
            return

        m = ADMIN_COMMAND_RE.match(stripped)
        if m:
            id_part, username_part, action = m.groups()
            target_id = None
            if id_part:
                target_id = int(id_part)
            elif username_part:
                try:
                    chat = await context.bot.get_chat(f"@{username_part}")
                    target_id = chat.id
                except Exception as e:
                    await update.message.reply_text(f"❌ نتونستم کاربر @{username_part} رو پیدا کنم: {e}")
                    return

            if action == "c":
                RESTRICTED_SUPPORT.add(target_id)
                await update.message.reply_text(f"✅ کاربر {target_id} از قابلیت پشتیبانی محدود شد.")
            elif action == "b":
                BANNED_USERS.add(target_id)
                await update.message.reply_text(f"⛔ کاربر {target_id} از کل ربات بن شد.")
            elif action == "n":
                BANNED_USERS.discard(target_id)
                await update.message.reply_text(f"✅ کاربر {target_id} از بن خارج شد.")
            elif action == "m":
                ADMIN_PENDING_TARGET = target_id
                await update.message.reply_text("✍️ پیامتون رو بنویسید تا برای کاربر ارسال بشه:")
            elif action == "a":
                activate_subscription(target_id, "zimage")
                await update.message.reply_text(f"✅ اشتراک Z Image برای کاربر {target_id} فعال شد (۱ ماهه).")
                try:
                    await context.bot.send_message(
                        target_id,
                        "🎉 اشتراک Z Image شما فعال شد!\nبرای استفاده تایپ کنید: منو"
                    )
                except Exception:
                    pass
            elif action == "g":
                activate_subscription(target_id, "gptimage2")
                await update.message.reply_text(f"✅ اشتراک GPT Image 2 برای کاربر {target_id} فعال شد (۱ ماهه).")
                try:
                    await context.bot.send_message(
                        target_id,
                        "🎉 اشتراک GPT Image 2 شما فعال شد!\nبرای استفاده تایپ کنید: منو"
                    )
                except Exception:
                    pass
            elif action == "s":
                deactivate_subscriptions(target_id)
                await update.message.reply_text(f"✅ اشتراک‌های کاربر {target_id} غیرفعال شد.")
            return

        if ADMIN_PENDING_TARGET:
            target_id = ADMIN_PENDING_TARGET
            ADMIN_PENDING_TARGET = None
            try:
                await context.bot.send_message(target_id, text_raw)
                await update.message.reply_text("✅ پیام برای کاربر ارسال شد.")
            except Exception as e:
                await update.message.reply_text(f"❌ ارسال پیام ناموفق بود: {e}")
            return

    if not BOT_ENABLED:
        await update.message.reply_text(MAINTENANCE_MESSAGE)
        return

    if user_id in BANNED_USERS:
        return

    # ===== منوی کاربر (فقط برای دارندگان اشتراک فعال) =====
    if stripped == "منو" and update.effective_chat.type == "private":
        entry = get_user_entry(user_id)
        z = ensure_plan(entry, "zimage")
        g = ensure_plan(entry, "gptimage2")
        has_active = is_plan_active(z) or is_plan_active(g)
        save_user_data()
        if not has_active:
            await update.message.reply_text(f"⛔ شما اشتراک فعالی ندارید.\nبرای خرید به {ADMIN_CONTACT} پیام بدید.")
            return
        await update.message.reply_text("📋 منو:", reply_markup=user_menu_keyboard())
        return

    # ===== قابلیت مخفی ساخت تصویر با کد یک‌بار مصرف =====
    if stripped.lower() == "p" and not chat_data.get("awaiting_text") and not chat_data.get("awaiting_support_message"):
        chat_data["awaiting_secret_code"] = True
        chat_data["owner_id"] = user_id
        chat_data["chat_id"] = update.effective_chat.id
        chat_data["user_name"] = update.effective_user.first_name or "کاربر"
        await update.message.reply_text("🔐 کد مخفی رو وارد کنید:")
        return

    if chat_data.get("awaiting_secret_code"):
        if user_id != chat_data.get("owner_id"):
            return
        if SECRET_IMAGE_KEY and stripped == SECRET_IMAGE_KEY:
            SECRET_IMAGE_KEY = None
            chat_data["awaiting_secret_code"] = False
            chat_data["awaiting_image_prompt"] = True
            chat_data["image_model"] = "zimage"
            chat_data["via_secret_code"] = True
            await update.message.reply_text("🖼 چه تصویری می‌خواید بسازم؟ توضیحش رو بنویسید:")
        elif SECRET_GPTIMAGE2_KEY and stripped == SECRET_GPTIMAGE2_KEY:
            SECRET_GPTIMAGE2_KEY = None
            chat_data["awaiting_secret_code"] = False
            chat_data["awaiting_image_prompt"] = True
            chat_data["image_model"] = "gptimage2"
            chat_data["via_secret_code"] = True
            await update.message.reply_text("🖼 چه تصویری می‌خواید بسازم؟ توضیحش رو بنویسید:")
        else:
            await update.message.reply_text("❌ کد مخفی اشتباهه.")
            chat_data["awaiting_secret_code"] = False
        return

    # ===== ساخت تصویر از روی متن (از منو یا کد مخفی) =====
    if chat_data.get("awaiting_image_prompt"):
        if user_id != chat_data.get("owner_id"):
            return
        chat_data["awaiting_image_prompt"] = False
        model = chat_data.get("image_model", "zimage")

        # اگه از مسیر منو (اشتراک واقعی) اومده، اعتبار روزانه رو چک و کم کن.
        # مسیر کد مخفی یک‌بار مصرف، خارج از سیستم اعتبار روزانه‌ست.
        if chat_data.get("via_secret_code"):
            chat_data["via_secret_code"] = False
        else:
            ok, err = check_and_consume_quota(user_id, model)
            if not ok:
                await update.message.reply_text(f"❌ {err}")
                return

        await update.message.reply_text("⏳ در حال ساخت تصویر هستم، لطفا صبر کنید...")
        loop = asyncio.get_running_loop()
        if model == "gptimage2":
            image_path = await loop.run_in_executor(BUILD_EXECUTOR, generate_ai_image_gpt2, text_raw)
        else:
            image_path = await loop.run_in_executor(BUILD_EXECUTOR, generate_ai_image, text_raw)
        if not image_path:
            await update.message.reply_text("❌ ساخت تصویر ناموفق بود، دوباره امتحان کنید.")
            return
        chat_data["ai_image_path"] = image_path
        await update.message.reply_text("آیا می‌خواهید روی تصویرتون متن باشه؟", reply_markup=ai_text_choice_keyboard())
        return

    # ===== ویرایش تصویر با GPT Image 2 =====
    if chat_data.get("awaiting_edit_prompt"):
        if user_id != chat_data.get("owner_id"):
            return
        chat_data["awaiting_edit_prompt"] = False
        ok, err = check_and_consume_quota(user_id, "gptimage2")
        if not ok:
            await update.message.reply_text(f"❌ {err}")
            return
        await update.message.reply_text("⏳ در حال ویرایش تصویر هستم، لطفا صبر کنید...")
        loop = asyncio.get_running_loop()
        image_path = await loop.run_in_executor(
            BUILD_EXECUTOR, edit_ai_image, chat_data["edit_source_path"], text_raw
        )
        if not image_path:
            await update.message.reply_text("❌ ویرایش تصویر ناموفق بود، دوباره امتحان کنید.")
            return
        chat_data["ai_image_path"] = image_path
        await update.message.reply_text("آیا می‌خواهید روی تصویرتون متن باشه؟", reply_markup=ai_text_choice_keyboard())
        return

    if chat_data.get("awaiting_ai_text"):
        if user_id != chat_data.get("owner_id"):
            return
        chat_data["awaiting_ai_text"] = False
        await finalize_ai_image(update.effective_chat.id, context, chat_data, text_raw)
        return
    # ===== پایان قابلیت AI =====

    if chat_data.get("awaiting_support_message"):
        if user_id != chat_data.get("owner_id"):
            return
        if user_id in RESTRICTED_SUPPORT:
            await update.message.reply_text("⛔ شما نمی‌تونید از این قابلیت استفاده کنید.")
            chat_data["awaiting_support_message"] = False
            return
        user = update.effective_user
        name = user.first_name or "کاربر"
        uname = f"@{user.username}" if user.username else "ندارد"
        sent = await notify_channel(
            context.bot,
            f"📩 پیام پشتیبانی از {name} ({uname}) | آیدی: {user_id}:\n\n{text_raw}"
        )
        if sent:
            SUPPORT_MAP[sent.message_id] = (update.effective_chat.id, name)
            await update.message.reply_text("✅ پیامتون به پشتیبانی ارسال شد، جواب رو همینجا می‌فرستیم.")
        else:
            await update.message.reply_text("❌ ارسال پیام ناموفق بود، دوباره امتحان کنید.")
        chat_data["awaiting_support_message"] = False
        return

    if not chat_data.get("awaiting_text"):
        return
    if user_id != chat_data.get("owner_id"):
        return

    chat_data["text"] = text_raw
    chat_data["awaiting_text"] = False

    await context.bot.edit_message_text(
        chat_id=chat_data["chat_id"],
        message_id=chat_data["settings_msg_id"],
        text="⚙️ تنظیمات رو انتخاب کنید، در پایان روی «🚀 ساخت خروجی» بزنید:",
        reply_markup=main_menu_keyboard(chat_data),
    )


async def handle_support_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.reply_to_message or not msg.text:
        return
    entry = SUPPORT_MAP.get(msg.reply_to_message.message_id)
    if not entry:
        return
    user_chat_id, name = entry
    try:
        await context.bot.send_message(user_chat_id, f"📩 پاسخ پشتیبانی:\n\n{msg.text}")
        await msg.reply_text("✅ پاسخ برای کاربر ارسال شد.")
    except Exception as e:
        logger.error(f"❌ support reply forward failed: {e}")


async def do_cancel(chat_id, context, chat_data):
    try:
        path = chat_data.get("media_path")
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:
        pass
    try:
        await context.bot.delete_message(chat_id, chat_data.get("ask_msg_id"))
    except Exception:
        pass
    try:
        await context.bot.edit_message_text(chat_id=chat_id, message_id=chat_data["settings_msg_id"], text="لغو شد ❌")
    except Exception:
        pass
    chat_data.clear()


async def build_and_send(chat_id, context, chat_data):
    job = {
        "chat_id": chat_id,
        "ask_msg_id": chat_data.get("ask_msg_id"),
        "settings_msg_id": chat_data.get("settings_msg_id"),
        "user_name": chat_data.get("user_name", "کاربر"),
        "data": dict(chat_data),
    }

    if CURRENTLY_BUILDING or BUILD_QUEUE.qsize() > 0:
        position = BUILD_QUEUE.qsize() + 1
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=chat_data["settings_msg_id"],
            text=f"🕐 شما در صف ساخت گیف قرار دارید (موقعیت {position})، لطفا صبر کنید...",
            reply_markup=support_only_keyboard(),
        )
    else:
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=chat_data["settings_msg_id"],
            text="⏳ در حال ساخت هستیم، لطفا کمی صبر کنید...",
            reply_markup=support_only_keyboard(),
        )

    await BUILD_QUEUE.put(job)


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_data = context.chat_data
    data = query.data
    chat_id = query.message.chat_id

    if query.from_user.id in BANNED_USERS:
        await query.answer("⛔ شما از استفاده از این ربات محروم شده‌اید.", show_alert=True)
        return

    if not BOT_ENABLED:
        await query.answer(MAINTENANCE_MESSAGE, show_alert=True)
        return

    # ===== دکمه‌های مربوط به پلن‌ها و منوی مستقل کاربر: نیازی به مالکیت قبلی چت ندارن =====
    self_service_buttons = {"show_ai_plans", "buy_plan", "ai_menu_start", "aimode_generate", "aimode_edit"}
    if data in self_service_buttons:
        await query.answer()
        chat_data["owner_id"] = query.from_user.id
        chat_data["chat_id"] = chat_id
        chat_data["user_name"] = query.from_user.first_name or "کاربر"

        if data == "show_ai_plans":
            text = (
                "🎨 Z Image\n"
                f"• {ZIMAGE_DAILY_LIMIT} تصویر در روز\n"
                "• بدون قابلیت ویرایش تصویر\n"
                f"• قیمت: {ZIMAGE_PRICE} (اشتراک ۱ ماهه)\n\n"
                "🖌 GPT Image 2\n"
                f"• {GPTIMAGE2_DAILY_LIMIT} ویرایش/ساخت تصویر در روز\n"
                "• قابلیت ویرایش تصویر\n"
                f"• قیمت: {GPTIMAGE2_PRICE} (اشتراک ۱ ماهه)\n\n"
                "تمام اشتراک‌ها یک ماهه هستند."
            )
            await context.bot.send_message(chat_id, text, reply_markup=ai_plans_keyboard())
            return

        if data == "buy_plan":
            await context.bot.send_message(chat_id, f"برای خرید به آیدی زیر پیام دهید:\n{ADMIN_CONTACT}")
            return

        if data == "ai_menu_start":
            entry = get_user_entry(query.from_user.id)
            z = ensure_plan(entry, "zimage")
            g = ensure_plan(entry, "gptimage2")
            g_active = is_plan_active(g)
            z_active = is_plan_active(z)
            save_user_data()
            if g_active:
                await context.bot.send_message(
                    chat_id, "می‌خواهید تصویر بسازید یا تصویر را ویرایش کنید؟",
                    reply_markup=gpt2_mode_keyboard()
                )
            elif z_active:
                chat_data["image_model"] = "zimage"
                chat_data["awaiting_image_prompt"] = True
                await context.bot.send_message(chat_id, "🖼 چه تصویری می‌خواید بسازم؟ توضیحش رو بنویسید:")
            else:
                await context.bot.send_message(chat_id, f"⛔ اشتراک فعالی ندارید.\nبرای خرید به {ADMIN_CONTACT} پیام بدید.")
            return

        if data == "aimode_generate":
            chat_data["image_model"] = "gptimage2"
            chat_data["awaiting_image_prompt"] = True
            await context.bot.send_message(chat_id, "🖼 چه تصویری می‌خواید بسازم؟ توضیحش رو بنویسید:")
            return

        if data == "aimode_edit":
            chat_data["image_model"] = "gptimage2"
            chat_data["awaiting_edit_photo"] = True
            await context.bot.send_message(chat_id, "🖼 عکستون رو بفرستید:")
            return

    if data in ("aitext_yes", "aitext_no"):
        if query.from_user.id != chat_data.get("owner_id"):
            await query.answer("این دکمه‌ها برای شما نیست ❌", show_alert=True)
            return
        await query.answer()
        if data == "aitext_yes":
            chat_data["awaiting_ai_text"] = True
            await context.bot.send_message(chat_id, "📝 متنتون رو بفرستید:")
        else:
            await finalize_ai_image(chat_id, context, chat_data, "")
        return

    if query.from_user.id != chat_data.get("owner_id"):
        await query.answer("این دکمه‌ها برای شما نیست ❌", show_alert=True)
        return
    await query.answer()

    main_text = "⚙️ تنظیمات رو انتخاب کنید، در پایان روی «🚀 ساخت خروجی» بزنید:"

    if data == "cancel":
        await do_cancel(chat_id, context, chat_data)
        return

    if data == "support_msg":
        if query.from_user.id in RESTRICTED_SUPPORT:
            await context.bot.send_message(chat_id, "⛔ شما نمی‌تونید از این قابلیت استفاده کنید.")
            return
        chat_data["awaiting_support_message"] = True
        await context.bot.send_message(chat_id, "✍️ پیامتون رو بنویسید، براتون به پشتیبانی می‌فرستیم:")
        return

    if data == "skip_text":
        chat_data["text"] = ""
        chat_data["awaiting_text"] = False
        await context.bot.edit_message_text(chat_id=chat_id, message_id=chat_data["settings_msg_id"],
                                             text=main_text, reply_markup=main_menu_keyboard(chat_data))
        return

    if data == "back_main":
        await context.bot.edit_message_text(chat_id=chat_id, message_id=chat_data["settings_msg_id"],
                                             text=main_text, reply_markup=main_menu_keyboard(chat_data))
        return

    if data == "back_output":
        await context.bot.edit_message_text(chat_id=chat_id, message_id=chat_data["settings_msg_id"],
                                             text="خروجی رو انتخاب کنید:", reply_markup=output_keyboard())
        return

    if data == "menu_position":
        await context.bot.edit_message_text(chat_id=chat_id, message_id=chat_data["settings_msg_id"],
                                             text="📍 موقعیت متن رو انتخاب کنید (یا خودکار):", reply_markup=position_keyboard())
        return

    if data == "menu_color":
        await context.bot.edit_message_text(chat_id=chat_id, message_id=chat_data["settings_msg_id"],
                                             text="🎨 رنگ متن رو انتخاب کنید:", reply_markup=color_keyboard())
        return

    if data == "menu_effect":
        await context.bot.edit_message_text(chat_id=chat_id, message_id=chat_data["settings_msg_id"],
                                             text="🎞 یه جلوه انتخاب کنید:", reply_markup=effect_keyboard())
        return

    if data == "menu_extra_effects":
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=chat_data["settings_msg_id"],
            text="🎭 افکت‌های مورد نظرتون رو انتخاب کنید (میتونید چندتا رو هم‌زمان بزنید):",
            reply_markup=extra_effects_keyboard(chat_data)
        )
        return

    if data.startswith("toggle_fx_"):
        key = data.replace("toggle_fx_", "")
        active = chat_data.setdefault("active_effects", [])
        if key in active:
            active.remove(key)
        else:
            active.append(key)
        await context.bot.edit_message_reply_markup(chat_id=chat_id, message_id=chat_data["settings_msg_id"],
                                                      reply_markup=extra_effects_keyboard(chat_data))
        return

    if data == "menu_output":
        await context.bot.edit_message_text(chat_id=chat_id, message_id=chat_data["settings_msg_id"],
                                             text="خروجی رو انتخاب کنید:", reply_markup=output_keyboard())
        return

    if data.startswith("pos_"):
        chat_data["position"] = data.replace("pos_", "")
        await context.bot.edit_message_text(chat_id=chat_id, message_id=chat_data["settings_msg_id"],
                                             text=main_text, reply_markup=main_menu_keyboard(chat_data))
        return

    if data.startswith("color_"):
        chat_data["color"] = data.replace("color_", "")
        await context.bot.edit_message_text(chat_id=chat_id, message_id=chat_data["settings_msg_id"],
                                             text=main_text, reply_markup=main_menu_keyboard(chat_data))
        return

    if data.startswith("effect_"):
        chat_data["effect"] = data.replace("effect_", "")
        await context.bot.edit_message_text(chat_id=chat_id, message_id=chat_data["settings_msg_id"],
                                             text=main_text, reply_markup=main_menu_keyboard(chat_data))
        return

    if data == "toggle_reverse":
        chat_data["reverse"] = not chat_data.get("reverse", False)
        await context.bot.edit_message_reply_markup(chat_id=chat_id, message_id=chat_data["settings_msg_id"],
                                                      reply_markup=main_menu_keyboard(chat_data))
        return

    if data == "toggle_mirror":
        chat_data["mirror"] = not chat_data.get("mirror", False)
        await context.bot.edit_message_reply_markup(chat_id=chat_id, message_id=chat_data["settings_msg_id"],
                                                      reply_markup=main_menu_keyboard(chat_data))
        return

    if data == "toggle_wide":
        chat_data["wide"] = not chat_data.get("wide", False)
        await context.bot.edit_message_reply_markup(chat_id=chat_id, message_id=chat_data["settings_msg_id"],
                                                      reply_markup=main_menu_keyboard(chat_data))
        return

    if data == "cycle_speed":
        cur = chat_data.get("speed", 1.0)
        idx = SPEED_STEPS.index(cur) if cur in SPEED_STEPS else 2
        chat_data["speed"] = SPEED_STEPS[(idx + 1) % len(SPEED_STEPS)]
        await context.bot.edit_message_reply_markup(chat_id=chat_id, message_id=chat_data["settings_msg_id"],
                                                      reply_markup=main_menu_keyboard(chat_data))
        return

    if data == "output_gif":
        chat_data["output_type"] = "gif"
        await build_and_send(chat_id, context, chat_data)
        return

    if data == "output_sticker":
        await context.bot.edit_message_text(chat_id=chat_id, message_id=chat_data["settings_msg_id"],
                                             text="🎯 ایموجی مناسب استیکر رو انتخاب کنید:", reply_markup=emoji_keyboard())
        return

    if data.startswith("emoji_"):
        chat_data["output_type"] = "sticker"
        chat_data["sticker_emoji"] = data.replace("emoji_", "")
        await build_and_send(chat_id, context, chat_data)
        return


def process_media(data):
    media_path = data["media_path"]
    media_type = data["media_type"]
    text = data.get("text", "")
    position = data.get("position", "auto")
    color = data.get("color", "c1")
    effect = data.get("effect", "colorful")
    mirror = data.get("mirror", False)
    wide = data.get("wide", False)
    reverse = data.get("reverse", False)
    speed = data.get("speed", 1.0)
    output_type = data.get("output_type", "gif")
    emoji = data.get("sticker_emoji") or random.choice(RANDOM_EMOJIS)

    uid = str(uuid.uuid4())
    frames_dir = os.path.join(TMP_DIR, uid + "_frames")
    os.makedirs(frames_dir, exist_ok=True)

    ext_out = "webm" if output_type == "sticker" else "mp4"
    output_path = os.path.join(TMP_DIR, f"{uid}_out.{ext_out}")

    layout_cache = {}
    watermark_cache = {}

    def finalize_frame(frame):
        frame = resize_to_target_width(frame).convert("RGB")
        w, h = frame.size

        if "layout" not in layout_cache:
            layout_cache["layout"] = calc_text_layout(w, h, text, color)
        render_text(frame, layout_cache["layout"], position, w, h)

        if "wm" not in watermark_cache:
            watermark_cache["wm"] = build_watermark_overlay(h, emoji)
        wm = watermark_cache["wm"]
        frame = frame.convert("RGBA")
        x = 8
        y = (h - wm.size[1]) // 2
        frame.paste(wm, (x, y), wm)
        frame = frame.convert("RGB")

        if mirror:
            frame = apply_mirror(frame)
        if wide:
            frame = apply_wide(frame)
        return frame

    if media_type == "photo":
        img = Image.open(media_path)
        img = finalize_frame(img)
        single_frame = os.path.join(frames_dir, "single.png")
        img.save(single_frame)
        input_args = ["-loop", "1", "-i", single_frame, "-t", "3"]
    else:
        subprocess.run([
            "ffmpeg", "-y", "-i", media_path,
            "-vf", f"fps=20,scale={TARGET_WIDTH}:-2:flags=lanczos",
            os.path.join(frames_dir, "raw_%04d.bmp")
        ], check=True)

        raw_files = sorted(f for f in os.listdir(frames_dir) if f.startswith("raw_"))
        for ff in raw_files:
            frame = Image.open(os.path.join(frames_dir, ff))
            frame = finalize_frame(frame)
            out_name = ff.replace("raw_", "f_")
            frame.save(os.path.join(frames_dir, out_name))
            os.remove(os.path.join(frames_dir, ff))

        input_args = ["-framerate", "20", "-i", os.path.join(frames_dir, "f_%04d.bmp")]

    vf_parts = ["scale=trunc(iw/2)*2:trunc(ih/2)*2"]
    effect_filter = EFFECTS.get(effect, {}).get("filter")
    if effect_filter:
        vf_parts.append(effect_filter)
    for fx_key in data.get("active_effects", []):
        fx_filter = EXTRA_EFFECTS.get(fx_key, {}).get("filter")
        if fx_filter:
            vf_parts.append(fx_filter)
    if speed and speed != 1.0:
        vf_parts.append(f"setpts={1 / speed:.4f}*PTS")
    if reverse and media_type != "photo":
        vf_parts.append("reverse")

    if output_type == "sticker":
        vf_parts = [
            f"scale={STICKER_SIZE}:{STICKER_SIZE}:force_original_aspect_ratio=decrease,"
            f"pad={STICKER_SIZE}:{STICKER_SIZE}:(ow-iw)/2:(oh-ih)/2:color=black@0"
        ] + vf_parts
        codec_args = ["-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "24",
                      "-deadline", "good", "-cpu-used", "4", "-row-mt", "1",
                      "-threads", "0", "-pix_fmt", "yuva420p"]
        duration_args = ["-t", str(STICKER_MAX_SECONDS)]
        extra_args = []
    else:
        codec_args = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
                       "-threads", "0", "-pix_fmt", "yuv420p"]
        duration_args = []
        extra_args = ["-movflags", "+faststart"]

    cmd = (
        ["ffmpeg", "-y"] + input_args + ["-vf", ",".join(vf_parts)]
        + duration_args + codec_args + ["-an"] + extra_args + [output_path]
    )
    subprocess.run(cmd, check=True)

    for f in os.listdir(frames_dir):
        os.remove(os.path.join(frames_dir, f))
    os.rmdir(frames_dir)

    return output_path, emoji


def main():
    request = HTTPXRequest(
        connect_timeout=30.0,
        read_timeout=60.0,
        write_timeout=60.0,
        pool_timeout=60.0,
    )
    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .request(request)
        .concurrent_updates(20)
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(
        (filters.PHOTO | filters.VIDEO | filters.ANIMATION | filters.Sticker.ALL) & filters.ChatType.PRIVATE,
        handle_media_private
    ))
    app.add_handler(MessageHandler(
        filters.REPLY & filters.Regex("^گیف$") & filters.ChatType.GROUPS,
        handle_group_trigger
    ))
    app.add_handler(MessageHandler(
        filters.Chat(username=CHANNEL_USERNAME) & filters.REPLY & filters.TEXT & ~filters.COMMAND,
        handle_support_reply
    ))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.run_polling()


if __name__ == "__main__":
    main()
