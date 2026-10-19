"""Optional bounded PDF rendering with an operator-supplied, digest-bound font."""
import hashlib
import io
import re
from xml.sax.saxutils import escape

from . import assessment as a

FONT_LIMIT = 8388608
PDF_LIMIT = 4194304
FAMILIES = ("Arial", "Courier New", "Palatino", "Palatino Linotype", "Times New Roman", "Computer Modern", "CM Roman", "cmr10")


def dependencies():
    try:
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfgen.canvas import Canvas
        from reportlab.platypus import Paragraph
        from reportlab.lib.styles import ParagraphStyle
        from pypdf import PdfReader
        return TTFont, pdfmetrics, Canvas, Paragraph, ParagraphStyle, PdfReader
    except ImportError:
        raise a.AssessmentError("PDF export requires the declared optional PDF dependencies.") from None


def validate_font_proof(value):
    from .allocation_export import check, digest, literal
    check(value, {"sha256": digest, "family": literal(*FAMILIES), "postscript_name": lambda s:
                  a.reject() if type(s) is not str or re.fullmatch(r"[A-Za-z0-9_-]{1,96}", s) is None else None,
                  "provenance": literal("operator_supplied_font_digest_approved"), "size_bytes": lambda n: a.number(n, 1, FONT_LIMIT)})


def inspect_font(payload):
    if type(payload) is not bytes or not 0 < len(payload) <= FONT_LIMIT:
        a.reject()
    TTFont, *_ = dependencies()
    try:
        font = TTFont("WaypointDraft" + hashlib.sha256(payload).hexdigest(), io.BytesIO(payload), validate=1)
        proof = {"sha256": hashlib.sha256(payload).hexdigest(), "family": font.face.familyName.decode("ascii"),
                 "postscript_name": font.face.name.decode("ascii"), "size_bytes": len(payload),
                 "provenance": "operator_supplied_font_digest_approved"}
        validate_font_proof(proof)
        return proof, font
    except Exception:
        raise a.AssessmentError("The supplied font is unsupported or invalid; no fallback font was used.") from None


def render_pdf(record, payload):
    from .allocation_export import PROFILES
    proof, font = inspect_font(payload)
    if proof != record["font"]:
        raise a.AssessmentError("The font differs from the approved preview.")
    _, pdfmetrics, Canvas, Paragraph, ParagraphStyle, PdfReader = dependencies()
    info = PROFILES[record["profile"]]
    title = info["title"] + " - DRAFT"
    footer = "Local operator draft; no submission or resource authority."
    strings = [title, footer, "Page 0123456789"] + [f["label"] + ": " + f["text"] for f in record["fields"]]
    if any(ord(c) not in font.face.charToGlyph for text in strings for c in text if c != "\n"):
        raise a.AssessmentError("The approved font lacks a required glyph; no substitution was made.")
    for baseline, points, text in ((706, 14, title), (88, 11, footer), (78, 11, "Page 8")):
        if baseline + font.face.ascent * points / 1000 > 720 or baseline + font.face.descent * points / 1000 < 72:
            raise a.AssessmentError("The approved font cannot fit the profile margins without alteration.")
    pdfmetrics.registerFont(font)
    style = ParagraphStyle("draft", fontName=font.fontName, fontSize=11, leading=13, spaceAfter=6,
                           splitLongWords=True, allowWidows=0, allowOrphans=0)
    # Lay out complete paragraphs first. Refuse rather than shrink or discard text.
    pages, current, top = [], [], 690
    for field in record["fields"]:
        text = escape(field["label"] + ": " + field["text"]).replace("\n", "<br/>")
        paragraph = Paragraph(text, style)
        _, height = paragraph.wrap(468, 590)
        if height > 590:
            raise a.AssessmentError("A narrative section exceeds the page area; shorten it and approve a new preview.")
        if top - height < 100:
            pages.append(current); current = []; top = 690
        current.append((paragraph, top - height)); top -= height + 7
    pages.append(current)
    if len(pages) > info["pages"]:
        raise a.AssessmentError("The draft exceeds this profile's page limit; shorten it and approve a new preview.")
    output = io.BytesIO()
    canvas = Canvas(output, pagesize=(612, 792), pageCompression=1, invariant=1)
    canvas.setTitle(title); canvas.setAuthor(""); canvas.setSubject("Synthetic provisional draft; no submission authority")
    canvas.setCreator("Waypoint allocation draft")
    for index, page in enumerate(pages, 1):
        canvas.setFont(font.fontName, 14); canvas.drawString(72, 706, title)
        for paragraph, y in page:
            paragraph.drawOn(canvas, 72, y)
        canvas.setFont(font.fontName, 11); canvas.drawString(72, 88, footer)
        canvas.drawRightString(540, 78, "Page " + str(index))
        canvas.showPage()
    canvas.save()
    result = output.getvalue()
    if len(result) > PDF_LIMIT:
        a.reject()
    reader = PdfReader(io.BytesIO(result), strict=True)
    root = reader.trailer["/Root"]
    if len(reader.pages) != len(pages) or any(k in root for k in ("/OpenAction", "/AA", "/Names", "/AcroForm")) or any(p.get("/Annots") for p in reader.pages):
        a.reject()
    return result
