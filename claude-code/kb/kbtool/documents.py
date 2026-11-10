"""Office, web and book documents -> text + provenance, standard library only.

Contract (5a.2 / 5a.3):
- Text keeps document order: docx body paragraphs (then footnotes, endnotes, headers,
  footers), pptx slides by presentation order with each slide's notes as its own
  segment, xlsx sheets by workbook order, epub chapters by spine order. Segments are
  joined by a blank line; ``document.segments`` records codepoint offsets instead of
  inserting marker text that a quote could pick up.
- Content present in the file but not converted to text (pictures, charts, diagrams,
  embedded objects, video) is a known gap, like raster pages in PDF: text + gap is
  ``partial``, no text + gap is ``ocr_required``, no text and no gap is ``empty``.
- A broken package is ``read_failed`` with ``error`` naming the cause; never an exception.
- Author and date come only from the document's own metadata. ``author_basis`` is
  ``document_metadata`` or ``none``; ``date_basis`` is ``created``, ``modified``,
  ``published`` or ``none``. The file's time on disk is never used as a document date
  (``file_mtime`` is a reserved basis that this reader does not emit).

COPIED into kb-plugin (plugins/kb/kbtool/documents.py), 2026-11-06.
Source: Saved Downloader/bounded_contexts/knowledge_wiki/src/knowledge/documents.py @ d24cafa
(repo HEAD 61f01cb, branch refactor/platform-only). Body below is verbatim; kb does not import SD.
kb MVP uses the .docx and .html parsers; the others are kept for v1 (xlsx/pptx/epub/rtf).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from html.parser import HTMLParser
import codecs
import io
import posixpath
import re
from urllib.parse import unquote
import xml.etree.ElementTree as ET
import zipfile
import zlib

MAX_PART_BYTES = 64 * 1024 * 1024
GAP_RELATIONS = {"image", "chart", "chartEx", "diagramData", "oleObject", "package",
                 "video", "audio", "media", "hdphoto"}
DATE_BASES = ("created", "modified", "published", "file_mtime", "none")


class DocumentError(ValueError):
    """A structural defect of the input file; reported as read_failed with this code."""


def _local(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _family(tag):
    """Namespace family, tolerant of transitional and strict OOXML URIs."""
    ns = tag[1:].split("}", 1)[0] if isinstance(tag, str) and tag.startswith("{") else ""
    for family in ("wordprocessingml", "drawingml", "presentationml", "spreadsheetml"):
        if family in ns:
            return family
    return ns


# ---------------------------------------------------------------------------
# provenance
# ---------------------------------------------------------------------------
_ISO = re.compile(r"(\d{4})(?:-(\d{2})(?:-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?"
                  r"(Z|[+-]\d{2}:?\d{2})?)?)?)?")


def iso_date(value):
    """Validated W3CDTF/ISO date string (original precision) or None."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    match = _ISO.fullmatch(value)
    if not match:
        return None
    year, month, day, hour, minute, second, _ = match.groups()
    try:
        datetime(int(year), int(month or 1), int(day or 1), int(hour or 0), int(minute or 0), int(second or 0))
    except ValueError:
        return None
    if int(year) < 1000:
        return None
    return value


def pdf_date(value):
    """PDF date string (``D:YYYYMMDDHHmmSSOHH'mm'``) -> ISO string or None."""
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"(?:D:)?(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?"
                         r"(Z|[+-]\d{2}'?\d{2}'?)?'?", value.strip())
    if not match:
        return None
    year, month, day, hour, minute, second, zone = match.groups()
    text = year + (f"-{month}" if month else "") + (f"-{day}" if month and day else "")
    if month and day and hour:
        text += f"T{hour}:{minute or '00'}:{second or '00'}"
        if zone:
            text += "Z" if zone == "Z" else zone[:3] + ":" + zone.replace("'", "")[3:5]
    return iso_date(text)


def set_provenance(meta, *, author=None, created=None, modified=None, published=None,
                   last_modified_by=None, title=None):
    """Write author/date with an explicit basis; absent values are ``none``, never guessed."""
    authors = [a for a in (author if isinstance(author, list) else [author])
               if isinstance(a, str) and " ".join(a.split())]
    authors = list(dict.fromkeys(" ".join(a.split()) for a in authors))
    meta["author"] = "; ".join(authors)
    meta["author_basis"] = "document_metadata" if authors else "none"
    dates = {k: v for k, v in (("created", iso_date(created)), ("modified", iso_date(modified)),
                               ("published", iso_date(published))) if v}
    rejected = {k: v for k, v in (("created", created), ("modified", modified), ("published", published))
                if isinstance(v, str) and v.strip() and k not in dates}
    basis = next((k for k in ("published", "created", "modified") if k in dates), "none")
    meta["date"] = dates.get(basis, "")
    meta["date_basis"] = basis
    if dates:
        meta["document_dates"] = dates
    if rejected:
        meta["document_dates_rejected"] = rejected
    if isinstance(last_modified_by, str) and last_modified_by.strip():
        meta["last_modified_by"] = " ".join(last_modified_by.split())
    if isinstance(title, str) and title.strip():
        meta["document_title"] = " ".join(title.split())


# ---------------------------------------------------------------------------
# shared assembly
# ---------------------------------------------------------------------------
class _Assembly:
    def __init__(self):
        self.text, self.segments = "", []

    def add(self, kind, body, **attrs):
        body = body.strip("\n")
        if self.text and body:
            self.text += "\n\n"
        start = len(self.text)
        self.text += body
        self.segments.append({"kind": kind, **attrs, "start": start, "end": len(self.text)})


def _finish(meta, assembly, gaps, *, limitations=(), fmt):
    """Coverage state from extracted text and known non-text content."""
    gaps = {k: v for k, v in gaps.items() if v}
    has_text = bool(assembly.text.strip())
    state = ("partial" if has_text else "ocr_required") if gaps else ("ready" if has_text else "empty")
    meta["document"] = {"format": fmt, "segments": assembly.segments,
                        "unextracted_content": gaps, "ocr_required": bool(gaps),
                        "assessment_scope": "text layer and embedded non-text parts; not visual-content comprehension"}
    if limitations:
        meta["coverage_limitations"] = list(dict.fromkeys(meta.get("coverage_limitations", []) + list(limitations)))
    return assembly.text, state


class _Package:
    def __init__(self, raw):
        self.zip = zipfile.ZipFile(io.BytesIO(raw))
        self.names = {n.lower(): n for n in self.zip.namelist()}

    def has(self, name):
        return name is not None and name.lower() in self.names

    def read(self, name):
        real = self.names.get(name.lower())
        if real is None:
            raise DocumentError("missing_part:" + name)
        with self.zip.open(real) as handle:
            data = handle.read(MAX_PART_BYTES + 1)
        if len(data) > MAX_PART_BYTES:
            raise DocumentError("part_too_large:" + name)
        return data

    def xml(self, name):
        data = self.read(name)
        if re.search(rb"<!(?:DOCTYPE|ENTITY)", data):
            raise DocumentError("xml_doctype_rejected:" + name)
        return ET.fromstring(data)

    def rels(self, part):
        folder, base = posixpath.split(part)
        path = posixpath.join(folder, "_rels", base + ".rels")
        if not self.has(path):
            return []
        result = []
        for rel in self.xml(path):
            if _local(rel.tag) != "Relationship":
                continue
            target, external = rel.get("Target", ""), rel.get("TargetMode") == "External"
            if not external:
                target = unquote(target)
                target = posixpath.normpath(target.lstrip("/") if target.startswith("/")
                                            else posixpath.join(folder, target))
            result.append({"id": rel.get("Id"), "kind": (rel.get("Type") or "").rsplit("/", 1)[-1],
                           "target": target, "external": external})
        return result

    def main_part(self, default):
        for rel in self.rels(""):
            if rel["kind"] == "officeDocument" and not rel["external"]:
                return rel["target"]
        return default

    def core_properties(self, meta):
        part = next((r["target"] for r in self.rels("") if r["kind"] == "core-properties"), "docProps/core.xml")
        values = {}
        if self.has(part):
            try:
                for element in self.xml(part):
                    values.setdefault(_local(element.tag), (element.text or "").strip())
            except (ET.ParseError, DocumentError):
                meta.setdefault("coverage_limitations", []).append("document_metadata:core_properties_unreadable")
        set_provenance(meta, author=values.get("creator"), created=values.get("created"),
                       modified=values.get("modified"), last_modified_by=values.get("lastModifiedBy"),
                       title=values.get("title"))


def _count_gaps(rels, gaps, *, where=None):
    for rel in rels:
        if rel["kind"] in GAP_RELATIONS:
            gaps[rel["kind"]] = gaps.get(rel["kind"], 0) + 1
            if where is not None:
                where.add(rel["kind"])


# ---------------------------------------------------------------------------
# docx
# ---------------------------------------------------------------------------
def _docx_paragraphs(root):
    """Paragraph texts in document order; text boxes follow their anchor paragraph."""
    lines = []

    def paragraph(p):
        parts, boxes = [], []

        def collect(element):
            for child in element:
                name = _local(child.tag)
                if name in {"Fallback", "moveFrom", "del", "instrText", "delText"}:
                    continue
                if name == "txbxContent":
                    boxes.append(child)
                    continue
                if _family(child.tag) == "wordprocessingml":
                    if name == "t":
                        parts.append(child.text or "")
                    elif name == "tab":
                        parts.append("\t")
                    elif name in {"br", "cr"}:
                        parts.append("\n")
                    elif name == "noBreakHyphen":
                        parts.append("-")
                collect(child)
        collect(p)
        line = "".join(parts)
        if line.strip():
            lines.append(line)
        for box in boxes:
            walk(box)

    def walk(element):
        for child in element:
            if _local(child.tag) == "Fallback":
                continue
            if _local(child.tag) == "p" and _family(child.tag) == "wordprocessingml":
                paragraph(child)
            else:
                walk(child)
    walk(root)
    return "\n".join(lines)


def _docx(raw, meta):
    pkg = _Package(raw)
    main = pkg.main_part("word/document.xml")
    doc = _Assembly()
    gaps, limitations = {}, []
    doc.add("body", _docx_paragraphs(pkg.xml(main)))
    rels = pkg.rels(main)
    _count_gaps(rels, gaps)
    seen = set()
    for kind in ("footnotes", "endnotes", "header", "footer"):
        for rel in rels:
            if rel["kind"] != kind or rel["external"] or not pkg.has(rel["target"]):
                continue
            text = _docx_paragraphs(pkg.xml(rel["target"]))
            _count_gaps(pkg.rels(rel["target"]), gaps)
            if text.strip() and text not in seen:
                seen.add(text)
                doc.add(kind, text, part=rel["target"])
    comments = [r for r in rels if r["kind"] == "comments" and not r["external"] and pkg.has(r["target"])]
    if comments:
        count = sum(1 for c in comments for e in pkg.xml(c["target"]) if _local(e.tag) == "comment")
        if count:
            limitations.append(f"document:comments_not_extracted:{count}")
    pkg.core_properties(meta)
    return _finish(meta, doc, gaps, limitations=limitations, fmt="docx")


# ---------------------------------------------------------------------------
# pptx
# ---------------------------------------------------------------------------
def _placeholder(shape):
    for element in shape.iter():
        if _local(element.tag) == "ph":
            return element.get("type", "body")
    return None


def _drawing_text(root, skip_placeholders):
    lines = []

    def walk(element):
        for child in element:
            name = _local(child.tag)
            if name == "Fallback":
                continue
            if name == "sp" and _placeholder(child) in skip_placeholders:
                continue
            if name == "p" and _family(child.tag) == "drawingml":
                parts = []
                for node in child.iter():
                    if _local(node.tag) == "t":
                        parts.append(node.text or "")
                    elif _local(node.tag) == "br":
                        parts.append("\n")
                line = "".join(parts)
                if line.strip():
                    lines.append(line)
                continue
            walk(child)
    walk(root)
    return "\n".join(lines)


def _pptx(raw, meta):
    pkg = _Package(raw)
    main = pkg.main_part("ppt/presentation.xml")
    rel_by_id = {r["id"]: r for r in pkg.rels(main)}
    slides = []
    for element in pkg.xml(main).iter():
        if _local(element.tag) == "sldId":
            rid = next((v for k, v in element.attrib.items() if _local(k) == "id" and k.startswith("{")), None)
            rel = rel_by_id.get(rid)
            if rel is None or rel["external"]:
                raise DocumentError("unresolved_slide_reference")
            slides.append(rel["target"])
    if not slides:  # presentation without a slide list: fall back to part numbering
        slides = sorted((n for n in pkg.names.values() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n, re.I)),
                        key=lambda n: int(re.search(r"(\d+)\.xml$", n)[1]))
    doc, gaps = _Assembly(), {}
    visual_slides, text_gap_slides = [], []
    for number, part in enumerate(slides, 1):
        root = pkg.xml(part)
        hidden = root.get("show") == "0"
        text = _drawing_text(root, {"sldNum", "dt"})
        rels = pkg.rels(part)
        kinds = set()
        _count_gaps(rels, gaps, where=kinds)
        attrs = {"slide": number, **({"hidden": True} if hidden else {})}
        doc.add("slide", text, **attrs)
        if kinds:
            visual_slides.append(number)
        if not text.strip():
            text_gap_slides.append(number)
        for rel in rels:
            if rel["kind"] == "notesSlide" and not rel["external"] and pkg.has(rel["target"]):
                notes = _drawing_text(pkg.xml(rel["target"]), {"sldNum", "dt", "hdr", "ftr", "sldImg"})
                _count_gaps(pkg.rels(rel["target"]), gaps)
                if notes.strip():
                    doc.add("notes", notes, slide=number)
    pkg.core_properties(meta)
    text, state = _finish(meta, doc, gaps, fmt="pptx")
    meta["document"].update(slides=len(slides), visual_slides=visual_slides, text_gap_slides=text_gap_slides)
    return text, state


# ---------------------------------------------------------------------------
# xlsx
# ---------------------------------------------------------------------------
_BUILTIN_DATE_FORMATS = set(range(14, 23)) | set(range(27, 37)) | set(range(45, 48)) | set(range(50, 59))


def _date_format(code):
    code = re.sub(r'"[^"]*"|\\.|\[[^\]]*\]', "", code or "").lower()
    return bool(re.search(r"[dy]", code) or re.search(r"[hs]", code))


def _serial(value, date1904):
    number = float(value)
    if not 0 <= number < 2958466:
        return value
    base = datetime(1904, 1, 1) if date1904 else datetime(1899, 12, 30 if number >= 61 else 31)
    moment = base + timedelta(days=number)
    moment = moment.replace(microsecond=0) + timedelta(seconds=round(moment.microsecond / 1e6))
    if number == int(number):
        return moment.date().isoformat()
    if number < 1:
        return moment.time().isoformat()
    return moment.isoformat()


def _xlsx(raw, meta):
    pkg = _Package(raw)
    main = pkg.main_part("xl/workbook.xml")
    rels = pkg.rels(main)
    by_id = {r["id"]: r for r in rels}
    book = pkg.xml(main)
    date1904 = any(_local(e.tag) == "workbookPr" and e.get("date1904") in {"1", "true"} for e in book.iter())
    shared = []
    for rel in rels:
        if rel["kind"] == "sharedStrings" and pkg.has(rel["target"]):
            for item in pkg.xml(rel["target"]):
                if _local(item.tag) != "si":
                    continue
                parts = []

                def collect(element):
                    for child in element:
                        if _local(child.tag) == "rPh":
                            continue
                        if _local(child.tag) == "t":
                            parts.append(child.text or "")
                        collect(child)
                collect(item)
                shared.append("".join(parts))
    date_styles = set()
    for rel in rels:
        if rel["kind"] == "styles" and pkg.has(rel["target"]):
            styles = pkg.xml(rel["target"])
            custom = {e.get("numFmtId"): e.get("formatCode") for e in styles.iter() if _local(e.tag) == "numFmt"}
            for block in styles:
                if _local(block.tag) == "cellXfs":
                    for index, xf in enumerate(x for x in block if _local(x.tag) == "xf"):
                        fmt = xf.get("numFmtId", "0")
                        if (fmt.isdigit() and int(fmt) in _BUILTIN_DATE_FORMATS) or _date_format(custom.get(fmt)):
                            date_styles.add(str(index))
    doc, gaps = _Assembly(), {}
    sheets = [e for e in book.iter() if _local(e.tag) == "sheet"]
    for number, sheet in enumerate(sheets, 1):
        rid = next((v for k, v in sheet.attrib.items() if _local(k) == "id" and k.startswith("{")), None)
        rel = by_id.get(rid)
        if rel is None or rel["external"]:
            raise DocumentError("unresolved_sheet_reference")
        attrs = {"sheet": number, "name": sheet.get("name", "")}
        if sheet.get("state") in {"hidden", "veryHidden"}:
            attrs["hidden"] = True
        if rel["kind"] != "worksheet":
            gaps[rel["kind"]] = gaps.get(rel["kind"], 0) + 1  # chartsheet/dialogsheet: no cell text
            doc.add("sheet", "", **attrs)
            continue
        rows = []
        for row in pkg.xml(rel["target"]).iter():
            if _local(row.tag) != "row":
                continue
            values = []
            for cell in row:
                if _local(cell.tag) != "c":
                    continue
                kind, raw_value = cell.get("t", "n"), None
                for child in cell:
                    if _local(child.tag) == "v":
                        raw_value = child.text
                    elif _local(child.tag) == "is":
                        raw_value = "".join(t.text or "" for t in child.iter()
                                            if _local(t.tag) == "t")
                if raw_value is None or kind == "e":
                    continue
                if kind == "s":
                    value = shared[int(raw_value)]
                elif kind == "b":
                    value = "TRUE" if raw_value.strip() == "1" else "FALSE"
                elif kind in {"str", "inlineStr", "d"}:
                    value = raw_value
                elif cell.get("s") in date_styles:
                    value = _serial(raw_value, date1904)
                else:
                    value = raw_value
                if value.strip():
                    values.append(value)
            if values:
                rows.append("\t".join(values))
        for sheet_rel in pkg.rels(rel["target"]):
            if sheet_rel["kind"] == "drawing" and not sheet_rel["external"] and pkg.has(sheet_rel["target"]):
                _count_gaps(pkg.rels(sheet_rel["target"]), gaps)
            elif sheet_rel["kind"] in GAP_RELATIONS:
                _count_gaps([sheet_rel], gaps)
        doc.add("sheet", "\n".join(rows), **attrs)
    pkg.core_properties(meta)
    return _finish(meta, doc, gaps, fmt="xlsx",
                   limitations=["document:xlsx_cell_values_without_number_formatting"])


# ---------------------------------------------------------------------------
# html / epub content
# ---------------------------------------------------------------------------
_BLOCK = {"address", "article", "aside", "blockquote", "br", "dd", "details", "div", "dl", "dt",
          "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6",
          "header", "hr", "legend", "li", "main", "nav", "ol", "p", "pre", "section", "summary",
          "table", "tbody", "thead", "tfoot", "tr", "ul", "caption", "body", "html", "title"}
_SKIP = {"script", "style", "noscript", "template", "svg", "math", "iframe", "object", "head"}
_PRE_SPACE, _PRE_BREAK, _PRE_TAB = "\x01", "\x00", "\x02"


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skip, self.pre, self.title = [], [], 0, None
        self.in_title, self.title_parts = False, []
        self.meta, self.times, self.images = {}, [], 0

    def handle_starttag(self, tag, attrs):
        attrs = {k.lower(): (v or "") for k, v in attrs}
        if tag in {"img", "image"} or (tag == "input" and attrs.get("type") == "image"):
            self.images += 1
        if tag == "meta":
            key = (attrs.get("name") or attrs.get("property") or attrs.get("itemprop") or "").strip().lower()
            if key and attrs.get("content", "").strip():
                self.meta.setdefault(key, []).append(attrs["content"].strip())
            return
        if tag == "time" and attrs.get("datetime", "").strip():
            self.times.append({"value": attrs["datetime"].strip(),
                               "published": "pubdate" in attrs or attrs.get("itemprop", "").lower() == "datepublished",
                               "modified": attrs.get("itemprop", "").lower() == "datemodified"})
        if tag == "body" and "head" in self.skip:  # unclosed <head> must not hide the body
            self.skip = [t for t in self.skip if t != "head"]
        if tag == "title" and not self.skip_active(ignore_head=True):
            self.in_title = True
            return
        if tag in _SKIP:
            self.skip.append(tag)
            return
        if tag == "pre":
            self.pre += 1
        if tag in {"td", "th"}:
            self.parts.append("\t")
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_startendtag(self, tag, attrs):
        if tag in _SKIP:  # self-closing <svg/> opens nothing
            return
        self.handle_starttag(tag, attrs)
        if tag == "pre":
            self.pre = max(0, self.pre - 1)

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
            return
        if self.skip and tag == self.skip[-1]:
            self.skip.pop()
            return
        if tag in _SKIP:
            if tag in self.skip:
                while self.skip and self.skip.pop() != tag:
                    pass
            return
        if tag == "pre":
            self.pre = max(0, self.pre - 1)
        if tag in _BLOCK:
            self.parts.append("\n")

    def skip_active(self, ignore_head=False):
        return any(t != "head" for t in self.skip) if ignore_head else bool(self.skip)

    def handle_data(self, data):
        data = data.replace(_PRE_SPACE, "").replace(_PRE_BREAK, "").replace(_PRE_TAB, "")
        if self.in_title:
            self.title_parts.append(data)
            return
        if self.skip:
            return
        if self.pre:
            self.parts.append(data.replace(" ", _PRE_SPACE).replace("\n", _PRE_BREAK).replace("\t", _PRE_TAB))
        else:
            self.parts.append(re.sub(r"\s+", " ", data))

    def result(self):
        text = "".join(self.parts)
        text = re.sub(r"[ \t]*\n[ \t]*", "\n", text)
        text = re.sub(r" {2,}", " ", text)
        text = re.sub(r"\n{2,}", "\n", text).strip()  # one line per block, like docx paragraphs
        return text.replace(_PRE_SPACE, " ").replace(_PRE_BREAK, "\n").replace(_PRE_TAB, "\t")


def _decode_markup(raw):
    for bom, encoding in ((codecs.BOM_UTF8, "utf-8-sig"), (codecs.BOM_UTF16_LE, "utf-16"),
                          (codecs.BOM_UTF16_BE, "utf-16")):
        if raw.startswith(bom):
            return raw.decode(encoding)
    head = raw[:4096]
    match = (re.search(rb"<meta[^>]+charset\s*=\s*[\"']?\s*([A-Za-z0-9_\-:.]+)", head, re.I)
             or re.search(rb"<\?xml[^>]+encoding\s*=\s*[\"']([A-Za-z0-9_\-:.]+)", head, re.I))
    encoding = match[1].decode("ascii") if match else "utf-8"
    codecs.lookup(encoding)
    return raw.decode(encoding)


def _parse_markup(raw):
    parser = _HTMLText()
    parser.feed(_decode_markup(raw))
    parser.close()
    return parser


def _html(raw, meta):
    parser = _parse_markup(raw)
    first = lambda *keys: next((v for k in keys for v in parser.meta.get(k, [])), None)
    published = first("article:published_time", "datepublished")
    modified = first("article:modified_time", "datemodified")
    if not published:
        marked = [t["value"] for t in parser.times if t["published"]]
        distinct = list(dict.fromkeys(t["value"] for t in parser.times))
        if marked:
            published = marked[0]
        elif len(distinct) == 1:
            published = distinct[0]
        elif len(distinct) > 1:
            meta["document_dates_ambiguous"] = distinct[:20]
    if not modified:
        modified = next((t["value"] for t in parser.times if t["modified"]), None)
    set_provenance(meta, author=parser.meta.get("author", []) + parser.meta.get("dc.creator", []),
                   published=published, modified=modified,
                   title=" ".join("".join(parser.title_parts).split()))
    doc = _Assembly()
    doc.add("body", parser.result())
    limitations = [f"document:html_linked_images_not_extracted:{parser.images}"] if parser.images else []
    return _finish(meta, doc, {}, limitations=limitations, fmt="html")


def _epub(raw, meta):
    pkg = _Package(raw)
    container = pkg.xml("META-INF/container.xml")
    rootfile = next((e.get("full-path") for e in container.iter() if _local(e.tag) == "rootfile"), None)
    if not rootfile:
        raise DocumentError("missing_rootfile")
    opf = pkg.xml(rootfile)
    folder = posixpath.dirname(rootfile)
    creators, dates, modified, title = [], {}, None, None
    manifest, spine = {}, []
    for element in opf.iter():
        name = _local(element.tag)
        text = (element.text or "").strip()
        if name == "creator" and text:
            creators.append(text)
        elif name == "title" and text and title is None:
            title = text
        elif name == "date" and text:
            event = next((v for k, v in element.attrib.items() if _local(k) == "event"), "publication")
            dates.setdefault(event.lower(), text)
        elif name == "meta" and element.get("property") == "dcterms:modified" and text:
            modified = text
        elif name == "item":
            manifest[element.get("id")] = element
        elif name == "itemref":
            spine.append(element.get("idref"))
    set_provenance(meta, author=creators, published=dates.get("publication"),
                   created=dates.get("creation"), modified=modified or dates.get("modification"), title=title)
    doc, gaps = _Assembly(), {}
    for number, idref in enumerate(spine, 1):
        item = manifest.get(idref)
        if item is None:
            raise DocumentError("unresolved_spine_item")
        href = posixpath.normpath(posixpath.join(folder, unquote(item.get("href", "").split("#")[0])))
        media = (item.get("media-type") or "").lower()
        if "html" not in media and "xml" not in media:
            gaps[media.split("/")[0] or "unknown"] = gaps.get(media.split("/")[0] or "unknown", 0) + 1
            doc.add("chapter", "", chapter=number, href=href)
            continue
        parser = _parse_markup(pkg.read(href))
        if parser.images:
            gaps["image"] = gaps.get("image", 0) + parser.images
        doc.add("chapter", parser.result(), chapter=number, href=href)
    return _finish(meta, doc, gaps, fmt="epub")


# ---------------------------------------------------------------------------
# rtf
# ---------------------------------------------------------------------------
_RTF_TOKEN = re.compile(r"\\([a-zA-Z]{1,32})(-?\d{1,10})? ?|\\'([0-9a-fA-F]{2})|\\(.)|([{}])|[\r\n]+|([^\\{}\r\n]+)", re.S)
_RTF_SKIP = {"fonttbl", "colortbl", "stylesheet", "info", "pict", "object", "header", "headerl", "headerr",
             "headerf", "footer", "footerl", "footerr", "footerf", "fldinst", "listtable", "listoverridetable",
             "rsidtbl", "generator", "xmlnstbl", "latentstyles", "datastore", "themedata", "colorschememapping",
             "filetbl", "revtbl", "pgdsctbl", "mmathPr", "shp", "nonshppict", "operator", "author", "title",
             "comment", "creatim", "revtim", "printim", "buptim", "company", "manager", "keywords", "subject",
             "doccomm", "category"}
_RTF_WORDS = {"par": "\n", "line": "\n", "sect": "\n\n", "page": "\n\n", "tab": "\t", "cell": "\t",
              "row": "\n", "emdash": "\u2014", "endash": "\u2013", "bullet": "\u2022", "lquote": "\u2018",
              "rquote": "\u2019", "ldblquote": "\u201c", "rdblquote": "\u201d", "emspace": " ", "enspace": " "}
_RTF_SYMBOLS = {"~": "\u00a0", "_": "-", "-": "", "\\": "\\", "{": "{", "}": "}", "\n": "\n", "\r": "\n"}


def _rtf_text(source, codepage):
    """Return (text, gap_counts). Hidden destinations are skipped, pictures/objects counted."""
    out, pending, gaps = [], bytearray(), {}
    stack, skip, uc, to_skip, group_start = [], False, 1, 0, False

    def flush():
        if pending:
            out.append(pending.decode(codepage, errors="replace"))
            pending.clear()

    pos = 0
    while pos < len(source):
        match = _RTF_TOKEN.match(source, pos)
        if not match:
            raise DocumentError("rtf_unparseable")
        pos = match.end()
        word, arg, hexbyte, symbol, brace, plain = match.groups()
        if not (word or hexbyte or symbol or brace or plain):
            continue  # raw line breaks carry no meaning in RTF
        if brace:
            flush()
            to_skip = 0
            if brace == "{":
                stack.append((skip, uc))
                group_start = True
            else:
                if not stack:
                    break
                skip, uc = stack.pop()
            continue
        starting, group_start = group_start, False
        if word:
            if word == "bin" and arg:
                pos += max(0, int(arg))
                continue
            if to_skip:
                to_skip -= 1
                continue
            flush()
            if starting and word in _RTF_SKIP:
                if word in {"pict", "object", "shp"} and not skip:
                    kind = {"pict": "image", "object": "oleObject", "shp": "shape"}[word]
                    gaps[kind] = gaps.get(kind, 0) + 1
                skip = True
            elif word == "uc" and arg:
                uc = max(0, int(arg))
            elif word == "u" and arg and not skip:
                value = int(arg)
                out.append(chr(value + 65536 if value < 0 else value))
                to_skip = uc
            elif not skip and word in _RTF_WORDS:
                out.append(_RTF_WORDS[word])
        elif hexbyte:
            if to_skip:
                to_skip -= 1
                continue
            if not skip:
                pending.append(int(hexbyte, 16))
        elif symbol:
            if symbol == "*":
                if starting:
                    skip = True
                continue
            if to_skip:
                to_skip -= 1
                continue
            flush()
            if not skip:
                out.append(_RTF_SYMBOLS.get(symbol, ""))
        elif plain:
            if to_skip:
                consumed = min(to_skip, len(plain))
                plain, to_skip = plain[consumed:], to_skip - consumed
            flush()
            if not skip:
                out.append(plain)
    flush()
    text = "".join(out).encode("utf-16", "surrogatepass").decode("utf-16", errors="replace")
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip(), gaps


def _rtf_time(source, word):
    match = re.search(r"\{\\" + word + r"((?:\\[a-z]+-?\d+\s?)+)\}", source)
    if not match:
        return None
    parts = dict((k, int(v)) for k, v in re.findall(r"\\([a-z]+)(-?\d+)", match[1]))
    try:
        stamp = datetime(parts["yr"], parts.get("mo", 1), parts.get("dy", 1), parts.get("hr", 0), parts.get("min", 0))
    except (KeyError, ValueError):
        return None
    return stamp.isoformat() if ("hr" in parts or "min" in parts) else date(stamp.year, stamp.month, stamp.day).isoformat()


def _rtf(raw, meta):
    source = raw.decode("latin-1")
    if not source.lstrip().startswith("{\\rtf"):
        raise DocumentError("not_rtf")
    cp = re.search(r"\\ansicpg(\d+)", source[:4096])
    codepage = f"cp{cp[1]}" if cp else "cp1252"
    try:
        codecs.lookup(codepage)
    except LookupError:
        codepage = "cp1252"
    author = re.search(r"\{\\author\s?((?:[^{}\\]|\\'[0-9a-fA-F]{2}|\\u-?\d+\s?\??|\\[^a-zA-Z])*)\}", source)
    author_text = _rtf_text("{" + author[1] + "}", codepage)[0] if author else None
    set_provenance(meta, author=author_text, created=_rtf_time(source, "creatim"),
                   modified=_rtf_time(source, "revtim"))
    text, gaps = _rtf_text(source, codepage)
    doc = _Assembly()
    doc.add("body", text)
    return _finish(meta, doc, gaps, fmt="rtf",
                   limitations=["document:rtf_headers_footers_not_extracted"])


PARSERS = {".docx": _docx, ".pptx": _pptx, ".xlsx": _xlsx, ".html": _html, ".htm": _html,
           ".epub": _epub, ".rtf": _rtf}
_FAILURES = (zipfile.BadZipFile, zipfile.LargeZipFile, KeyError, IndexError, ET.ParseError, ValueError,
             UnicodeError, LookupError, OSError, EOFError, NotImplementedError, RuntimeError, zlib.error,
             RecursionError)


def extract(suffix, raw, meta):
    """Parse ``raw`` bytes of a supported document into (text, state); fills ``meta``."""
    parser = PARSERS[suffix]
    try:
        return parser(raw, meta)
    except _FAILURES as exc:
        for key in ("document", "author", "author_basis", "date", "date_basis", "document_dates",
                    "document_dates_rejected", "document_dates_ambiguous", "last_modified_by", "document_title"):
            meta.pop(key, None)
        set_provenance(meta)
        meta["error"] = str(exc) if isinstance(exc, DocumentError) else type(exc).__name__
        return "", "read_failed"
