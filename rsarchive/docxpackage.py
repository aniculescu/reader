#!/usr/bin/env python3
"""
rsarchive.docxpackage — build ``.docx`` packages that Word opens, and refuse ones it wouldn't.

Word is strict where LibreOffice is lenient. It rejects a property element whose
children are out of schema order, a single-use property given twice, and parts and
relationships that don't line up. It also asks to update fields on every open when
``updateFields`` is set. On 2026-10-30 GA 170 had all of these, and the way the file was
generated introduced every one of them:

  * a helper that appended each property to the end of ``<w:pPr>``;
  * a template merge that combined two packages wholesale;
  * a script that switched ``updateFields`` on.

So generate correctly:

  * :func:`add` puts a new child where ``wml.xsd`` says it goes. :func:`put` does the same
    for a single-use property, replacing any existing one. Use them instead of
    ``SubElement``/``append``/``insert(0)`` on WordprocessingML elements. String builders
    must write children in the order of the table below.
  * :func:`adopt` brings one part of another package into a package. Its relationships,
    the parts they reach and the content types they need all come with it, and it is
    linked in. Build from one package (normally the template, as docxbuild does) and
    adopt what else is needed. Never merge two packages wholesale.
  * :func:`write`, :func:`repackage` and :func:`verify` check the result first. They raise
    :class:`DocxPackageError` naming every defect that this write *introduced*, so a
    generator bug surfaces where it is made, not as a Word error later. Defects that
    were already in the source package are listed by ``docx-check``, but they don't
    block an unrelated edit.

``python3 -m rsarchive.cli docx-check FILE...`` lists a package's defects. Pure stdlib.
"""

from __future__ import annotations

import argparse
import os
import posixpath
import re
import sys
import zipfile
from collections import Counter
from typing import Dict, Iterable, List, Optional, Union
from urllib.parse import unquote
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
PKG_RELS = "http://schemas.openxmlformats.org/package/2006/relationships"
CONTENT_TYPES = "http://schemas.openxmlformats.org/package/2006/content-types"
RT = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
CT_PART = "[Content_Types].xml"
ROOT_RELS = "_rels/.rels"

Parts = Dict[str, bytes]

# ------------------------------------------------------------------ schema child order

# Generated from ISO/IEC 29500-4:2016 wml.xsd. Each child slot is separated by a space.
# '|' joins elements that share one slot (an xsd:choice: any order among them), and '+'
# marks an element that may repeat; any other single-element slot is single-use. '*'
# stands for every element not named. Elements from other namespaces (w14:, w15:, ...)
# that the schema doesn't place belong at the end. A container with an unknown w: or
# mc: child isn't judged.
_CHILD_ORDER = {
    'pPr': (
        'pStyle keepNext keepLines pageBreakBefore framePr widowControl numPr '
        'suppressLineNumbers pBdr shd tabs suppressAutoHyphens kinsoku wordWrap '
        'overflowPunct topLinePunct autoSpaceDE autoSpaceDN bidi adjustRightInd '
        'snapToGrid spacing ind contextualSpacing mirrorIndents suppressOverlap jc '
        'textDirection textAlignment textboxTightWrap outlineLvl divId cnfStyle rPr '
        'sectPr pPrChange'
    ),
    'rPr': (
        'ins del moveFrom moveTo rStyle|rFonts|b|bCs|i|iCs|caps|smallCaps|strike|dstrike|'
        'outline|shadow|emboss|imprint|noProof|snapToGrid|vanish|webHidden|color|spacing|'
        'w|kern|position|sz|szCs|highlight|u|effect|bdr|shd|fitText|vertAlign|rtl|cs|em|'
        'lang|eastAsianLayout|specVanish|oMath rPrChange'
    ),
    'sectPr': (
        'headerReference|footerReference footnotePr endnotePr type pgSz pgMar paperSrc '
        'pgBorders lnNumType pgNumType cols formProt vAlign noEndnote titlePg '
        'textDirection bidi rtlGutter docGrid printerSettings sectPrChange'
    ),
    'style': (
        'name aliases basedOn next link autoRedefine hidden uiPriority semiHidden '
        'unhideWhenUsed qFormat locked personal personalCompose personalReply rsid pPr '
        'rPr tblPr trPr tcPr tblStylePr+'
    ),
    'tblPr': (
        'tblStyle tblpPr tblOverlap bidiVisual tblStyleRowBandSize tblStyleColBandSize '
        'tblW jc tblCellSpacing tblInd tblBorders shd tblLayout tblCellMar tblLook '
        'tblCaption tblDescription tblPrChange'
    ),
    'tcPr': (
        'cnfStyle tcW gridSpan hMerge vMerge tcBorders shd noWrap tcMar textDirection '
        'tcFitText vAlign hideMark headers cellIns|cellDel|cellMerge tcPrChange'
    ),
    'trPr': (
        'cnfStyle|divId|gridBefore|gridAfter|wBefore|wAfter|cantSplit|trHeight|tblHeader|'
        'tblCellSpacing|jc|hidden ins del trPrChange'
    ),
    'settings': (
        'writeProtection view zoom removePersonalInformation removeDateAndTime '
        'doNotDisplayPageBoundaries displayBackgroundShape printPostScriptOverText '
        'printFractionalCharacterWidth printFormsData embedTrueTypeFonts '
        'embedSystemFonts saveSubsetFonts saveFormsData mirrorMargins '
        'alignBordersAndEdges bordersDoNotSurroundHeader bordersDoNotSurroundFooter '
        'gutterAtTop hideSpellingErrors hideGrammaticalErrors activeWritingStyle+ '
        'proofState formsDesign attachedTemplate linkStyles stylePaneFormatFilter '
        'stylePaneSortMethod documentType mailMerge revisionView trackRevisions '
        'doNotTrackMoves doNotTrackFormatting documentProtection autoFormatOverride '
        'styleLockTheme styleLockQFSet defaultTabStop autoHyphenation '
        'consecutiveHyphenLimit hyphenationZone doNotHyphenateCaps showEnvelope '
        'summaryLength clickAndTypeStyle defaultTableStyle evenAndOddHeaders '
        'bookFoldRevPrinting bookFoldPrinting bookFoldPrintingSheets '
        'drawingGridHorizontalSpacing drawingGridVerticalSpacing '
        'displayHorizontalDrawingGridEvery displayVerticalDrawingGridEvery '
        'doNotUseMarginsForDrawingGridOrigin drawingGridHorizontalOrigin '
        'drawingGridVerticalOrigin doNotShadeFormData noPunctuationKerning '
        'characterSpacingControl printTwoOnOne strictFirstAndLastChars noLineBreaksAfter '
        'noLineBreaksBefore savePreviewPicture doNotValidateAgainstSchema saveInvalidXml '
        'ignoreMixedContent alwaysShowPlaceholderText doNotDemarcateInvalidXml '
        'saveXmlDataOnly useXSLTWhenSaving saveThroughXslt showXMLTags '
        'alwaysMergeEmptyNamespace updateFields hdrShapeDefaults footnotePr endnotePr '
        'compat docVars rsids m:mathPr attachedSchema+ themeFontLang clrSchemeMapping '
        'doNotIncludeSubdocsInStats doNotAutoCompressPictures forceUpgrade captions '
        'readModeInkLockDown smartTagType+ sl:schemaLibrary shapeDefaults '
        'doNotEmbedSmartTags decimalSymbol listSeparator'
    ),
    'compat': (
        'useSingleBorderforContiguousCells wpJustification noTabHangInd noLeading '
        'spaceForUL noColumnBalance balanceSingleByteDoubleByteWidth noExtraLineSpacing '
        'doNotLeaveBackslashAlone ulTrailSpace doNotExpandShiftReturn '
        'spacingInWholePoints lineWrapLikeWord6 printBodyTextBeforeHeader printColBlack '
        'wpSpaceWidth showBreaksInFrames subFontBySize suppressBottomSpacing '
        'suppressTopSpacing suppressSpacingAtTopOfPage suppressTopSpacingWP '
        'suppressSpBfAfterPgBrk swapBordersFacingPages convMailMergeEsc '
        'truncateFontHeightsLikeWP6 mwSmallCaps usePrinterMetrics '
        'doNotSuppressParagraphBorders wrapTrailSpaces footnoteLayoutLikeWW8 '
        'shapeLayoutLikeWW8 alignTablesRowByRow forgetLastTabAlignment '
        'adjustLineHeightInTable autoSpaceLikeWord95 noSpaceRaiseLower '
        'doNotUseHTMLParagraphAutoSpacing layoutRawTableWidth layoutTableRowsApart '
        'useWord97LineBreakRules doNotBreakWrappedTables doNotSnapToGridInCell '
        'selectFldWithFirstOrLastChar applyBreakingRules doNotWrapTextWithPunct '
        'doNotUseEastAsianBreakRules useWord2002TableStyleRules growAutofit useFELayout '
        'useNormalStyleForList doNotUseIndentAsNumberingTabStop '
        'useAltKinsokuLineBreakRules allowSpaceOfSameStyleInTable '
        'doNotSuppressIndentation doNotAutofitConstrainedTables '
        'autofitToFirstFixedWidthCell underlineTabInNumList displayHangulFixedWidth '
        'splitPgBreakAndParaMark doNotVertAlignCellWithSp '
        'doNotBreakConstrainedForcedTable doNotVertAlignInTxbx useAnsiKerningPairs '
        'cachedColBalance compatSetting+'
    ),
    'lvl': (
        'start numFmt lvlRestart pStyle isLgl suff lvlText lvlPicBulletId legacy lvlJc '
        'pPr rPr'
    ),
    'abstractNum': 'nsid multiLevelType tmpl name styleLink numStyleLink lvl+',
    'num': 'abstractNumId lvlOverride+',
    'numbering': 'numPicBullet+ abstractNum+ num+ numIdMacAtCleanup',
    'styles': 'docDefaults latentStyles style+',
    'docDefaults': 'rPrDefault pPrDefault',
    'footnotePr': 'pos numFmt numStart numRestart footnote+',
    'endnotePr': 'pos numFmt numStart numRestart endnote+',
    'font': (
        'altName panose1 charset family notTrueType pitch sig embedRegular embedBold '
        'embedItalic embedBoldItalic'
    ),
    'tblStylePr': 'pPr rPr tblPr trPr tcPr',
    'p': 'pPr *',
    'r': 'rPr *',
    'tc': 'tcPr *',
    'tr': 'tblPrEx trPr *',
    'body': '* sectPr',
}
_PREFIXES = {"": W,
             "m": "http://schemas.openxmlformats.org/officeDocument/2006/math",
             "sl": "http://schemas.openxmlformats.org/schemaLibrary/2006/main"}


def _compile(spec: str):
    ranks, single, star = {}, [], None
    for i, slot in enumerate(spec.split()):
        names = slot.split("|")
        for name in names:
            if name == "*":
                star = i
                continue
            prefix, _, local = name.rstrip("+").rpartition(":")
            ranks[(_PREFIXES[prefix], local)] = i
            if len(names) == 1 and not name.endswith("+"):
                single.append((_PREFIXES[prefix], local))
    return ranks, frozenset(single), star


_ORDERS = {tag: _compile(spec) for tag, spec in _CHILD_ORDER.items()}
_END = 10 ** 6


def _split(tag: str):
    if tag[:1] == "{":
        ns, _, local = tag[1:].partition("}")
        return ns, local
    return None, tag


def _spec(parent_tag):
    ns, local = _split(parent_tag)
    return _ORDERS.get(local) if ns == W else None


def _rank(spec, tag) -> Optional[int]:
    ranks, _, star = spec
    r = ranks.get(_split(tag))
    return star if r is None else r


def _child_ranks(spec, tags: List[str]) -> Optional[List[int]]:
    """Schema rank of each child; None if a child is a w:/mc: element the table lacks."""
    out = []
    for tag in tags:
        r = _rank(spec, tag)
        if r is None:
            if _split(tag)[0] in (W, MC):
                return None
            r = _END                      # extension element: belongs at the end
        out.append(r)
    return out


def _q(tag: str) -> str:
    return tag if tag[:1] == "{" else "{%s}%s" % (W, tag)


def _show(tag: str) -> str:
    ns, local = _split(tag)
    return "w:" + local if ns == W else local


# --------------------------------------------------------------- building elements

def insert(parent, child):
    """Insert ``child`` into ``parent`` at its schema position (after siblings sharing
    it) and return it. Works on ElementTree and lxml elements alike."""
    spec = _spec(parent.tag)
    r = _rank(spec, child.tag) if spec else None
    if r is not None:
        for i, c in enumerate(list(parent)):
            if isinstance(c.tag, str):            # lxml comments/PIs have callable tags
                rc = _rank(spec, c.tag)
                if (_END if rc is None else rc) > r:
                    parent.insert(i, child)
                    return child
    parent.append(child)
    return child


def add(parent, tag: str, attrib: Optional[Dict[str, str]] = None, **attrs):
    """Create ``<w:tag>`` (``attrs`` become ``w:`` attributes; ``attrib`` keys are used
    as given, e.g. xml:space) and insert it at its schema position in ``parent``."""
    values = {_q(k): str(v) for k, v in attrs.items()}
    values.update(attrib or {})
    return insert(parent, parent.makeelement(_q(tag), values))


def put(parent, tag: str, attrib: Optional[Dict[str, str]] = None, **attrs):
    """Like :func:`add`, for a single-use property: any existing ``<w:tag>`` is removed
    first, so the property is set, never doubled."""
    for old in [c for c in parent if c.tag == _q(tag)]:
        parent.remove(old)
    return add(parent, tag, attrib, **attrs)


# --------------------------------------------------------------- package plumbing

def _rels_path(part: str) -> str:
    folder, _, base = part.rpartition("/")
    return (folder + "/" if folder else "") + "_rels/" + base + ".rels"


def _rels_source(rels_path: str) -> str:
    """'word/_rels/document.xml.rels' -> 'word/document.xml'; '_rels/.rels' -> ''."""
    folder, _, base = rels_path.rpartition("/")
    return posixpath.join(posixpath.dirname(folder), base[:-len(".rels")]) if base != ".rels" else ""


def _resolve(source: str, target: str) -> str:
    target = unquote(target.split("#", 1)[0])
    if target.startswith("/"):
        return target[1:]
    return posixpath.normpath(posixpath.join(posixpath.dirname(source), target))


def _read_rels(data: bytes) -> List[Dict[str, str]]:
    return [dict(r.attrib) for r in ET.fromstring(data) if r.tag == "{%s}Relationship" % PKG_RELS]


def _rels_xml(rels: List[Dict[str, str]]) -> bytes:
    out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns="%s">' % PKG_RELS]
    for r in rels:
        out.append("<Relationship %s/>" % " ".join(
            '%s="%s"' % (k, escape(r[k], {'"': "&quot;"}))
            for k in ("Id", "Type", "Target", "TargetMode") if k in r))
    out.append("</Relationships>")
    return "".join(out).encode("utf-8")


def _types(parts: Parts):
    root = ET.fromstring(parts[CT_PART])
    defaults = {e.get("Extension").lower(): e.get("ContentType")
                for e in root if e.tag == "{%s}Default" % CONTENT_TYPES}
    overrides = {e.get("PartName").lstrip("/"): e.get("ContentType")
                 for e in root if e.tag == "{%s}Override" % CONTENT_TYPES}
    return defaults, overrides


def _types_xml(defaults: Dict[str, str], overrides: Dict[str, str]) -> bytes:
    q = lambda v: escape(v, {'"': "&quot;"})    # noqa: E731
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Types xmlns="%s">' % CONTENT_TYPES
            + "".join('<Default Extension="%s" ContentType="%s"/>' % (q(e), q(t)) for e, t in defaults.items())
            + "".join('<Override PartName="/%s" ContentType="%s"/>' % (q(p), q(t)) for p, t in overrides.items())
            + "</Types>").encode("utf-8")


def _type_of(name: str, defaults, overrides) -> Optional[str]:
    return overrides.get(name) or defaults.get(name.rpartition(".")[2].lower())


def link(parts: Parts, source: str, target: str, rel_type: str) -> str:
    """Relate ``target`` from ``source`` (e.g. 'word/document.xml') with ``rel_type``
    (a full URI, or a short name like 'footer'); reuse an existing equal relationship.
    Returns the relationship id."""
    rel_type = rel_type if "/" in rel_type else RT + rel_type
    rname = _rels_path(source)
    rels = _read_rels(parts[rname]) if rname in parts else []
    for r in rels:
        if r["Type"] == rel_type and r.get("TargetMode") != "External" and _resolve(source, r["Target"]) == target:
            return r["Id"]
    ids = {r["Id"] for r in rels}
    rid = next("rId%d" % i for i in range(1, len(ids) + 2) if "rId%d" % i not in ids)
    rels.append({"Id": rid, "Type": rel_type,
                 "Target": posixpath.relpath(target, posixpath.dirname(source) or ".")})
    parts[rname] = _rels_xml(rels)
    return rid


def adopt(dst: Parts, src: Parts, name: str, as_name: Optional[str] = None,
          link_from: Optional[str] = None) -> Optional[str]:
    """Bring part ``name`` of package ``src`` into package ``dst`` as ``as_name``
    (default: the same name), replacing a part already there.

    Its own relationships come along, and so do the parts they reach. A reached part
    is renamed if ``dst`` already holds a different part of that name. Every adopted
    part gets its content type from ``src``. With ``link_from`` (e.g.
    'word/document.xml'), the part is also related from that part, with the
    relationship type ``src`` uses for it, and the relationship id is returned."""
    target = as_name or name
    defaults, overrides = _types(dst)
    sdefaults, soverrides = _types(src)

    def copy(part: str, to: str) -> None:
        dst[to] = src[part]
        ctype = _type_of(part, sdefaults, soverrides)
        if ctype is None:
            raise DocxPackageError(part, ["%s has no content type in the source package" % part])
        ext = to.rpartition(".")[2].lower()
        if part in soverrides or defaults.get(ext, ctype) != ctype:
            overrides[to] = ctype
        else:
            defaults.setdefault(ext, ctype)
            overrides.pop(to, None)
        srels = _rels_path(part)
        if srels not in src:
            dst.pop(_rels_path(to), None)
            return
        out = []
        for r in _read_rels(src[srels]):
            r = dict(r)
            if r.get("TargetMode") != "External":
                child, want = _resolve(part, r["Target"]), _resolve(to, r["Target"])
                if dst.get(want) != src[child]:
                    stem, dot, ext2 = want.rpartition(".")
                    n = 1
                    while want in dst:
                        want, n = "%s-%d%s%s" % (stem, n, dot, ext2), n + 1
                    copy(child, want)
                r["Target"] = posixpath.relpath(want, posixpath.dirname(to) or ".")
            out.append(r)
        dst[_rels_path(to)] = _rels_xml(out)

    copy(name, target)
    dst[CT_PART] = _types_xml(defaults, overrides)
    if not link_from:
        return None
    rtype = next((r["Type"] for rn, b in src.items() if rn.endswith(".rels")
                  for r in _read_rels(b) if r.get("TargetMode") != "External"
                  and _resolve(_rels_source(rn), r["Target"]) == name), None)
    if rtype is None:
        raise DocxPackageError(name, ["nothing in the source package relates %s" % name])
    return link(dst, link_from, target, rtype)


# --------------------------------------------------------------------- the check

_WML = "application/vnd.openxmlformats-officedocument.wordprocessingml."
_TYPE_BY_REL = {
    RT + "styles": _WML + "styles+xml", RT + "settings": _WML + "settings+xml",
    RT + "webSettings": _WML + "webSettings+xml", RT + "fontTable": _WML + "fontTable+xml",
    RT + "numbering": _WML + "numbering+xml", RT + "footnotes": _WML + "footnotes+xml",
    RT + "endnotes": _WML + "endnotes+xml", RT + "header": _WML + "header+xml",
    RT + "footer": _WML + "footer+xml", RT + "comments": _WML + "comments+xml",
    RT + "theme": "application/vnd.openxmlformats-officedocument.theme+xml",
}


class DocxPackageError(ValueError):
    """A package defect: Word would reject (or prompt about) the file."""

    def __init__(self, path: str, problems: List[str]):
        self.problems = problems
        lines = ["%s%s" % (p, " (x%d)" % n if n > 1 else "") for p, n in Counter(problems).items()]
        more = "\n  ... and %d more" % (len(lines) - 25) if len(lines) > 25 else ""
        super().__init__("%s: not a package Word accepts:\n  %s%s" % (path, "\n  ".join(lines[:25]), more))


def _order_problems(name: str, data: bytes) -> List[str]:
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        return ["%s: not well-formed XML (%s)" % (name, e)]
    out = []
    for el in root.iter():
        spec = _spec(el.tag)
        if spec is None or not len(el):
            continue
        tags = [c.tag for c in el]
        ranks = _child_ranks(spec, tags)
        if ranks is None:
            continue
        where = "%s: <%s>" % (name, _show(el.tag))
        bad = next((i for i in range(len(ranks) - 1) if ranks[i] > ranks[i + 1]), None)
        if bad is not None:
            out.append("%s has <%s> before <%s>" % (where, _show(tags[bad]), _show(tags[bad + 1])))
        for key, n in Counter(_split(t) for t in tags).items():
            if n > 1 and key in spec[1]:
                out.append("%s has <%s> %d times" % (where, _show("{%s}%s" % key), n))
    return out


def _package_problems(parts: Parts) -> List[str]:
    out: List[str] = []
    rels: Dict[str, List[Dict[str, str]]] = {}
    for n, b in parts.items():
        if n.endswith(".rels"):
            try:
                rels[n] = _read_rels(b)
            except ET.ParseError:
                out.append("%s: not well-formed XML" % n)
    for n, lst in rels.items():
        for rid, c in Counter(r["Id"] for r in lst).items():
            if c > 1:
                out.append("%s: relationship id %s used %d times" % (n, rid, c))
        source = _rels_source(n)
        if source and source not in parts:
            continue
        for r in lst:
            if r.get("TargetMode") != "External" and _resolve(source, r["Target"]) not in parts:
                out.append("%s: %s points to missing %s" % (n, r["Id"], _resolve(source, r["Target"])))

    reached, incoming, todo = {ROOT_RELS}, {}, [""]
    while todo:
        source = todo.pop()
        for r in rels.get(_rels_path(source), []):
            target = _resolve(source, r["Target"])
            if r.get("TargetMode") != "External" and target in parts and target not in reached:
                reached.add(target)
                incoming[target] = r["Type"]
                todo.append(target)
                if _rels_path(target) in parts:
                    reached.add(_rels_path(target))
    out += ["unreferenced part %s (nothing relates it; Word rejects such leftovers)" % n
            for n in sorted(parts) if n not in reached and n != CT_PART and not n.endswith("/")]

    try:
        defaults, overrides = _types(parts)
    except ET.ParseError:
        return out + ["%s: not well-formed XML" % CT_PART]
    out += ["content type declared for missing part %s" % p for p in sorted(overrides) if p not in parts]
    for n in sorted(reached - {CT_PART}):
        have, want = _type_of(n, defaults, overrides), _TYPE_BY_REL.get(incoming.get(n, ""))
        if have is None:
            out.append("no content type for %s" % n)
        elif want and have != want:
            out.append("%s has content type %s, not %s" % (n, have, want))
        if not parts[n] and n.endswith((".xml", ".rels")):
            out.append("%s is empty" % n)

    main = next((t for t, kind in incoming.items() if kind == RT + "officeDocument"), None)
    if main is None:
        return out + ["no main document part"]
    linked = {kind: t for t, kind in incoming.items()}
    settings = linked.get(RT + "settings")
    running = {t for t, kind in incoming.items() if kind in (RT + "header", RT + "footer")}
    if settings and b"updateFields" in parts[settings]:
        s = ET.fromstring(parts[settings])
        on = s.find("{%s}updateFields" % W)
        if on is not None and on.get("{%s}val" % W, "true") in ("true", "1", "on") and not any(
                any(b"<%s" % k in parts[n] or b":%s" % k in parts[n]
                    for k in (b"fldSimple", b"instrText", b"fldChar"))
                for n in reached if n.endswith(".xml") and n not in running):
            out.append("%s: updateFields is on with no fields to update (Word prompts on every open)" % settings)
    for kind in ("footnote", "endnote"):
        notes = linked.get(RT + kind + "s")
        have_ids = set()
        if notes:
            have_ids = {e.get("{%s}id" % W) for e in ET.fromstring(parts[notes]).iter("{%s}%s" % (W, kind))}
        for n in sorted(reached):
            if n.endswith(".xml") and n != notes and (b"%sReference" % kind.encode()) in parts[n]:
                used = {e.get("{%s}id" % W) for e in ET.fromstring(parts[n]).iter("{%s}%sReference" % (W, kind))}
                out += ["%s: %s reference %s has no %s" % (n, kind, i, kind) for i in sorted(used - have_ids)]
        if settings and not notes:
            s = ET.fromstring(parts[settings])
            if s.find("{%s}%sPr/{%s}%s" % (W, kind, W, kind)) is not None:
                out.append("%s names %s separators but no %ss part is linked" % (settings, kind, kind))
    return out


def problems(parts: Parts, only: Optional[Iterable[str]] = None) -> List[str]:
    """Every defect of package ``parts``. ``only`` limits the per-part XML checks to
    those part names; package-level checks always run."""
    names = sorted(parts if only is None else set(only) & set(parts))
    out = []
    for n in names:
        if n.endswith(".xml") and W.encode() in parts[n]:
            out += _order_problems(n, parts[n])
    if CT_PART in parts and ROOT_RELS in parts:
        out += _package_problems(parts)
    return out


def _key(problem: str) -> str:
    # Which pair is reported first depends on the container's other children, so a
    # disordered container counts as one defect whatever pair shows it.
    return re.sub(r" has <[^>]+> before <[^>]+>$", " has children out of schema order", problem)


def introduced(parts: Parts, baseline: Optional[Parts] = None) -> List[str]:
    """The defects of ``parts`` that ``baseline`` (the package it was made from) lacks."""
    if baseline is None:
        return problems(parts)
    changed = [n for n in parts if baseline.get(n) != parts[n]]
    found = problems(parts, changed)
    excess = Counter(map(_key, found)) - Counter(map(_key, problems(baseline, changed)))
    out = []
    for p in found:
        if excess[_key(p)]:
            excess[_key(p)] -= 1
            out.append(p)
    return out


# ------------------------------------------------------------------------- files

def read(path: str):
    """``(entry order, ZipInfo by name, bytes by name)`` of a .docx."""
    if not os.path.exists(path):
        raise DocxPackageError(path, ["no such file"])
    if not zipfile.is_zipfile(path):
        raise DocxPackageError(path, ["not a zip package (a Word ~$ lock file?)"])
    with zipfile.ZipFile(path) as z:
        infos = z.infolist()
        names = [i.filename for i in infos]
        dup = sorted(n for n, c in Counter(names).items() if c > 1)
        if dup:
            raise DocxPackageError(path, ["duplicate zip entries: " + ", ".join(dup)])
        return names, {i.filename: i for i in infos}, {n: z.read(n) for n in names}


def _parts(baseline: Union[None, str, os.PathLike, Parts]) -> Optional[Parts]:
    if baseline is None or isinstance(baseline, dict):
        return baseline
    return read(os.fspath(baseline))[2]


def write(out_docx: str, parts: Parts, order: Optional[Iterable[str]] = None,
          infos: Optional[Dict[str, zipfile.ZipInfo]] = None,
          baseline: Union[None, str, os.PathLike, Parts] = None) -> List[str]:
    """Write ``parts`` to ``out_docx`` exactly as given, after checking them.

    ``baseline`` is the package this one was made from (parts or a path). Only defects
    it lacks count, so an edit isn't blamed for what it inherited; without it, every
    defect counts. Raises :class:`DocxPackageError` and writes nothing if this write
    introduced a defect. Entries keep ``order``, and those in ``infos`` keep their
    date_time / compress_type / attributes, so an untouched part is copied byte-for-byte.
    Returns the entry names written."""
    out_docx = os.fspath(out_docx)
    bad = introduced(parts, _parts(baseline))
    if bad:
        raise DocxPackageError(out_docx, bad)
    names = [n for n in (order if order is not None else parts) if n in parts]
    names += [n for n in parts if n not in set(names)]
    infos = infos or {}
    tmp = out_docx + ".partial"
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
            for n in names:
                zi = infos.get(n)
                if zi is None:
                    zout.writestr(n, parts[n])
                    continue
                ni = zipfile.ZipInfo(n, date_time=zi.date_time)
                ni.compress_type = zi.compress_type
                ni.external_attr = zi.external_attr
                ni.internal_attr = zi.internal_attr
                ni.create_system = zi.create_system
                zout.writestr(ni, parts[n])
        os.replace(tmp, out_docx)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return names


def repackage(src_docx: str, out_docx: str, replacements: Optional[Dict[str, object]] = None,
              additions: Optional[Parts] = None) -> List[str]:
    """Copy ``src_docx`` to ``out_docx`` byte-for-byte except for ``replacements``
    (name -> str or bytes) and new ``additions``, checked against the source."""
    names, infos, data = read(src_docx)
    source = dict(data)
    for name, value in (replacements or {}).items():
        data[name] = value.encode("utf-8") if isinstance(value, str) else value
    data.update(additions or {})
    return write(out_docx, data, names, infos, baseline=source)


def verify(path, baseline: Union[None, str, os.PathLike, Parts] = None) -> None:
    """Raise :class:`DocxPackageError` if the file at ``path`` (written by something
    else, e.g. python-docx) has defects that ``baseline`` lacks."""
    bad = introduced(read(os.fspath(path))[2], _parts(baseline))
    if bad:
        raise DocxPackageError(os.fspath(path), bad)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m rsarchive.docxpackage",
                                 description="List the defects Word rejects in .docx packages.")
    ap.add_argument("docx", nargs="+")
    args = ap.parse_args(argv)
    status = 0
    for path in args.docx:
        try:
            found = problems(read(path)[2])
        except DocxPackageError as e:
            found = e.problems
        print("%s: %s" % (path, "ok" if not found else "%d defect(s)" % len(found)))
        for p, n in Counter(found).items():
            print("  %s%s" % (p, " (x%d)" % n if n > 1 else ""))
        status |= bool(found)
    return status


if __name__ == "__main__":
    sys.exit(main())
