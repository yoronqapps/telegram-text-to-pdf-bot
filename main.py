import os
import io
import re
import html
import asyncio
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
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Image as RLImage,
    Preformatted, Table, TableStyle, ListFlowable, ListItem, HRFlowable,
)
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.units import cm
from reportlab.lib.colors import HexColor
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

logging.basicConfig(level=logging.INFO)
BOT_TOKEN = os.environ.get("BOT_TOKEN")

# ── Port / domain detection ───────────────────────────────────────────────────
PORT           = int(os.environ.get("PORT", 6000))
RAILWAY_DOMAIN = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")   # set by Railway automatically

# ── Flask keep-alive (used only when NOT on Railway / no webhook) ─────────────
app_web = Flask('')

@app_web.route('/')
def home():
    return "alive"

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

_DEJAVU_DIR = "/usr/share/fonts/truetype/dejavu"
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

# Register DejaVu Sans + its real bold/italic faces (system font — covers
# Latin/Greek/Cyrillic/Hebrew). Having the real faces means <b>/<i> tags in
# markdown actually render bold/italic instead of just falling back to regular.
_try_register("DejaVuSans", f"{_DEJAVU_DIR}/DejaVuSans.ttf")
_try_register("DejaVuSans-Bold", f"{_DEJAVU_DIR}/DejaVuSans-Bold.ttf")
_try_register("DejaVuSans-Oblique", f"{_DEJAVU_DIR}/DejaVuSans-Oblique.ttf")
_try_register("DejaVuSans-BoldOblique", f"{_DEJAVU_DIR}/DejaVuSans-BoldOblique.ttf")

if "DejaVuSans" in _REGISTERED:
    pdfmetrics.registerFontFamily(
        "DejaVuSans",
        normal="DejaVuSans",
        bold="DejaVuSans-Bold" if "DejaVuSans-Bold" in _REGISTERED else "DejaVuSans",
        italic="DejaVuSans-Oblique" if "DejaVuSans-Oblique" in _REGISTERED else "DejaVuSans",
        boldItalic="DejaVuSans-BoldOblique" if "DejaVuSans-BoldOblique" in _REGISTERED else "DejaVuSans",
    )

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
        # No bold/italic faces available for these — map them to themselves so
        # that <b>/<i> tags in markdown don't crash ReportLab's Paragraph parser
        # (it still renders regular weight, but that's better than an exception).
        if name in _REGISTERED:
            pdfmetrics.registerFontFamily(name, normal=name, bold=name, italic=name, boldItalic=name)

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
    # NOTE: previously this fell through to DejaVuSans even for "latin" text
    # whenever DejaVuSans was registered, which silently ignored the user's
    # /font choice (Helvetica/Times/Courier) for almost all English text.
    # Latin script should always respect the explicit choice.
    if script == "latin":
        return latin_font
    for candidate in _SCRIPT_FONTS.get(script, []):
        if candidate in _REGISTERED:
            return candidate
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
C_QUOTE     = HexColor("#52525B")
C_CODE_BG   = HexColor("#F4F4F5")
C_CODE_BOX  = HexColor("#E4E4E7")
C_LINK      = HexColor("#2563EB")

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


# ── Markdown inline parsing (bold / italic / strike / code / links) ───────────
_CODE_SPAN_RE = re.compile(r'`([^`]+?)`')
_LINK_RE      = re.compile(r'\[([^\]]+)\]\(([^)\s]+)\)')
_BOLD_RE      = re.compile(r'\*\*(.+?)\*\*|__(.+?)__')
_ITALIC_RE    = re.compile(r'(?<!\*)\*(?!\*)([^*]+?)\*(?!\*)|(?<!_)_(?!_)([^_]+?)_(?!_)')
_STRIKE_RE    = re.compile(r'~~(.+?)~~')


def _inline_to_xml(text: str) -> str:
    """Convert one line/paragraph of markdown inline syntax into ReportLab's
    mini-XML markup, escaping everything else so it's always safe to feed
    straight into a Paragraph()."""
    # Stash code spans and links first so markup inside them (e.g. an
    # underscore in a URL, or asterisks in a code sample) is never touched
    # by the bold/italic/strike passes below.
    stash = []

    def _stash(value: str) -> str:
        token = f"\x00{len(stash)}\x00"
        stash.append(value)
        return token

    def _code_sub(m):
        return _stash(f'<font face="Courier" size="10">{html.escape(m.group(1))}</font>')

    def _link_sub(m):
        label = html.escape(m.group(1))
        url = html.escape(m.group(2), quote=True)
        return _stash(f'<link href="{url}"><font color="#{C_LINK.hexval()[2:]}"><u>{label}</u></font></link>')

    text = _CODE_SPAN_RE.sub(_code_sub, text)
    text = _LINK_RE.sub(_link_sub, text)

    # Escape what's left, then layer on bold/italic/strikethrough tags.
    text = html.escape(text)
    text = _BOLD_RE.sub(lambda m: f"<b>{m.group(1) or m.group(2)}</b>", text)
    text = _ITALIC_RE.sub(lambda m: f"<i>{m.group(1) or m.group(2)}</i>", text)
    text = _STRIKE_RE.sub(lambda m: f"<strike>{m.group(1)}</strike>", text)

    # Restore stashed code/link fragments.
    for i, value in enumerate(stash):
        text = text.replace(f"\x00{i}\x00", value)
    return text


# ── Markdown block parsing (headings / lists / quotes / code fences / hr) ────
_HEADER_RE = re.compile(r'^(#{1,6})\s+(.*)$')
_UL_RE     = re.compile(r'^\s*[-*+]\s+(.*)$')
_OL_RE     = re.compile(r'^\s*\d+[.)]\s+(.*)$')
_HR_RE     = re.compile(r'^\s*([-*_])\1{2,}\s*$')
_QUOTE_RE  = re.compile(r'^\s*>\s?(.*)$')
_FENCE_RE  = re.compile(r'^\s*```')


def _parse_markdown_blocks(text: str) -> list[tuple[str, str]]:
    """Split raw text into (block_type, content) pairs. block_type is one of:
    'p', 'h1'..'h6', 'ul', 'ol', 'quote', 'code', 'hr'."""
    blocks: list[tuple[str, str]] = []
    lines = text.split("\n")
    buf: list[str] = []
    i = 0

    def _flush_paragraph():
        if buf:
            blocks.append(("p", " ".join(buf)))
            buf.clear()

    while i < len(lines):
        line = lines[i]

        if _FENCE_RE.match(line):
            _flush_paragraph()
            i += 1
            code_lines = []
            while i < len(lines) and not _FENCE_RE.match(lines[i]):
                code_lines.append(lines[i])
                i += 1
            i += 1  # skip closing fence (if present)
            blocks.append(("code", "\n".join(code_lines)))
            continue

        if not line.strip():
            _flush_paragraph()
            i += 1
            continue

        m = _HEADER_RE.match(line)
        if m:
            _flush_paragraph()
            blocks.append((f"h{len(m.group(1))}", m.group(2).strip()))
            i += 1
            continue

        if _HR_RE.match(line):
            _flush_paragraph()
            blocks.append(("hr", ""))
            i += 1
            continue

        m = _QUOTE_RE.match(line)
        if m:
            _flush_paragraph()
            quote_lines = [m.group(1)]
            i += 1
            while i < len(lines) and _QUOTE_RE.match(lines[i]):
                quote_lines.append(_QUOTE_RE.match(lines[i]).group(1))
                i += 1
            blocks.append(("quote", " ".join(quote_lines)))
            continue

        m = _UL_RE.match(line)
        if m:
            _flush_paragraph()
            blocks.append(("ul", m.group(1)))
            i += 1
            continue

        m = _OL_RE.match(line)
        if m:
            _flush_paragraph()
            blocks.append(("ol", m.group(1)))
            i += 1
            continue

        buf.append(line.strip())
        i += 1

    _flush_paragraph()
    return blocks


# ── Story builders ─────────────────────────────────────────────────────────────
def _make_para_style(font_name: str, script: str) -> ParagraphStyle:
    return ParagraphStyle(
        "body",
        fontName=font_name,
        fontSize=12,
        leading=18,
        textColor=C_BODY,
        spaceAfter=8,
        alignment=TA_RIGHT if script == "arabic" else TA_LEFT,
    )


_HEADING_SIZES = {1: 21, 2: 18, 3: 16, 4: 14, 5: 12.5, 6: 12}


def _heading_style(level: int, font_name: str, script: str) -> ParagraphStyle:
    size = _HEADING_SIZES.get(level, 12)
    return ParagraphStyle(
        f"h{level}",
        fontName=font_name,
        fontSize=size,
        leading=size * 1.25,
        textColor=C_BODY,
        spaceBefore=14 if level <= 2 else 8,
        spaceAfter=6,
        alignment=TA_RIGHT if script == "arabic" else TA_LEFT,
    )


def _quote_block(text_xml: str, font_name: str, accent_color) -> Table:
    style = ParagraphStyle("quote", fontName=font_name, fontSize=11.5, leading=16, textColor=C_QUOTE)
    para = Paragraph(text_xml, style)
    width = PAGE_W - 2 * MG
    tbl = Table([["", para]], colWidths=[5, width - 5])
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, 0), accent_color),
        ("LEFTPADDING", (1, 0), (1, 0), 10),
        ("LEFTPADDING", (0, 0), (0, 0), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    return tbl


def _code_block_flowable(code_text: str) -> Table:
    style = ParagraphStyle("code", fontName="Courier", fontSize=9.5, leading=13, textColor=C_BODY)
    pre = Preformatted(code_text, style)
    width = PAGE_W - 2 * MG
    tbl = Table([[pre]], colWidths=[width])
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), C_CODE_BG),
        ("BOX", (0, 0), (-1, -1), 0.5, C_CODE_BOX),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    return tbl


def _build_list(items_xml: list[str], font_name: str, ordered: bool) -> ListFlowable:
    style = ParagraphStyle("li", fontName=font_name, fontSize=12, leading=17, spaceAfter=3)
    items = [ListItem(Paragraph(t, style), leftIndent=4) for t in items_xml]
    return ListFlowable(
        items,
        bulletType="1" if ordered else "bullet",
        start=1 if ordered else None,
        leftIndent=20,
        bulletFontName="Helvetica",
        bulletFontSize=11,
        bulletColor=C_BODY,
        spaceBefore=2,
        spaceAfter=10,
    )


def _markdown_to_story(text: str, latin_font: str, accent_color) -> list:
    """Render markdown (the same kind Claude outputs: headings, **bold**,
    *italic*, `code`, fenced code blocks, lists, quotes, links, hr) into a
    Platypus story, auto-selecting Unicode fonts per block as before."""
    blocks = _parse_markdown_blocks(text)
    story: list = []
    pending_items: list[str] = []
    pending_ordered: bool | None = None

    def _flush_list():
        nonlocal pending_items, pending_ordered
        if pending_items:
            script = _detect_script(" ".join(pending_items))
            font_name = _pick_font(script, latin_font)
            story.append(_build_list(pending_items, font_name, bool(pending_ordered)))
        pending_items = []
        pending_ordered = None

    for btype, content in blocks:
        if btype in ("ul", "ol"):
            ordered = (btype == "ol")
            if pending_items and pending_ordered != ordered:
                _flush_list()
            pending_ordered = ordered
            pending_items.append(_inline_to_xml(content))
            continue

        _flush_list()

        if btype == "hr":
            story.append(Spacer(1, 4))
            story.append(HRFlowable(width="100%", thickness=0.6, color=accent_color, spaceAfter=10))
            continue

        if btype == "code":
            story.append(Spacer(1, 2))
            story.append(_code_block_flowable(content))
            story.append(Spacer(1, 8))
            continue

        if btype == "quote":
            script = _detect_script(content)
            font_name = _pick_font(script, latin_font)
            story.append(_quote_block(_inline_to_xml(content), font_name, accent_color))
            story.append(Spacer(1, 4))
            continue

        if btype.startswith("h") and btype[1:].isdigit():
            level = int(btype[1:])
            script = _detect_script(content)
            font_name = _pick_font(script, latin_font)
            xml = f"<b>{_inline_to_xml(content)}</b>"
            story.append(Paragraph(xml, _heading_style(level, font_name, script)))
            continue

        # plain paragraph
        script = _detect_script(content)
        font_name = _pick_font(script, latin_font)
        display = _prepare_line(content, script) if script == "arabic" else content
        xml = _inline_to_xml(display)
        story.append(Paragraph(xml, _make_para_style(font_name, script)))

    _flush_list()
    return story


def _text_to_story(text: str, latin_font: str) -> list:
    """Plain-text fallback (no markdown parsing) — kept so a malformed
    markdown edge case can never crash PDF generation outright."""
    story = []
    for line in text.split("\n"):
        if not line.strip():
            story.append(Spacer(1, 6))
            continue
        script = _detect_script(line)
        font_name = _pick_font(script, latin_font)
        display = _prepare_line(line, script)
        safe = html.escape(display)
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
    accent_color = THEMES.get(theme_key, THEMES[DEFAULT_THEME])[0]
    try:
        story = _markdown_to_story(text, latin_font, accent_color)
        if not story:
            story = [Spacer(1, 1)]
    except Exception as e:
        logging.warning(f"Markdown rendering failed, falling back to plain text: {e}")
        story = _text_to_story(text, latin_font)
    return _build_pdf(story, theme_key)


def make_pdf_with_image(img_bytes: bytes, caption: str,
                        theme_key: str = DEFAULT_THEME,
                        font_key: str = DEFAULT_FONT) -> bytes:
    latin_font = FONTS.get(font_key, FONTS[DEFAULT_FONT])[0]
    accent_color = THEMES.get(theme_key, THEMES[DEFAULT_THEME])[0]
    story = _image_story(img_bytes)
    if caption.strip():
        story.append(Spacer(1, 0.4 * cm))
        try:
            story.extend(_markdown_to_story(caption, latin_font, accent_color))
        except Exception as e:
            logging.warning(f"Markdown caption rendering failed, falling back: {e}")
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
        "Send me any text — in *any language*, with Markdown formatting — "
        "and I'll turn it into a beautiful PDF that keeps your formatting.\n"
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
        "• Plain or Markdown-formatted text (any language)\n"
        "• Photo → PDF with embedded image\n"
        "• Photo + caption → image and text together\n\n"
        "*Markdown formatting kept in the PDF:*\n"
        "`# Headings`, `**bold**`, `*italic*`, `~~strike~~`,\n"
        "`` `inline code` ``, fenced \\`\\`\\` code blocks \\`\\`\\`,\n"
        "`- bullet` / `1. numbered` lists, `> quotes`,\n"
        "`[links](url)` and `---` dividers\n\n"
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


# Telegram's Bot API hard-caps a single text message at 4096 characters.
# When a client has to split a longer paste, it tries to cut at a nearby
# line break rather than exactly at the limit — so chunk length alone isn't
# a reliable signal for "more is coming." Instead, every incoming text
# message restarts a short timer; only once the timer elapses with nothing
# new for that chat do we merge whatever arrived and generate one PDF.
BATCH_DELAY = 1.5   # seconds to wait for a possible continuation chunk

# chat_id -> {"chunks": [...], "task": asyncio.Task | None}
_pending_text: dict[int, dict] = {}


async def _flush_pending_text(chat_id: int, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = _pending_text.pop(chat_id, None)
    if not state or not state["chunks"]:
        return
    full_text = "".join(state["chunks"])
    try:
        pdf_bytes = make_pdf(full_text, _get_theme(context), _get_font(context))
    except Exception as e:
        logging.error(f"PDF generation failed: {e}", exc_info=True)
        await update.message.reply_text("Sorry, could not generate PDF. Try again.")
        return
    await _send_pdf(update, pdf_bytes)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        raw = update.message.text
        if not raw or not raw.strip():
            await update.message.reply_text("Please send a text message.")
            return

        chat_id = update.effective_chat.id
        state = _pending_text.setdefault(chat_id, {"chunks": [], "task": None})

        # A fresh chunk arrived — any previous timer is stale, cancel it.
        if state["task"] is not None:
            state["task"].cancel()
            state["task"] = None

        state["chunks"].append(raw)

        # Quick "I'm on it" indicator since the actual PDF now arrives after
        # a short delay rather than instantly.
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action="upload_document")
        except Exception:
            pass

        async def _delayed_flush():
            try:
                await asyncio.sleep(BATCH_DELAY)
                await _flush_pending_text(chat_id, update, context)
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logging.error(f"delayed flush failed: {e}", exc_info=True)

        state["task"] = asyncio.create_task(_delayed_flush())
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


# ── General error handler ─────────────────────────────────────────────────────
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.error(f"Unhandled error: {context.error}", exc_info=context.error)


# ── Wire up & run ─────────────────────────────────────────────────────────────
def main() -> None:
    if not RAILWAY_DOMAIN:
        Thread(target=lambda: app_web.run(host='0.0.0.0', port=PORT), daemon=True).start()

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help",  cmd_help))
    app.add_handler(CommandHandler("font",  cmd_font))
    app.add_handler(CommandHandler("style", cmd_style))
    app.add_handler(CallbackQueryHandler(cb_font,  pattern=r"^font:"))
    app.add_handler(CallbackQueryHandler(cb_theme, pattern=r"^theme:"))
    app.add_handler(MessageHandler(filters.PHOTO,                   handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(error_handler)
    print("Bot running...")

    if RAILWAY_DOMAIN:
        logging.info(f"Webhook mode → https://{RAILWAY_DOMAIN}/{BOT_TOKEN}")
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=BOT_TOKEN,
            webhook_url=f"https://{RAILWAY_DOMAIN}/{BOT_TOKEN}",
            drop_pending_updates=True,
        )
    else:
        logging.info("Polling mode (local)")
        app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
