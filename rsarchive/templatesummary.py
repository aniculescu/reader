#!/usr/bin/env python3
"""
rsarchive.templatesummary — a readable, diffable summary of a ``.docx`` template.

``templates/print-template.docx`` is the versioned source of the print layout. It is
edited in Word, straight from the repo. Git can't diff a Word file, so this summary is
committed next to it as ``templates/print-template.styles.md``. It covers page setup,
document defaults, every style's paragraph and character settings in readable units,
footers, fonts, theme fonts, the settings that affect layout, and media. Each template
change then shows up in review as plain text, for example "FootnoteText: spacing after
0pt → 6pt".

Word's per-save noise (revision ids, paragraph ids, timestamps) is left out, so a save
with no real change produces the same summary. Regenerate it after every template edit::

    python3 -m rsarchive.templatesummary templates/print-template.docx > templates/print-template.styles.md

``rsarchive/tests/test_template.py`` fails if the committed summary is out of date, or
if the template carries personal paths or a defect Word would reject. Pure stdlib.
"""

from __future__ import annotations

import hashlib
import sys
import zipfile
from typing import Dict, List, Optional
from xml.etree import ElementTree as ET

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PKG = "{http://schemas.openxmlformats.org/package/2006/relationships}"

# Word's per-save bookkeeping, never a layout change.
_NOISE = {"rsid", "rsidR", "rsidRDefault", "rsidP", "rsidRPr", "rsidSect", "rsidTr",
          "rsidDel", "rsidRoot", "uiPriority", "unhideWhenUsed", "semiHidden", "qFormat",
          "locked", "personal", "personalCompose", "personalReply"}


# On/off properties, where val="0"/"false" switches the property off. Every other
# property's val is a value in its own right (outlineLvl 0 is heading level 1).
_TOGGLES = {"b", "bCs", "i", "iCs", "caps", "smallCaps", "strike", "dstrike", "vanish",
            "webHidden", "specVanish", "noProof", "outline", "shadow", "emboss", "imprint",
            "rtl", "cs", "keepNext", "keepLines", "pageBreakBefore", "widowControl",
            "suppressLineNumbers", "suppressAutoHyphens", "contextualSpacing", "mirrorIndents",
            "suppressOverlap", "snapToGrid", "kinsoku", "wordWrap", "overflowPunct",
            "topLinePunct", "autoSpaceDE", "autoSpaceDN", "bidi", "adjustRightInd", "titlePg",
            "evenAndOddHeaders", "mirrorMargins", "gutterAtTop", "autoHyphenation"}


def _num(v: str, div: float, unit: str, places: int = 2) -> str:
    try:
        x = round(int(v) / div, places)
    except ValueError:
        return v
    return ("%g" % x) + unit


def _pt(v: str) -> str:     # twentieths of a point
    return _num(v, 20, "pt", 1)


def _cm(v: str) -> str:     # twips
    return _num(v, 567, "cm")


def _mm(v: str) -> str:     # twips
    return _num(v, 56.692913, "mm", 1)


def _half(v: str) -> str:   # half-points
    return _num(v, 2, "pt", 1)


def _line(v: str, rule: str) -> str:
    if rule in ("", "auto"):
        return "×" + _num(v, 240, "", 2)
    return _pt(v) + (" exactly" if rule == "exact" else " at least")


def _attrs(el) -> Dict[str, str]:
    return {k.replace(W, ""): v for k, v in el.attrib.items()
            if k.startswith(W) and k.replace(W, "") not in _NOISE}


def _describe(el) -> Optional[str]:
    """One property element as 'name: value' in readable units (None for noise)."""
    name = el.tag.replace(W, "")
    if not el.tag.startswith(W) or name in _NOISE:
        return None
    a = _attrs(el)
    if name == "spacing":
        parts = []
        for k in ("before", "after"):
            if k in a:
                parts.append("%s %s" % (k, _pt(a[k])))
            if k + "Autospacing" in a and a[k + "Autospacing"] in ("1", "true", "on"):
                parts.append("%s auto" % k)
        if "line" in a:
            parts.append("line " + _line(a["line"], a.get("lineRule", "")))
        return "spacing: " + ", ".join(parts)
    if name == "ind":
        return "indent: " + ", ".join("%s %s" % (k, _cm(v)) for k, v in sorted(a.items())
                                      if k in ("left", "right", "start", "end", "firstLine", "hanging"))
    if name in ("sz", "szCs", "kern"):
        return "%s: %s" % (name, _half(a.get("val", "")))
    if name == "rFonts":
        return "fonts: " + ", ".join("%s=%s" % (k, v) for k, v in sorted(a.items()))
    if name == "tabs":
        tabs = [_attrs(t) for t in el.findall(W + "tab")]
        return "tabs: " + "; ".join("%s at %s%s" % (t.get("val", ""), _cm(t.get("pos", "0")),
                                                     (", " + t["leader"] + " leader") if "leader" in t else "")
                                    for t in tabs)
    if name in ("pBdr", "rPr", "pPr", "numPr", "shd") and len(el):
        inner = [d for d in (_describe(c) for c in el) if d]
        return "%s: %s" % (name, "; ".join(inner)) if inner else None
    if not a:
        return name
    if list(a) == ["val"]:
        v = a["val"]
        if name in _TOGGLES and v in ("0", "false", "off"):
            return "no " + name
        if name in _TOGGLES and v in ("1", "true", "on"):
            return name
        return "%s: %s" % (name, v)
    return "%s: %s" % (name, ", ".join("%s=%s" % (k, v) for k, v in sorted(a.items())))


def _block(el, heading: str) -> List[str]:
    if el is None:
        return []
    lines = [d for d in (_describe(c) for c in el) if d]
    return ["- %s: %s" % (heading, x) for x in lines]


def _text(p) -> str:
    """Visible text, with fields shown as {INSTRUCTION} and their cached results dropped."""
    out, in_result = [], False
    for node in p.iter():
        if node.tag == W + "fldChar":
            kind = node.get(W + "fldCharType")
            in_result = kind == "separate" if kind in ("separate", "end") else in_result
        elif node.tag == W + "t" and node.text and not in_result:
            out.append(node.text)
        elif node.tag == W + "instrText" and node.text:
            out.append("{%s}" % node.text.strip())
        elif node.tag == W + "fldSimple":
            out.append("{%s}" % node.get(W + "instr", "").strip())
        elif node.tag == W + "tab":
            out.append("→")
    return "".join(out)


def summarize(path: str) -> str:
    z = zipfile.ZipFile(path)
    names = set(z.namelist())
    read = lambda n: ET.fromstring(z.read(n))    # noqa: E731
    out = ["# Template summary: %s" % path.split("/")[-1], "",
           "Generated by `python3 -m rsarchive.templatesummary`; do not edit by hand.", ""]

    doc = read("word/document.xml")
    rels = {r.get("Id"): r.get("Target") for r in read("word/_rels/document.xml.rels")}
    sect = doc.find(W + "body/" + W + "sectPr")
    out += ["## Page setup", ""]
    if sect is not None:
        size, mar = sect.find(W + "pgSz"), sect.find(W + "pgMar")
        if size is not None:
            a = _attrs(size)
            out.append("- page: %s × %s%s" % (_mm(a.get("w", "0")), _mm(a.get("h", "0")),
                                              ", " + a["orient"] if "orient" in a else ""))
        if mar is not None:
            out.append("- margins: " + ", ".join("%s %s" % (k, _mm(v)) for k, v in sorted(_attrs(mar).items())))
        for c in sect:
            d = _describe(c)
            if d and c.tag not in (W + "pgSz", W + "pgMar", W + "headerReference", W + "footerReference"):
                out.append("- " + d)
        for ref in sect:
            if ref.tag in (W + "headerReference", W + "footerReference"):
                kind = ref.tag.replace(W, "").replace("Reference", "")
                target = rels.get(ref.get(R + "id"), "?")
                part = read("word/" + target) if "word/" + target in names else None
                body = " / ".join(filter(None, (_text(p) for p in part.iter(W + "p")))) if part is not None else ""
                out.append("- %s (%s): %s" % (kind, ref.get(W + "type", "default"), body or "(empty)"))
    body_ps = doc.findall(W + "body/" + W + "p")
    out += ["- body: %d paragraph(s), %d with text" % (len(body_ps), sum(1 for p in body_ps if _text(p))), ""]

    styles = read("word/styles.xml")
    dd = styles.find(W + "docDefaults")
    if dd is not None:
        out += ["## Document defaults", ""]
        out += _block(dd.find(W + "pPrDefault/" + W + "pPr"), "paragraph")
        out += _block(dd.find(W + "rPrDefault/" + W + "rPr"), "character")
        out.append("")
    order = {"paragraph": 0, "character": 1, "numbering": 2, "table": 3}
    by_type: Dict[str, list] = {}
    for s in styles.findall(W + "style"):
        by_type.setdefault(s.get(W + "type", "?"), []).append(s)
    for kind in sorted(by_type, key=lambda k: order.get(k, 9)):
        out += ["## %s styles" % kind.capitalize(), ""]
        for s in sorted(by_type[kind], key=lambda s: s.get(W + "styleId", "")):
            sid = s.get(W + "styleId", "")
            name = s.find(W + "name")
            meta = []
            for tag in ("basedOn", "next", "link"):
                e = s.find(W + tag)
                if e is not None:
                    meta.append("%s %s" % ({"basedOn": "based on", "next": "next", "link": "linked"}[tag],
                                           e.get(W + "val")))
            if s.get(W + "default") in ("1", "true"):
                meta.append("default")
            head = "%s (\"%s\")" % (sid, name.get(W + "val") if name is not None else sid)
            if kind == "table":
                out.append("- %s%s" % (head, ": " + ", ".join(meta) if meta else ""))
                continue
            out += ["### " + head, ""]
            if meta:
                out.append("- " + ", ".join(meta))
            out += _block(s.find(W + "pPr"), "paragraph")
            out += _block(s.find(W + "rPr"), "character")
            out.append("")
        if kind == "table":
            out.append("")

    out += ["## Fonts", ""]
    for f in read("word/fontTable.xml").findall(W + "font"):
        emb = [c.tag.replace(W + "embed", "").lower() for c in f if c.tag.startswith(W + "embed")]
        out.append("- %s%s" % (f.get(W + "name"), " (embedded: %s)" % ", ".join(emb) if emb else ""))
    theme = [n for n in names if n.startswith("word/theme/")]
    if theme:
        t = read(sorted(theme)[0])
        for kind in ("major", "minor"):
            latin = t.find(".//%s%sFont/%slatin" % (A, kind, A))
            if latin is not None:
                out.append("- theme %s font: %s" % (kind, latin.get("typeface")))
    out.append("")

    out += ["## Settings", ""]
    s = read("word/settings.xml")
    for tag in ("defaultTabStop", "evenAndOddHeaders", "mirrorMargins", "gutterAtTop",
                "autoHyphenation", "hyphenationZone", "footnotePr", "endnotePr", "characterSpacingControl"):
        e = s.find(W + tag)
        if e is not None:
            out.append("- " + (("defaultTabStop: " + _cm(e.get(W + "val", "0"))) if tag == "defaultTabStop"
                               else (_describe(e) or tag)))
    for cs in s.iter(W + "compatSetting"):
        if cs.get(W + "name") == "compatibilityMode":
            out.append("- compatibility mode: %s" % cs.get(W + "val"))
    attached = s.find(W + "attachedTemplate")
    out += ["- attached template: %s" % ("yes" if attached is not None else "none"), ""]

    out += ["## Media", ""]
    for n in sorted(x for x in names if x.startswith("word/media/")):
        data = z.read(n)
        out.append("- %s: %d bytes, sha256 %s" % (n[len("word/media/"):], len(data), hashlib.sha256(data).hexdigest()[:12]))
    out.append("")
    return "\n".join(out).rstrip("\n") + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python3 -m rsarchive.templatesummary TEMPLATE.docx", file=sys.stderr)
        return 2
    sys.stdout.write(summarize(args[0]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
