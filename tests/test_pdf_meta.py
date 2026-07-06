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
