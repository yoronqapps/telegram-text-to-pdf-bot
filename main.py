import os
import io
import html
import logging
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from threading import Thread

from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CallbackQueryHandler, CommandHandler,
    MessageHandler, filters, ContextTypes,
)
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image as RLImage
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.units import cm
from reportlab.lib.colors import HexColor
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

logging.basicConfig(level=logging.INFO)
BOT_TOKEN = os.environ.get("BOT_TOKEN")

# ── Flask keep-alive ──────────────────────────────────────────────────────────
app_web = Flask('')

@app_web.route('/')
def home():
    return "alive"

Thread(target=lambda: app_web.run(host='0.0.0.0', port=6000), daemon=True).start()

# ── Arabic reshaper (optional; graceful fallback if not installed) ─────────────
try:
    import arabic_reshaper
    from bidi.algorithm import get_display as bidi_display
    _ARABIC_SUPPORT = True
except ImportError:
    _ARABIC_SUPPORT = False
    logging.warning("arabic-reshaper / python-bidi not installed; Arabic may render incorrectly.")

# ── Font registration ─────────────────────────────────────────────────────────
FONT_DIR = Path(__file__).parent / "fonts"
FONT_DIR.mkdir(exist_ok=True)

_DEJAVU = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
_REGISTERED: set[str] = set()   # tracks successfully registered font names

def _try_register(name: str, path: str) -> bool:
    """Register a TTFont with pdfmetrics; return True on success."""
    try:
        pdfmetrics.registerFont(TTFont(name, path))
        _REGISTERED.add(name)
        logging.info(f"Font registered: {name}")
        return True
    except Exception as e:
        logging.warning(f"Could not register {name}: {e}")
        return False

# Register DejaVu Sans immediately (system font — covers Latin/Greek/Cyrillic/Hebrew)
if Path(_DEJAVU).exists():
    _try_register("DejaVuSans", _DEJAVU)

# Noto fonts to download for non-Latin scripts
_NOTO_URLS: dict[str, str] = {
    "NotoSansEthiopic":   "https://github.com/googlefonts/noto-fonts/raw/main/hinted/ttf/NotoSansEthiopic/NotoSansEthiopic-Regular.ttf",
    "NotoSansArabic":     "https://github.com/googlefonts/noto-fonts/raw/main/hinted/ttf/NotoSansArabic/NotoSansArabic-Regular.ttf",
    "NotoSansDevanagari": "https://github.com/googlefonts/noto-fonts/raw/main/hinted/ttf/NotoSansDevanagari/NotoSansDevanagari-Regular.ttf",
    "NotoSansThai":       "https://github.com/googlefonts/noto-fonts/raw/main/hinted/ttf/NotoSansThai/NotoSansThai-Regular.ttf",
}

def _load_noto_fonts() -> None:
    """Download + register Noto fonts; called once at startup."""
    for name, url in _NOTO_URLS.items():
        dest = FONT_DIR / f"{name}.ttf"
        if not dest.exists():
            try:
                logging.info(f"Downloading {name}…")
                urllib.request.urlretrieve(url, dest)
            except Exception as e:
                logging.warning(f"Download failed ({name}): {e}")
                continue
        _try_register(name, str(dest))

# Load Noto fonts synchronously at startup so they're ready before first PDF
_load_noto_fonts()

# ── Script detection ───────────────────────────────────────────────────────────
def _detect_script(text: str) -> str:
    """Return the dominant Unicode script name in text."""
    counts: dict[str, int] = {}
    for ch in text:
        cp = ord(ch)
        if 0x1200 <= cp <= 0x137F or 0xAB00 <= cp <= 0xAB2F or 0x2D80 <= cp <= 0x2DDF:
            counts["ethiopic"] = counts.get("ethiopic", 0) + 1
        elif 0x0600 <= cp <= 0x06FF or 0x0750 <= cp <= 0x077F or 0xFB50 <= cp <= 0xFDFF:
            counts["arabic"] = counts.get("arabic", 0) + 1
        elif 0x0900 <= cp <= 0x097F:
            counts["devanagari"] = counts.get("devanagari", 0) + 1
        elif 0x4E00 <= cp <= 0x9FFF or 0x3040 <= cp <= 0x30FF or 0xAC00 <= cp <= 0xD7AF:
            counts["cjk"] = counts.get("cjk", 0) + 1
        elif 0x0E00 <= cp <= 0x0E7F:
            counts["thai"] = counts.get("thai", 0) + 1
        elif 0x10A0 <= cp <= 0x10FF:
            counts["georgian"] = counts.get("georgian", 0) + 1
        elif ch.isalpha():
            counts["latin"] = counts.get("latin", 0) + 1
    if not counts:
        return "latin"
    return max(counts, key=counts.get)


# Maps script → preferred registered font name (in priority order)
_SCRIPT_FONTS: dict[str, list[str]] = {
    "ethiopic":   ["NotoSansEthiopic", "DejaVuSans"],
    "arabic":     ["NotoSansArabic",   "DejaVuSans"],
    "devanagari": ["NotoSansDevanagari"],
    "thai":       ["NotoSansThai"],
    "georgian":   ["DejaVuSans"],
    "cjk":        ["DejaVuSans"],
    "latin":      [],   # caller supplies the user-chosen Latin font
}


def _pick_font(script: str, latin_font: str) -> str:
    """Return the best available registered font for the given script."""
    for candidate in _SCRIPT_FONTS.get(script, []):
        if candidate in _REGISTERED:
            return candidate
    # Fallback chain: user Latin font → DejaVuSans → Helvetica
    if "DejaVuSans" in _REGISTERED:
        return "DejaVuSans"
    return latin_font


def _prepare_line(line: str, script: str) -> str:
    """Apply Arabic reshaping + BiDi for correct RTL visual rendering."""
    if script == "arabic" and _ARABIC_SUPPORT:
        reshaped = arabic_reshaper.reshape(line)
        return bidi_display(reshaped)
    return line


# ── Design constants ──────────────────────────────────────────────────────────
PAGE_W, PAGE_H = A4
MG = 2.5 * cm

C_LABEL     = HexColor("#999999")
C_FOOTER    = HexColor("#AAAAAA")
C_WATERMARK = HexColor("#DEDEDE")
C_BODY      = HexColor("#1A1A1A")

THEMES = {
    "navy":    (HexColor("#DBEAFE"), "🌊 Ocean Navy"),
    "forest":  (HexColor("#D1FAE5"), "🌲 Forest Green"),
    "purple":  (HexColor("#EDE9FE"), "🌌 Midnight Purple"),
    "crimson": (HexColor("#FFE4E6"), "🔥 Crimson Red"),
    "slate":   (HexColor("#FEF3C7"), "🍊 Slate & Orange"),
}
DEFAULT_THEME = "navy"

FONTS = {
    "helvetica": ("Helvetica",   "Aa  Sans-serif · Helvetica",  "Clean and modern"),
    "times":     ("Times-Roman", "Aa  Serif · Times Roman",     "Classic and editorial"),
    "courier":   ("Courier",     "Aa  Mono · Courier",          "Typewriter / code"),
}
DEFAULT_FONT = "helvetica"


# ── NumberedCanvas — header / footer / watermark / "Page X of Y" ─────────────
class NumberedCanvas(rl_canvas.Canvas):
    def __init__(self, *args, divider_color=None, timestamp="", **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_page_states = []
        self._divider_color = divider_color or HexColor("#DBEAFE")
        self._timestamp = timestamp

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._saved_page_states)
        for i, state in enumerate(self._saved_page_states, 1):
            self.__dict__.update(state)
            self._draw_chrome(i, total)
            rl_canvas.Canvas.showPage(self)
        rl_canvas.Canvas.save(self)

    def _draw_tracked(self, x: float, y: float, text: str,
                      font: str, size: float, gap: float) -> None:
        """Draw text with manual inter-character spacing."""
        self.setFont(font, size)
        for ch in text:
            self.drawString(x, y, ch)
            x += self.stringWidth(ch, font, size) + gap

    def _draw_chrome(self, page_num: int, total_pages: int) -> None:
        hdr_y = PAGE_H - MG
        div_y = hdr_y - 16
        ftr_y = MG + 6
        wmk_y = MG - 14

        # tracked "PDF BOT" header label
        self.setFillColor(C_LABEL)
        self._draw_tracked(MG, hdr_y, "PDF BOT", "Helvetica", 7.5, 2.2)

        # hairline divider
        self.setStrokeColor(self._divider_color)
        self.setLineWidth(0.5)
        self.line(MG, div_y, PAGE_W - MG, div_y)

        # footer: timestamp left | "Page X of Y" right
        self.setFillColor(C_FOOTER)
        self.setFont("Helvetica", 7.5)
        self.drawString(MG, ftr_y, self._timestamp)
        self.setFont("Helvetica-Bold", 7.5)
        self.drawRightString(PAGE_W - MG, ftr_y, f"Page {page_num} of {total_pages}")

        # watermark
        self.setFillColor(C_WATERMARK)
        self.setFont("Helvetica", 7)
        self.drawCentredString(PAGE_W / 2, wmk_y, "@TextToPdfChangeBot")


def _canvas_factory(divider_color, timestamp):
    class _C(NumberedCanvas):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, divider_color=divider_color,
                             timestamp=timestamp, **kwargs)
    return _C


# ── Story builders ─────────────────────────────────────────────────────────────
def _make_para_style(font_name: str, script: str) -> ParagraphStyle:
    return ParagraphStyle(
        "body",
        fontName=font_name,
        fontSize=12,
        leading=18,
        textColor=C_BODY,
        spaceAfter=4,
        alignment=TA_RIGHT if script == "arabic" else TA_LEFT,
    )


def _text_to_story(text: str, latin_font: str) -> list:
    """Convert plain text to a Platypus story, auto-selecting Unicode fonts."""
    story = []
    for line in text.split("\n"):
        if not line.strip():
            story.append(Spacer(1, 6))
            continue
        script     = _detect_script(line)
        font_name  = _pick_font(script, latin_font)
        display    = _prepare_line(line, script)
        safe       = html.escape(display)
        story.append(Paragraph(safe, _make_para_style(font_name, script)))
    return story


def _image_story(img_bytes: bytes) -> list:
    """Scale photo to fit page width (max 60% page height) and return as story item."""
    max_w = PAGE_W - 2 * MG
    max_h = PAGE_H * 0.60
    img_io = io.BytesIO(img_bytes)
    img = RLImage(img_io, lazy=0)
    iw, ih = img.imageWidth, img.imageHeight
    scale = min(max_w / iw, max_h / ih, 1.0)
    return [RLImage(io.BytesIO(img_bytes), width=iw * scale, height=ih * scale)]


# ── PDF generation ─────────────────────────────────────────────────────────────
def _build_pdf(story: list, theme_key: str) -> bytes:
    divider_color = THEMES.get(theme_key, THEMES[DEFAULT_THEME])[0]
    timestamp     = datetime.now(timezone.utc).strftime("%-d %B %Y  \u2022  %H:%M UTC")
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=MG,
        rightMargin=MG,
        topMargin=3.5 * cm,
        bottomMargin=3.0 * cm,
    )
    doc.build(story, canvasmaker=_canvas_factory(divider_color, timestamp))
    buf.seek(0)
    return buf.read()


def make_pdf(text: str, theme_key: str = DEFAULT_THEME,
             font_key: str = DEFAULT_FONT) -> bytes:
    latin_font = FONTS.get(font_key, FONTS[DEFAULT_FONT])[0]
    story = _text_to_story(text, latin_font)
    return _build_pdf(story, theme_key)


def make_pdf_with_image(img_bytes: bytes, caption: str,
                        theme_key: str = DEFAULT_THEME,
                        font_key: str = DEFAULT_FONT) -> bytes:
    latin_font = FONTS.get(font_key, FONTS[DEFAULT_FONT])[0]
    story = _image_story(img_bytes)
    if caption.strip():
        story.append(Spacer(1, 0.4 * cm))
        story.extend(_text_to_story(caption, latin_font))
    return _build_pdf(story, theme_key)


# ── Keyboard helpers ──────────────────────────────────────────────────────────
def _theme_keyboard(current: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            ("✅ " if k == current else "") + label,
            callback_data=f"theme:{k}",
        )]
        for k, (_, label) in THEMES.items()
    ])


def _font_keyboard(current: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            ("✅ " if k == current else "") + label,
            callback_data=f"font:{k}",
        )]
        for k, (_, label, _feel) in FONTS.items()
    ])


def _get_theme(ctx: ContextTypes.DEFAULT_TYPE) -> str:
    return ctx.user_data.get("theme", DEFAULT_THEME)

def _get_font(ctx: ContextTypes.DEFAULT_TYPE) -> str:
    return ctx.user_data.get("font", DEFAULT_FONT)


# ── Command handlers ──────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name or "there"
    await update.message.reply_text(
        f"👋 Hey {name}! Welcome to *PDF Bot*.\n\n"
        "Send me any text — in *any language* — and I'll turn it into a beautiful PDF.\n"
        "Send a *photo with a caption* and I'll include both.\n\n"
        "/font  — choose a typeface\n"
        "/style — choose an accent tint\n"
        "/help  — full guide",
        parse_mode="Markdown",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📄 *PDF Bot — Help*\n\n"
        "*Supported input:*\n"
        "• Plain text (any language)\n"
        "• Photo → PDF with embedded image\n"
        "• Photo + caption → image and text together\n\n"
        "*Languages supported:*\n"
        "Amharic 🇪🇹, Arabic 🇸🇦, Hindi 🇮🇳, Thai 🇹🇭,\n"
        "Latin/Greek/Cyrillic and more\n\n"
        "*Commands:*\n"
        "/start  — Welcome\n"
        "/help   — This message\n"
        "/font   — Choose body typeface\n"
        "/style  — Choose accent tint\n\n"
        "*PDF features:*\n"
        "• Apple-minimal design\n"
        "• Auto Unicode font per script\n"
        "• Hairline divider with accent tint\n"
        "• Timestamp + Page X of Y footer\n"
        "• Watermark at bottom",
        parse_mode="Markdown",
    )


async def cmd_font(update: Update, context: ContextTypes.DEFAULT_TYPE):
    current = _get_font(context)
    _, label, feel = FONTS[current]
    await update.message.reply_text(
        f"🔤 *Choose a body typeface* _(for Latin text)_:\nCurrent: _{label}_ — {feel}",
        parse_mode="Markdown",
        reply_markup=_font_keyboard(current),
    )


async def cmd_style(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎨 *Choose an accent tint for the divider line:*",
        parse_mode="Markdown",
        reply_markup=_theme_keyboard(_get_theme(context)),
    )


# ── Callback handlers ─────────────────────────────────────────────────────────
async def cb_font(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    key = q.data.split(":", 1)[1]
    if key not in FONTS:
        return
    context.user_data["font"] = key
    _, label, feel = FONTS[key]
    await q.edit_message_text(
        f"✅ Font set to *{label}*\n_{feel}_\n\nSend any text to try it.",
        parse_mode="Markdown",
        reply_markup=_font_keyboard(key),
    )


async def cb_theme(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    key = q.data.split(":", 1)[1]
    if key not in THEMES:
        return
    context.user_data["theme"] = key
    await q.edit_message_text(
        f"✅ Accent set to *{THEMES[key][1]}*.\n\nSend any text to try it.",
        parse_mode="Markdown",
        reply_markup=_theme_keyboard(key),
    )


# ── Message handlers ──────────────────────────────────────────────────────────
async def _send_pdf(update: Update, pdf_bytes: bytes) -> None:
    await update.message.reply_document(
        document=io.BytesIO(pdf_bytes),
        filename="output.pdf",
        caption="Here is your PDF 📄",
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        raw = update.message.text
        if not raw:
            await update.message.reply_text("Please send a text message.")
            return
        try:
            pdf_bytes = make_pdf(raw, _get_theme(context), _get_font(context))
        except Exception as e:
            logging.error(f"PDF generation failed: {e}", exc_info=True)
            await update.message.reply_text("Sorry, could not generate PDF. Try again.")
            return
        await _send_pdf(update, pdf_bytes)
    except Exception as e:
        logging.error(f"handle_text error: {e}", exc_info=True)
        try:
            await update.message.reply_text("Something went wrong. Please try again.")
        except Exception:
            pass


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        photo   = update.message.photo[-1]           # largest available resolution
        tg_file = await context.bot.get_file(photo.file_id)
        img_bytes = bytes(await tg_file.download_as_bytearray())
        caption   = update.message.caption or ""
        try:
            pdf_bytes = make_pdf_with_image(
                img_bytes, caption, _get_theme(context), _get_font(context)
            )
        except Exception as e:
            logging.error(f"Photo PDF failed: {e}", exc_info=True)
            await update.message.reply_text("Sorry, could not process that photo. Try again.")
            return
        await _send_pdf(update, pdf_bytes)
    except Exception as e:
        logging.error(f"handle_photo error: {e}", exc_info=True)
        try:
            await update.message.reply_text("Something went wrong. Please try again.")
        except Exception:
            pass


# ── Wire up & run ─────────────────────────────────────────────────────────────
app = ApplicationBuilder().token(BOT_TOKEN).build()
app.add_handler(CommandHandler("start", cmd_start))
app.add_handler(CommandHandler("help",  cmd_help))
app.add_handler(CommandHandler("font",  cmd_font))
app.add_handler(CommandHandler("style", cmd_style))
app.add_handler(CallbackQueryHandler(cb_font,  pattern=r"^font:"))
app.add_handler(CallbackQueryHandler(cb_theme, pattern=r"^theme:"))
app.add_handler(MessageHandler(filters.PHOTO,                         handle_photo))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,       handle_text))
print("Bot running...")
app.run_polling()
