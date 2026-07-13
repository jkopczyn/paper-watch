"""PDF page-1 metadata extraction. A tiny valid PDF is built in-process so the
test needs no binary fixture and no network."""

from paper_watch.sources.pdf_meta import (
    PdfMetaResolver,
    parse_first_page_text,
    pdf_first_page_pdf,
    pdf_first_page_text,
)


def _make_pdf(lines: list[str]) -> bytes:
    ops = ["BT", "/F1 12 Tf", "72 720 Td"]
    for i, line in enumerate(lines):
        esc = line.replace("(", r"\(").replace(")", r"\)")
        if i:
            ops.append("0 -16 Td")
        ops.append(f"({esc}) Tj")
    ops.append("ET")
    content = " ".join(ops).encode()

    objs = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R"
        b"/Resources<</Font<</F1 5 0 R>>>>>>",
        b"<</Length %d>>stream\n" % len(content) + content + b"\nendstream",
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj" % i + o + b"endobj\n"
    xref_pos = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer<</Size %d/Root 1 0 R>>\nstartxref\n%d\n%%%%EOF" % (len(objs) + 1, xref_pos)
    return out


PAPER = _make_pdf(["Scalable Oversight of Language Models", "Abstract We study oversight at scale."])


def test_first_page_text_and_parse():
    text = pdf_first_page_text(PAPER)
    assert "Scalable Oversight" in text
    parsed = parse_first_page_text(text)
    assert parsed["title"].startswith("Scalable Oversight")
    assert "oversight at scale" in parsed["abstract"].lower()


def test_first_page_pdf_is_single_page():
    from io import BytesIO

    from pypdf import PdfReader

    one = pdf_first_page_pdf(_make_pdf(["Title Here", "Abstract body."]))
    assert len(PdfReader(BytesIO(one)).pages) == 1


def test_resolver_returns_metadata_from_text():
    r = PdfMetaResolver(fetch=lambda _u: PAPER)
    meta = r.resolve("https://x/paper.pdf")
    assert meta["title"].startswith("Scalable Oversight")
    assert meta["abstract"]


def test_resolver_no_ocr_on_empty_text_returns_none():
    empty = _make_pdf([])  # no text on the page
    calls = []
    r = PdfMetaResolver(fetch=lambda _u: empty, ocr=None)
    assert r.resolve("https://x/scan.pdf") is None
    assert not calls  # nothing to call


def test_resolver_ocr_fallback_invoked_on_empty_text():
    empty = _make_pdf([])
    seen = {}

    def fake_ocr(page_pdf: bytes):
        seen["called"] = True
        return {"title": "OCR Title", "abstract": "ocr abstract"}

    r = PdfMetaResolver(fetch=lambda _u: empty, ocr=fake_ocr)
    meta = r.resolve("https://x/scan.pdf")
    assert seen.get("called")
    assert meta["title"] == "OCR Title"


def test_resolver_fetch_error_is_none():
    def boom(_u):
        raise RuntimeError("down")

    assert PdfMetaResolver(fetch=boom).resolve("https://x/p.pdf") is None


# -- title selection on real page-1 layouts ---------------------------------
# Verbatim page-1 extractions from the PDFs that put junk titles in the digest.

SPRINGER = """Vol.:(0123456789)
Minds and Machines (2020) 30:411-437
https://doi.org/10.1007/s11023-020-09539-2
1 3
GENERAL ARTICLE
Artificial Intelligence, Values, and Alignment
Iason Gabriel1
Received: 22 February 2020 / Accepted: 26 August 2020
(c) The Author(s) 2020
Abstract
This paper looks at philosophical questions that arise in the context of AI design.
"""

COGNITION = """Cognition. 40 (1991) l-19
Newborns' preferential tracking of face-like
stimuli and its subsequent decline*
Mark H. Johnson
MRC Cognitive Development Unit, 17 Cordott Street. London. WClH OAH. U.K.
Received August 2, 1989. final revision accepted October 22. 1990
Abstract
Johnson. M.H., Dziurawiec. S., Ellis, H., and Morton. J.. 1990.
"""

REFERENCES_ONLY = """48. H.S.Mayberg etal.,Ann.Neurol. 28,57(1990).
49. R. M. Cohenet al.,Neuropsychopharmacology 2,
241(1989).
50. J.E.LeDoux, Sci.Am. 6,50(June1994);M.Davis,
Annu.Rev.Neurosci. 15,353(1992).
"""


def test_springer_running_head_is_not_taken_as_the_title():
    # line 0 is Springer's running head, then the journal ref, the DOI and a
    # section marker. The title is the sixth line down.
    parsed = parse_first_page_text(SPRINGER)
    assert parsed["title"] == "Artificial Intelligence, Values, and Alignment"


def test_journal_header_is_not_taken_as_the_title():
    # line 0 is the journal/volume header; the title runs across the next two.
    parsed = parse_first_page_text(COGNITION)
    assert parsed["title"] == (
        "Newborns' preferential tracking of face-like stimuli and its subsequent decline*"
    )


def test_a_page_of_references_yields_no_title():
    # This PDF's extractable page 1 is a reference column -- there is no title on
    # it. Returning None lets the caller fall back to OCR or leave the entry be;
    # returning the first reference line is how "48. H.S.Mayberg..." became a
    # paper in the digest.
    assert parse_first_page_text(REFERENCES_ONLY) is None


def test_a_plain_title_on_line_one_still_wins():
    parsed = parse_first_page_text(
        "Scalable Oversight of Language Models\nJane Roe\nAbstract\nWe study it.\n"
    )
    assert parsed["title"] == "Scalable Oversight of Language Models"
