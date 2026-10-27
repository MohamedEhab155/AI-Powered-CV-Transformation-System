# renderer.py
"""
Fill the fixed DOCX template (layout, fonts, colors, shapes) with data from cv.json.

The template file is only used for its design: page setup, the navy sidebar,
the green lines, styles, numbering and footer. ALL of its text is removed and
replaced by the sections and titles found in the original CV.

Usage:
    python renderer.py output/cv.json templates/template.docx output/cv_new.docx
"""
import json
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

from lxml import etree

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {"w": W}

# ---- Design values copied from the template -------------------------------
NAVY = "1B2A6B"       # sidebar fill, name, section titles
NAVY_DARK = "13204F"  # item headings
GREEN = "5FA83C"      # job title, dates, main section line
GREEN_LIGHT = "7CC144"  # sidebar section line
GREY = "3A3A3A"       # body text
MIN_ROW_HEIGHT = 13000  # twips; lower it if a short CV creates an empty 2nd page
RIGHT_TAB = 7500      # right edge of the main column (twips)

NUM_SIDEBAR = 49      # bullet list ids that already exist in the template
NUM_ITEM_HEADING = 26
NUM_BODY = 31

# A section goes to the sidebar when ONE of these is true:
#   1) its title contains one of SIDEBAR_KEYWORDS (e.g. "Personal Information", "Core Skills")
#   2) it is only a short list of lines (no dates / companies)
# You can also force a title by name in FORCE_SIDEBAR / FORCE_MAIN (case-insensitive).
SIDEBAR_KEYWORDS = ["personal", "skill", "language", "interest", "tool", "technolog",
                    "competenc", "contact", "hobbies", "strength"]
FORCE_SIDEBAR = []
FORCE_MAIN = []
SIDEBAR_MAX_LINES = 20
SIDEBAR_MAX_CHARS = 70

FONT = '<w:rFonts w:ascii="Mulish" w:eastAsia="Calibri" w:hAnsi="Mulish" w:cs="Calibri"/>'


# ---- XML helpers ----------------------------------------------------------
def _clean(s) -> str:
    return re.sub(r"\s+", " ", str(s)).strip() if s else ""


def _run(text, *, b=False, i=False, color=GREY, sz=22, caps=False, spc=None, tab=False):
    rpr = (
        FONT
        + ("<w:b/><w:bCs/>" if b else "")
        + ("<w:i/><w:iCs/>" if i else "")
        + ("<w:caps/>" if caps else "")
        + f'<w:color w:val="{color}"/>'
        + (f'<w:spacing w:val="{spc}"/>' if spc else "")
        + f'<w:sz w:val="{sz}"/><w:szCs w:val="{sz}"/>'
    )
    return (
        f"<w:r><w:rPr>{rpr}</w:rPr>"
        + ("<w:tab/>" if tab else "")
        + f'<w:t xml:space="preserve">{escape(text)}</w:t></w:r>'
    )


def _p(ppr: str, runs: str = "") -> str:
    return f"<w:p><w:pPr>{ppr}</w:pPr>{runs}</w:p>"


def _num(num_id: int) -> str:
    return (
        '<w:pStyle w:val="ListParagraph"/>'
        f'<w:numPr><w:ilvl w:val="0"/><w:numId w:val="{num_id}"/></w:numPr>'
    )


# ---- Sidebar blocks -------------------------------------------------------
def sidebar_title(text):
    ppr = (
        f'<w:pBdr><w:bottom w:val="single" w:sz="8" w:space="4" w:color="{GREEN_LIGHT}"/></w:pBdr>'
        '<w:spacing w:before="320" w:after="90"/>'
    )
    return _p(ppr, _run(text, b=True, color="FFFFFF", caps=True, spc=20))


def sidebar_line(text, bullet=True, bold=False):
    if bullet:
        ppr = _num(NUM_SIDEBAR) + '<w:spacing w:after="50"/>'
    else:
        ppr = '<w:keepNext/><w:spacing w:before="60" w:after="40"/>'
    return _p(ppr, _run(text, b=bold, color="FFFFFF"))


def sidebar_section(sec):
    out = [sidebar_title(_clean(sec["title"]))]
    for it in sec.get("items") or []:
        heading = _clean(it.get("heading"))
        sub = _clean(it.get("subheading"))
        date = _clean(it.get("date"))
        loc = _clean(it.get("location"))
        text = _clean(it.get("text"))
        bullets = [_clean(b) for b in (it.get("bullets") or []) if _clean(b)]

        head_line = heading
        extra = " | ".join(x for x in (sub, date, loc) if x)
        if bullets or (text and heading and len(text) > 60):
            if head_line:
                out.append(sidebar_line(head_line, bullet=False, bold=True))
            if extra:
                out.append(sidebar_line(extra, bullet=False))
        else:
            parts = [x for x in (head_line, extra) if x]
            if parts:
                out.append(sidebar_line(" - ".join(parts)))
        if text:
            out.append(sidebar_line(text, bullet=len(text) <= 60))
        for b in bullets:
            out.append(sidebar_line(b))
    return out


def sidebar_contact(lines):
    """Contact details at the top of the sidebar (no title, so no invented text)."""
    out = []
    for n, line in enumerate(lines):
        ppr = ("" if n else "") + '<w:spacing w:after="70"/>'
        out.append(_p(ppr, _run(line, color="FFFFFF", sz=20)))
    return out


# ---- Main column blocks ---------------------------------------------------
def main_name(text):
    ppr = '<w:spacing w:after="20"/>'
    return _p(ppr, _run(text, b=True, color=NAVY, sz=44))


def main_job_title(text, border=True):
    ppr = (
        (
            '<w:pBdr><w:bottom w:val="single" w:sz="4" w:space="8" w:color="D9DBD6"/></w:pBdr>'
            if border
            else ""
        )
        + f'<w:spacing w:after="{200 if border else 60}"/>'
    )
    return _p(ppr, _run(text, color=GREEN, sz=28, spc=10))


def main_contact(text):
    ppr = (
        '<w:pBdr><w:bottom w:val="single" w:sz="4" w:space="8" w:color="D9DBD6"/></w:pBdr>'
        '<w:spacing w:after="200"/>'
    )
    return _p(ppr, _run(text, color=GREY, sz=20))


def main_title(text):
    ppr = (
        "<w:keepNext/>"
        f'<w:pBdr><w:bottom w:val="single" w:sz="12" w:space="4" w:color="{GREEN}"/></w:pBdr>'
        '<w:spacing w:before="300" w:after="120"/>'
    )
    return _p(ppr, _run(text, b=True, color=NAVY, sz=24, caps=True, spc=14))


def main_body(text):
    ppr = '<w:spacing w:after="120" w:line="276" w:lineRule="auto"/><w:jc w:val="both"/>'
    return _p(ppr, _run(text))


def main_bullet(text):
    ppr = _num(NUM_BODY) + '<w:spacing w:after="60" w:line="264" w:lineRule="auto"/><w:jc w:val="both"/>'
    return _p(ppr, _run(text))


def main_item_heading(heading, date):
    ppr = (
        _num(NUM_ITEM_HEADING).replace("<w:numPr>", "<w:keepNext/><w:numPr>")
        + f'<w:tabs><w:tab w:val="right" w:pos="{RIGHT_TAB}"/></w:tabs>'
        '<w:spacing w:before="160" w:after="40"/>'
    )
    runs = _run(heading, b=True, color=NAVY_DARK)
    if date:
        runs += _run(date, i=True, color=GREEN, tab=True)
    return _p(ppr, runs)


def main_date_only(date):
    ppr = '<w:keepNext/><w:spacing w:before="160" w:after="40"/>'
    return _p(ppr, _run(date, i=True, color=GREEN))


def main_subline(text):
    ppr = '<w:keepNext/><w:spacing w:after="80"/>'
    return _p(ppr, _run(text, i=True))


def main_section(sec):
    out = [main_title(_clean(sec["title"]))]
    for it in sec.get("items") or []:
        heading = _clean(it.get("heading"))
        sub = _clean(it.get("subheading"))
        date = _clean(it.get("date"))
        loc = _clean(it.get("location"))
        text = _clean(it.get("text"))
        bullets = [_clean(b) for b in (it.get("bullets") or []) if _clean(b)]

        if heading:
            out.append(main_item_heading(heading, date))
        elif date:
            out.append(main_date_only(date))
        subline = " | ".join(x for x in (sub, loc) if x)
        if subline:
            out.append(main_subline(subline))
        if text:
            out.append(main_body(text))
        for b in bullets:
            out.append(main_bullet(b))
    return out


# ---- Which column? --------------------------------------------------------
def goes_to_sidebar(sec, loose=False) -> bool:
    title = _clean(sec.get("title")).lower()
    if title in [t.lower() for t in FORCE_MAIN]:
        return False
    if title in [t.lower() for t in FORCE_SIDEBAR]:
        return True
    items = sec.get("items") or []
    if not items:
        return False
    if any(k in title for k in SIDEBAR_KEYWORDS):
        return True
    if not loose:  # the short-list rule is only a fallback when no title matched
        return False
    lines = 0
    for it in items:
        if any(it.get(k) for k in ("date", "subheading", "location")):
            return False
        for line in [it.get("heading"), it.get("text")] + list(it.get("bullets") or []):
            line = _clean(line)
            if line:
                lines += 1
                if len(line) > SIDEBAR_MAX_CHARS:
                    return False
    return 0 < lines <= SIDEBAR_MAX_LINES


# ---- Build the document ---------------------------------------------------
def _parse_fragments(xml_strings):
    wrapper = f'<root xmlns:w="{W}">' + "".join(xml_strings) + "</root>"
    return list(etree.fromstring(wrapper.encode("utf-8")))


def plan_columns(sections) -> list:
    """Return 'sidebar' or 'main' for each section (used by the renderer and the web UI)."""
    loose = not any(goes_to_sidebar(s) for s in sections)  # fallback if nothing matched
    return ["sidebar" if goes_to_sidebar(s, loose=loose) else "main" for s in sections]


def render_cv(cv: dict, template_path: str, output_path: str, layout=None) -> str:
    """
    layout (optional): list of (section_index, "sidebar" | "main") in the order the
    user wants. Indexes refer to the CV sections that have a title. If it is None,
    the original order and the automatic sidebar rule are used.
    """
    with zipfile.ZipFile(template_path) as z:
        names = z.namelist()
        files = {n: z.read(n) for n in names}

    root = etree.fromstring(files["word/document.xml"])
    tbl = root.find(".//w:tbl", NS)
    rows = tbl.findall("w:tr", NS)
    side_cell, main_cell = rows[1].findall("w:tc", NS)

    # 0) make the sidebar fill the page even when the CV is short
    if rows[1].find("w:trPr", NS) is None:
        trpr = etree.fromstring(
            f'<w:trPr xmlns:w="{W}"><w:trHeight w:val="{MIN_ROW_HEIGHT}" w:hRule="atLeast"/></w:trPr>'
        )
        rows[1].insert(0, trpr)

    # 1) remove ALL template text (and the sample logo)
    for cell in (side_cell, main_cell):
        for p in cell.findall("w:p", NS):
            cell.remove(p)

    # 2) header
    main, side = [], []
    name = _clean(cv.get("name"))
    job = _clean(cv.get("job_title"))
    c = cv.get("contact") or {}
    contact_parts = [_clean(c.get("email")), _clean(c.get("phone")), _clean(c.get("location"))]
    contact_parts += [_clean(x) for x in (c.get("links") or [])]
    contact = "  |  ".join(x for x in contact_parts if x)

    if name:
        main.append(main_name(name))
    if job:
        main.append(main_job_title(job, border=True))
    if contact_parts and any(contact_parts):
        side.extend(sidebar_contact([x for x in contact_parts if x]))

    # 3) sections, same order as the original CV
    sections = [s for s in (cv.get("sections") or []) if _clean(s.get("title"))]
    if layout is None:
        layout = list(enumerate(plan_columns(sections)))
    for idx, column in layout:
        sec = sections[idx]
        if column == "sidebar":
            print(f"  [sidebar] {sec['title']}")
            side.extend(sidebar_section(sec))
        else:
            print(f"  [main]    {sec['title']}")
            main.extend(main_section(sec))

    for cell, blocks in ((side_cell, side), (main_cell, main)):
        # keep-with-next on the very first paragraph of a cell pushes the whole
        # row to page 2 in some renderers, so remove it there.
        if blocks:
            blocks[0] = blocks[0].replace("<w:keepNext/>", "", 1)
        for el in _parse_fragments(blocks):
            cell.append(el)
        if not cell.findall("w:p", NS):  # a cell must end with a paragraph
            etree.SubElement(cell, f"{{{W}}}p")

    files["word/document.xml"] = etree.tostring(
        root, xml_declaration=True, encoding="UTF-8", standalone=True
    )

    # 4) drop media that is no longer used (old sample logo), keep the fixed logos
    doc_xml = files["word/document.xml"].decode("utf-8")
    rels = files["word/_rels/document.xml.rels"].decode("utf-8")
    for m in re.finditer(r'<Relationship [^>]*/>', rels):
        rel = m.group(0)
        target = re.search(r'Target="(media/[^"]*)"', rel)
        rid = re.search(r'Id="([^"]*)"', rel)
        if target and rid and f'"{rid.group(1)}"' not in doc_xml:
            rels = rels.replace(rel, "")
            files.pop("word/" + target.group(1), None)
    files["word/_rels/document.xml.rels"] = rels.encode("utf-8")
    if "docProps/core.xml" in files:
        core = files["docProps/core.xml"].decode("utf-8")
        for tag in ("dc:title", "dc:creator", "cp:lastModifiedBy"):
            value = escape(name) if tag != "dc:title" else escape(name + " - CV" if name else "CV")
            core = re.sub(rf"<{tag}>.*?</{tag}>", f"<{tag}>{value}</{tag}>", core, flags=re.S)
        files["docProps/core.xml"] = core.encode("utf-8")

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as z:
        for n in names:
            if n in files:
                z.writestr(n, files[n])
    return output_path


def to_pdf(docx_path: str):
    """Optional: convert to PDF. Uses Word (docx2pdf) or LibreOffice if found."""
    docx_path = str(docx_path)
    pdf_path = str(Path(docx_path).with_suffix(".pdf"))
    try:
        try:
            import pythoncom  # Windows only: needed when called from a web-server thread

            pythoncom.CoInitialize()
        except ImportError:
            pass
        from docx2pdf import convert  # pip install docx2pdf (needs MS Word)

        convert(docx_path, pdf_path)
        return pdf_path
    except Exception:
        pass
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if soffice:
        subprocess.run(
            [soffice, "--headless", "--convert-to", "pdf", "--outdir", str(Path(docx_path).parent), docx_path],
            check=True,
        )
        return pdf_path
    print("PDF export skipped: install 'docx2pdf' (needs Word) or LibreOffice.")
    return None


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("Usage: python renderer.py cv.json template.docx output.docx")
        sys.exit(1)
    with open(sys.argv[1], encoding="utf-8") as f:
        data = json.load(f)
    print("Saved", render_cv(data, sys.argv[2], sys.argv[3]))