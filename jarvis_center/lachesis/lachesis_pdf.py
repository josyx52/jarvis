"""
Lachesis — gerador de PDF minimalista, sem dependências externas.

Substitui o fpdf2: sob a conta de serviço gMSA usada pelo `jarvis_center`,
pacotes pip instalados em `C:\\Python313\\Lib\\site-packages` (fpdf, Pillow,
fonttools) ficam com ACLs corrompidas e não importáveis. Este módulo escreve
directamente a estrutura de um PDF 1.4 (objectos + xref + trailer), usando só
as fontes standard Helvetica / Helvetica-Bold (Core 14, não embutidas).
"""

PAGE_W = 595.28
PAGE_H = 841.89
MARGIN = 40.0
_CONTENT_W = PAGE_W - 2 * MARGIN

# Larguras de carateres Helvetica / Helvetica-Bold (1/1000 em), WinAnsiEncoding 32-126
_HELVETICA_WIDTHS = {
    32: 278, 33: 278, 34: 355, 35: 556, 36: 556, 37: 889, 38: 667, 39: 191,
    40: 333, 41: 333, 42: 389, 43: 584, 44: 278, 45: 333, 46: 278, 47: 278,
    48: 556, 49: 556, 50: 556, 51: 556, 52: 556, 53: 556, 54: 556, 55: 556,
    56: 556, 57: 556, 58: 278, 59: 278, 60: 584, 61: 584, 62: 584, 63: 556,
    64: 1015, 65: 667, 66: 667, 67: 722, 68: 722, 69: 667, 70: 611, 71: 778,
    72: 722, 73: 278, 74: 500, 75: 667, 76: 556, 77: 833, 78: 722, 79: 778,
    80: 667, 81: 778, 82: 722, 83: 667, 84: 611, 85: 722, 86: 667, 87: 944,
    88: 667, 89: 667, 90: 611, 91: 278, 92: 278, 93: 278, 94: 469, 95: 556,
    96: 333, 97: 556, 98: 556, 99: 500, 100: 556, 101: 556, 102: 278,
    103: 556, 104: 556, 105: 222, 106: 222, 107: 500, 108: 222, 109: 833,
    110: 556, 111: 556, 112: 556, 113: 556, 114: 333, 115: 500, 116: 278,
    117: 556, 118: 500, 119: 722, 120: 500, 121: 500, 122: 500, 123: 334,
    124: 260, 125: 334, 126: 584,
}

_HELVETICA_BOLD_WIDTHS = {
    32: 278, 33: 333, 34: 474, 35: 556, 36: 556, 37: 889, 38: 722, 39: 238,
    40: 333, 41: 333, 42: 389, 43: 584, 44: 278, 45: 333, 46: 278, 47: 278,
    48: 556, 49: 556, 50: 556, 51: 556, 52: 556, 53: 556, 54: 556, 55: 556,
    56: 556, 57: 556, 58: 333, 59: 333, 60: 584, 61: 584, 62: 584, 63: 611,
    64: 975, 65: 722, 66: 722, 67: 722, 68: 722, 69: 667, 70: 667, 71: 778,
    72: 778, 73: 278, 74: 556, 75: 722, 76: 611, 77: 833, 78: 722, 79: 778,
    80: 667, 81: 778, 82: 722, 83: 667, 84: 611, 85: 722, 86: 667, 87: 944,
    88: 667, 89: 667, 90: 611, 91: 333, 92: 278, 93: 333, 94: 584, 95: 556,
    96: 333, 97: 556, 98: 611, 99: 556, 100: 611, 101: 556, 102: 333,
    103: 611, 104: 611, 105: 278, 106: 278, 107: 556, 108: 278, 109: 889,
    110: 611, 111: 611, 112: 611, 113: 611, 114: 389, 115: 556, 116: 333,
    117: 611, 118: 556, 119: 778, 120: 556, 121: 556, 122: 500, 123: 389,
    124: 280, 125: 389, 126: 584,
}

_DEFAULT_WIDTH = 556
_DEFAULT_WIDTH_BOLD = 611


def _encode(text: str) -> bytes:
    """Codifica para WinAnsiEncoding (~cp1252), substituindo carateres não suportados."""
    return (text or "").encode("cp1252", errors="replace")


def _escape(text: str) -> bytes:
    raw = _encode(text)
    return raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def _str_width(text: str, bold: bool, size: float) -> float:
    table = _HELVETICA_BOLD_WIDTHS if bold else _HELVETICA_WIDTHS
    default = _DEFAULT_WIDTH_BOLD if bold else _DEFAULT_WIDTH
    total = sum(table.get(b, default) for b in _encode(text))
    return total * size / 1000.0


def _wrap_text(text: str, bold: bool, size: float, max_width: float) -> list[str]:
    lines: list[str] = []
    for paragraph in (text or "").split("\n"):
        if not paragraph.strip():
            lines.append("")
            continue
        words = paragraph.split(" ")
        current = ""
        for word in words:
            candidate = f"{current} {word}" if current else word
            if _str_width(candidate, bold, size) <= max_width or not current:
                current = candidate
            else:
                lines.append(current)
                current = word
        lines.append(current)
    return lines


class _Canvas:
    """Acumula operadores de conteúdo PDF, paginando automaticamente."""

    def __init__(self):
        self._pages: list[bytes] = []
        self._ops: list[str] = []
        self.y = PAGE_H - MARGIN
        self.font = "F1"
        self.bold = False
        self.size = 10.0

    def set_font(self, bold: bool, size: float) -> None:
        self.bold = bold
        self.font = "F2" if bold else "F1"
        self.size = size

    def _new_page(self) -> None:
        self._pages.append("\n".join(self._ops).encode("latin-1"))
        self._ops = []
        self.y = PAGE_H - MARGIN

    def gap(self, height: float) -> None:
        if self.y - height < MARGIN:
            self._new_page()
        else:
            self.y -= height

    def text_line(self, text: str, indent: float = 0.0) -> None:
        line_height = self.size * 1.25
        if self.y - line_height < MARGIN:
            self._new_page()
        self.y -= line_height
        x = MARGIN + indent
        escaped = _escape(text).decode("latin-1")
        self._ops.append(f"BT /{self.font} {self.size:.1f} Tf {x:.2f} {self.y:.2f} Td ({escaped}) Tj ET")

    def wrapped(self, text: str, indent: float = 0.0) -> None:
        for line in _wrap_text(text, self.bold, self.size, _CONTENT_W - indent):
            self.text_line(line, indent=indent)

    def finish(self) -> list[bytes]:
        self._new_page()
        return [p for p in self._pages if p.strip()] or [b""]


def _build_pdf(pages: list[bytes]) -> bytes:
    objs: list[bytes | None] = [None]

    def reserve() -> int:
        objs.append(None)
        return len(objs) - 1

    catalog_num = reserve()
    pages_num = reserve()
    font_reg_num = reserve()
    font_bold_num = reserve()

    objs[font_reg_num] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"
    objs[font_bold_num] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>"

    page_nums = []
    for content in pages:
        content_num = reserve()
        objs[content_num] = b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream"

        page_num = reserve()
        objs[page_num] = (
            b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 595.28 841.89] "
            b"/Resources << /Font << /F1 %d 0 R /F2 %d 0 R >> >> /Contents %d 0 R >>"
        ) % (pages_num, font_reg_num, font_bold_num, content_num)
        page_nums.append(page_num)

    kids = b" ".join(b"%d 0 R" % n for n in page_nums)
    objs[pages_num] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, len(page_nums))
    objs[catalog_num] = b"<< /Type /Catalog /Pages %d 0 R >>" % pages_num

    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i in range(1, len(objs)):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i
        out += objs[i]
        out += b"\nendobj\n"

    xref_offset = len(out)
    out += b"xref\n0 %d\n" % len(objs)
    out += b"0000000000 65535 f \n"
    for off in offsets[1:]:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF" % (len(objs), catalog_num, xref_offset)
    return bytes(out)


def pdf_document(title: str, meta_lines: list[str], body: str, table: list[list[str]] | None = None) -> bytes:
    """Gera um PDF simples: título, linhas de metadados, tabela opcional e corpo de texto."""
    c = _Canvas()

    c.set_font(bold=True, size=16)
    c.wrapped(title)
    c.gap(6)

    c.set_font(bold=False, size=10)
    for meta in meta_lines:
        c.wrapped(meta)
    c.gap(10)

    if table:
        _header, *rows = table
        for row in rows:
            check_id, check_title, severity, passed, note = (list(row) + [""] * 5)[:5]
            c.set_font(bold=True, size=10)
            c.wrapped(f"{check_id} — {check_title}")
            c.set_font(bold=False, size=9)
            c.wrapped(f"Severidade: {severity}  |  Passou: {passed}", indent=10)
            if note:
                c.wrapped(f"Nota: {note}", indent=10)
            c.gap(4)
        c.gap(6)

    if body:
        c.set_font(bold=True, size=12)
        c.wrapped("Resultado" if not table else "Resumo")
        c.gap(2)
        c.set_font(bold=False, size=10)
        c.wrapped(body)

    return _build_pdf(c.finish())