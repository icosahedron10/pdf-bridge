"""Rebuild the original PDF Bridge evaluation corpus.

This utility is intentionally outside the test dependency graph. Rebuilding the
checked-in binary fixtures requires exactly ReportLab 4.4.3; normal test runs
consume the PDFs and do not import ReportLab.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

try:
    import reportlab
    from reportlab.lib.colors import HexColor
    from reportlab.lib.pagesizes import LETTER, landscape
    from reportlab.lib.pdfencrypt import StandardEncryption
    from reportlab.pdfgen import canvas
except ImportError as exc:  # pragma: no cover - operator-only rebuild guard
    raise SystemExit(
        "Install the pinned corpus builder with: python -m pip install reportlab==4.4.3"
    ) from exc


ROOT = Path(__file__).resolve().parent
REPORTLAB_VERSION = "4.4.3"
CREATOR = f"PDF Bridge deterministic corpus builder (ReportLab {REPORTLAB_VERSION})"
BODY_STYLE = ("Helvetica", 11)


def _require_pinned_reportlab() -> None:
    if reportlab.Version != REPORTLAB_VERSION:
        raise SystemExit(
            f"ReportLab {REPORTLAB_VERSION} is required; found {reportlab.Version}"
        )


def _new_canvas(
    filename: str,
    *,
    pagesize: tuple[float, float] = LETTER,
    encrypt: StandardEncryption | None = None,
) -> canvas.Canvas:
    document = canvas.Canvas(
        str(ROOT / filename),
        pagesize=pagesize,
        pageCompression=0,
        invariant=1,
        encrypt=encrypt,
    )
    document.setAuthor("PDF Bridge contributors")
    document.setCreator(CREATOR)
    document.setSubject("Original offline acceptance fixture; CC0-1.0")
    document.setTitle(filename.removesuffix(".pdf").replace("-", " ").title())
    return document


def _draw_text_page(
    document: canvas.Canvas,
    *,
    lines: Iterable[tuple[str, str, int]],
    top: float,
) -> None:
    y = top
    for text, font, size in lines:
        document.setFont(font, size)
        document.drawString(54, y, text)
        y -= 28 if size >= 18 else 21 if size >= 13 else 17
    document.showPage()


def _build_operations_guide() -> None:
    _, height = LETTER
    document = _new_canvas("operations-guide.pdf")
    _draw_text_page(
        document,
        top=height - 60,
        lines=(
            ("Operations Guide", "Helvetica-Bold", 20),
            ("Installation", "Helvetica-Bold", 14),
            ("Follow this ordered checklist before opening the service:", *BODY_STYLE),
            ("1. Confirm the package checksum matches the approved release record.", *BODY_STYLE),
            (
                "2. Install the package in the designated private application network.",
                *BODY_STYLE,
            ),
            (
                "3. Record the immutable revision and the operator identity in the log.",
                *BODY_STYLE,
            ),
            ("The checklist preserves a clear audit trail for every installation.", *BODY_STYLE),
        ),
    )
    _draw_text_page(
        document,
        top=height - 60,
        lines=(
            ("Verification", "Helvetica-Bold", 14),
            ("Complete these checks before publishing prepared content:", *BODY_STYLE),
            (
                "- Run the readiness probe and confirm every required provider is ready.",
                *BODY_STYLE,
            ),
            ("- Verify the document page count and the canonical Markdown hash.", *BODY_STYLE),
            (
                "- Compare the expected point count with the active vector collection.",
                *BODY_STYLE,
            ),
            ("Stop publication immediately if any recorded value differs.", *BODY_STYLE),
        ),
    )
    document.save()


TABLE_HEADER = "Region      Item             SKU       Owner        Window       Status"
TABLE_ROWS_PAGE_1 = (
    "North       Field Kit        FK-101    Rowan       08:00-10:00  Ready",
    "South       Safety Manual    SM-204    Morgan      10:00-12:00  Review",
    "East        Sensor Pack      SP-310    Taylor      12:00-14:00  Ready",
    "West        Cable Bundle     CB-415    Jordan      14:00-16:00  Hold",
)
TABLE_ROWS_PAGE_2 = (
    "Central     Valve Set        VS-520    Casey       08:30-09:30  Ready",
    "Coastal     Pump Guide       PG-625    Avery       09:30-11:30  Review",
    "Mountain    Filter Case      FC-730    Quinn       11:30-13:30  Ready",
    "Prairie     Meter Card       MC-845    Riley       13:30-15:30  Hold",
)


def _draw_table_page(
    document: canvas.Canvas,
    *,
    title: str,
    rows: Iterable[str],
    height: float,
) -> None:
    document.setFont("Helvetica-Bold", 18)
    document.drawString(42, height - 48, title)
    document.setFont("Courier-Bold", 10)
    document.drawString(42, height - 90, TABLE_HEADER)
    y = height - 112
    document.setFont("Courier", 10)
    for row in rows:
        document.drawString(42, y, row)
        y -= 22
    document.setFont("Helvetica", 10)
    document.drawString(
        42,
        y - 10,
        "Each row is authoritative; the repeated header preserves table meaning across pages.",
    )
    document.showPage()


def _build_inventory_tables() -> None:
    pagesize = landscape(LETTER)
    _, height = pagesize
    document = _new_canvas("inventory-tables.pdf", pagesize=pagesize)
    _draw_table_page(
        document,
        title="Regional Inventory Table",
        rows=TABLE_ROWS_PAGE_1,
        height=height,
    )
    _draw_table_page(
        document,
        title="Regional Inventory Table (continued)",
        rows=TABLE_ROWS_PAGE_2,
        height=height,
    )
    document.save()


def _build_boundary_procedure() -> None:
    _, height = LETTER
    document = _new_canvas("page-boundary-procedure.pdf")
    _draw_text_page(
        document,
        top=height - 60,
        lines=(
            ("Recovery Procedure", "Helvetica-Bold", 20),
            ("Checkpoint", "Helvetica-Bold", 14),
            ("After the first durable checkpoint, keep the document lease active.", *BODY_STYLE),
            (
                "If the warning indicator remains visible at the bottom of this page,",
                *BODY_STYLE,
            ),
            ("continue with the next numbered page before changing any state.", *BODY_STYLE),
            ("This boundary sentence must remain associated with page one.", *BODY_STYLE),
        ),
    )
    _draw_text_page(
        document,
        top=height - 60,
        lines=(
            ("Resume", "Helvetica-Bold", 14),
            (
                "On page two, confirm that the lease owner and checkpoint still match.",
                *BODY_STYLE,
            ),
            (
                "Resume only the persisted phase and never repeat a completed side effect.",
                *BODY_STYLE,
            ),
            ("Record the final state after every expected artifact is present.", *BODY_STYLE),
            ("This boundary sentence must remain associated with page two.", *BODY_STYLE),
        ),
    )
    document.save()


def _build_handbook(filename: str, *, revision: str, effective_date: str, final_line: str) -> None:
    _, height = LETTER
    document = _new_canvas(filename)
    _draw_text_page(
        document,
        top=height - 60,
        lines=(
            ("Employee Onboarding Handbook", "Helvetica-Bold", 20),
            (f"Revision {revision}", "Helvetica-Bold", 14),
            (f"Effective date: {effective_date}", *BODY_STYLE),
            ("New employees review safety, privacy, and access requirements.", *BODY_STYLE),
            ("Managers verify required training before granting production access.", *BODY_STYLE),
            ("Every access decision is documented in the approved system of record.", *BODY_STYLE),
            (final_line, *BODY_STYLE),
        ),
    )
    document.save()


def _build_image_only() -> None:
    width, height = LETTER
    document = _new_canvas("image-only-diagram.pdf")
    document.setFillColor(HexColor("#E4E7EC"))
    document.roundRect(100, 250, width - 200, 260, 12, stroke=0, fill=1)
    document.setStrokeColor(HexColor("#344054"))
    document.setLineWidth(5)
    document.line(150, 310, width - 150, 450)
    document.circle(width / 2, 380, 70, stroke=1, fill=0)
    document.setFillColor(HexColor("#101828"))
    document.showPage()
    document.save()


def _build_encrypted() -> None:
    encryption = StandardEncryption(
        "fixture-password",
        ownerPassword="fixture-owner-password",
        canPrint=0,
        canModify=0,
        canCopy=0,
        canAnnotate=0,
        strength=40,
    )
    _, height = LETTER
    document = _new_canvas("encrypted-notice.pdf", encrypt=encryption)
    _draw_text_page(
        document,
        top=height - 60,
        lines=(
            ("Encrypted Evaluation Notice", "Helvetica-Bold", 20),
            ("This content must never reach the extraction stage without a password.", *BODY_STYLE),
        ),
    )
    document.save()


def _build_malformed() -> None:
    (ROOT / "malformed-truncated.pdf").write_bytes(
        b"%PDF-1.7\n% Original CC0 PDF Bridge malformed fixture\n"
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
        b"2 0 obj\n<< /Type /Pages /Count 1 /Kids [3 0 R] >>\n"
    )


def main() -> None:
    _require_pinned_reportlab()
    _build_operations_guide()
    _build_inventory_tables()
    _build_boundary_procedure()
    _build_handbook(
        "employee-onboarding-handbook-v1.pdf",
        revision="1",
        effective_date="2026-01-15",
        final_line="Revision one requires manager approval for the first access request.",
    )
    _build_handbook(
        "employee-onboarding-handbook-v2.pdf",
        revision="2",
        effective_date="2026-06-30",
        final_line="Revision two also requires security approval for privileged access.",
    )
    _build_image_only()
    _build_encrypted()
    _build_malformed()


if __name__ == "__main__":
    main()
