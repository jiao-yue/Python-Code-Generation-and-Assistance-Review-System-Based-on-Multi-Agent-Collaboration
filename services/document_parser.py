"""把上传附件解析为对话文本与 RAG Markdown。

支持 DOCX、XLSX/XLS、PDF、常见图片以及文本/代码文件。PDF 文本层和
图片 OCR 都只在本模块中处理；调用方不需要了解任何第三方库的返回结构。

每个附件产生两种文本：
1. plain_text：保留正文、表格值或代码，用于当前生成/审核对话；
2. markdown：带一级文档标题和二级小节标题，供 RAG 切分与溯源。

OCR 使用进程内懒加载的 RapidOCR，不调用在线接口。所有格式都有大小、页数、
行数或字符上限；发生截断时会显式写入 warnings 和 truncated，不静默丢内容。
"""

from __future__ import annotations

import re
import threading
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime
from io import BytesIO
from pathlib import Path
from typing import Any

from config import settings


class UploadError(ValueError):
    """可安全返回给前端的附件校验或解析错误。"""


@dataclass(frozen=True)
class ParsedDocument:
    """一个附件的解析结果。"""

    plain_text: str
    markdown: str
    warnings: list[str] = field(default_factory=list)
    truncated: bool = False


# ---------------------------------------------------------------------------
# OCR：模型只在第一次遇到图片或扫描页时加载
# ---------------------------------------------------------------------------
class _LocalOCR:
    """RapidOCR 适配器，把不同版本的返回结构统一为文本行。"""

    def __init__(self) -> None:
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError as exc:
            raise UploadError("图片 OCR 依赖未安装，请安装 rapidocr_onnxruntime") from exc
        self._engine = RapidOCR()

    def extract(self, image_array: Any) -> str:
        """识别 RGB 图片数组，按识别顺序拼接文字行。"""
        result = self._engine(image_array)
        # rapidocr_onnxruntime 返回 (result, elapse)；新版 RapidOCR 也可能返回对象。
        payload = result[0] if isinstance(result, tuple) else result
        if payload is None:
            return ""
        if hasattr(payload, "txts"):
            return "\n".join(str(text).strip() for text in payload.txts if str(text).strip())

        lines: list[str] = []
        for item in payload:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                text = str(item[1]).strip()
                if text:
                    lines.append(text)
        return "\n".join(lines)


_ocr_instance: _LocalOCR | None = None
_ocr_lock = threading.Lock()


def _get_ocr() -> _LocalOCR:
    """返回进程级 OCR 单例，避免每张图片重复加载模型。"""
    global _ocr_instance
    if _ocr_instance is None:
        with _ocr_lock:
            if _ocr_instance is None:
                _ocr_instance = _LocalOCR()
    return _ocr_instance


def _ocr_image(data: bytes) -> str:
    """校验图片尺寸后执行本地 OCR。"""
    try:
        import numpy as np
        from PIL import Image, UnidentifiedImageError
    except ImportError as exc:
        raise UploadError("图片处理依赖未安装，请安装 Pillow 和 numpy") from exc

    try:
        with Image.open(BytesIO(data)) as image:
            if image.width * image.height > settings.UPLOAD_MAX_IMAGE_PIXELS:
                raise UploadError(
                    f"图片像素过大，最多支持 {settings.UPLOAD_MAX_IMAGE_PIXELS:,} 像素"
                )
            image.load()
            rgb = image.convert("RGB")
            image_array = np.asarray(rgb)
    except UploadError:
        raise
    except (UnidentifiedImageError, OSError) as exc:
        raise UploadError("图片文件损坏或格式不受支持") from exc

    return _get_ocr().extract(image_array).strip()


# ---------------------------------------------------------------------------
# 通用文本工具
# ---------------------------------------------------------------------------
def _clean_text(text: str) -> str:
    """统一换行并去掉会造成索引或前端异常的 NUL 字符。"""
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "").strip()


def _limited(text: str) -> tuple[str, bool]:
    """按配置限制提取文本长度，返回文本和是否发生截断。"""
    clean = _clean_text(text)
    limit = settings.UPLOAD_MAX_TEXT_CHARS
    if len(clean) <= limit:
        return clean, False
    return clean[:limit].rstrip() + "\n\n[内容过长，已截断]", True


def _markdown_escape(value: str) -> str:
    """转义 Markdown 表格单元格中的特殊字符。"""
    return value.replace("|", "\\|").replace("\n", "<br>")


def _markdown_table(rows: list[list[str]]) -> str:
    """把表格行转换为 Markdown；第一行作为表头，短行自动补齐。"""
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    normalized = [row + [""] * (width - len(row)) for row in rows]
    header = normalized[0] or [""]
    lines = [
        "| " + " | ".join(_markdown_escape(cell) for cell in header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    lines.extend(
        "| " + " | ".join(_markdown_escape(cell) for cell in row) + " |" for row in normalized[1:]
    )
    return "\n".join(lines)


def _document_result(
    title: str,
    plain_parts: list[str],
    markdown_parts: list[str],
    warnings: list[str] | None = None,
) -> ParsedDocument:
    """统一生成两种文本并应用字符上限。"""
    plain, plain_truncated = _limited("\n\n".join(part for part in plain_parts if part.strip()))
    markdown_body, markdown_truncated = _limited(
        "\n\n".join(part for part in markdown_parts if part.strip())
    )
    markdown = f"# {title}\n\n{markdown_body}".strip()
    truncated = plain_truncated or markdown_truncated
    result_warnings = list(warnings or [])
    if truncated:
        result_warnings.append(f"提取文本超过 {settings.UPLOAD_MAX_TEXT_CHARS:,} 字，已截断")
    if not plain.strip():
        raise UploadError("未从附件中提取到可用文字")
    return ParsedDocument(plain, markdown, result_warnings, truncated)


def _decode_text(data: bytes) -> str:
    """按 BOM、UTF-8、GB18030 顺序严格解码文本文件。"""
    if not data.strip():
        raise UploadError("文本文件为空")
    encodings: list[str] = []
    if data.startswith(b"\xef\xbb\xbf"):
        encodings.append("utf-8-sig")
    elif data.startswith((b"\xff\xfe", b"\xfe\xff")):
        encodings.append("utf-16")
    encodings.extend(["utf-8", "gb18030"])
    for encoding in encodings:
        try:
            return data.decode(encoding, errors="strict")
        except UnicodeDecodeError:
            continue
    raise UploadError("文本编码无法识别，请使用 UTF-8、UTF-16 或 GB18030")


def _check_zip_size(data: bytes) -> None:
    """防止 DOCX/XLSX 压缩包解压后占用过多内存。"""
    if not zipfile.is_zipfile(BytesIO(data)):
        raise UploadError("Office 文件损坏：不是有效的 DOCX/XLSX 压缩文档")
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            total = sum(item.file_size for item in archive.infolist())
    except zipfile.BadZipFile as exc:
        raise UploadError("Office 文件压缩结构损坏") from exc
    if total > settings.UPLOAD_MAX_UNCOMPRESSED_BYTES:
        raise UploadError("Office 文件解压后过大，请拆分后再上传")


# ---------------------------------------------------------------------------
# Word / Excel
# ---------------------------------------------------------------------------
def _parse_docx(filename: str, data: bytes) -> ParsedDocument:
    """按正文顺序提取 Word 段落和表格，并补充页眉页脚文字。"""
    _check_zip_size(data)
    try:
        from docx import Document
        from docx.table import Table
        from docx.text.paragraph import Paragraph
    except ImportError as exc:
        raise UploadError("Word 解析依赖未安装，请安装 python-docx") from exc

    try:
        document = Document(BytesIO(data))
    except Exception as exc:
        raise UploadError("DOCX 无法打开；加密或损坏的文档请先另存为普通 DOCX") from exc

    title = Path(filename).stem
    plain_parts: list[str] = []
    markdown_parts: list[str] = []
    for block in document.iter_inner_content():
        if isinstance(block, Paragraph):
            text = block.text.strip()
            if not text:
                continue
            plain_parts.append(text)
            style_name = block.style.name if block.style is not None else ""
            if style_name.lower().startswith("heading") or style_name.startswith("标题"):
                markdown_parts.append(f"## {text}")
            else:
                markdown_parts.append(text)
        elif isinstance(block, Table):
            rows = [[cell.text.strip() for cell in row.cells] for row in block.rows]
            if not rows:
                continue
            plain_parts.append("\n".join("\t".join(row) for row in rows))
            markdown_parts.append(_markdown_table(rows))

    # 页眉页脚可能包含文档编号、适用范围等知识信息，去重后补充提取。
    extra_texts: list[str] = []
    for section in document.sections:
        for container in (section.header, section.footer):
            for paragraph in container.paragraphs:
                text = paragraph.text.strip()
                if text and text not in extra_texts:
                    extra_texts.append(text)
    if extra_texts:
        plain_parts.append("页眉页脚：\n" + "\n".join(extra_texts))
        markdown_parts.append("## 页眉与页脚\n\n" + "\n".join(extra_texts))

    if not markdown_parts:
        markdown_parts = ["## 正文\n\n" + "\n\n".join(plain_parts)]
    return _document_result(title, plain_parts, markdown_parts)


def _excel_value(value: Any) -> str:
    """把 Excel 单元格值转换为不丢公式和日期的文本。"""
    if value is None:
        return ""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value).strip()


# 每个工作表条目都保留原始 Excel 行号，空行不会造成后续行号错位。
ExcelRows = list[tuple[int, list[str]]]


def _sheet_parts(sheet_name: str, rows: ExcelRows) -> tuple[str, str]:
    """生成一个工作表的对话文本和 RAG 小节。"""
    plain = f"工作表：{sheet_name}\n" + "\n".join(
        f"行 {row_number}：" + "\t".join(row) for row_number, row in rows
    )
    # 使用“行号 + 列值”形式，不把第一条数据误当表头，也支持无表头工作表。
    width = max((len(row) for _, row in rows), default=1)
    table_rows = [["行号", *(f"列{i}" for i in range(1, width + 1))]]
    table_rows.extend([str(row_number), *row] for row_number, row in rows)
    markdown = f"## 工作表：{sheet_name}\n\n{_markdown_table(table_rows)}"
    return plain, markdown


def _collect_excel_parts(
    sheets: list[tuple[str, ExcelRows]],
) -> tuple[list[str], list[str]]:
    """汇总工作表，并统一处理空表。"""
    plain_parts: list[str] = []
    markdown_parts: list[str] = []
    for sheet_name, rows in sheets:
        if not rows:
            continue
        plain, markdown = _sheet_parts(sheet_name, rows)
        plain_parts.append(plain)
        markdown_parts.append(markdown)
    if not plain_parts:
        raise UploadError("Excel 中没有非空单元格")
    return plain_parts, markdown_parts


def _parse_xlsx(filename: str, data: bytes) -> ParsedDocument:
    """以只读方式提取全部工作表，保留公式文本而不是臆造计算结果。"""
    _check_zip_size(data)
    try:
        from openpyxl import load_workbook
    except ImportError as exc:
        raise UploadError("Excel 解析依赖未安装，请安装 openpyxl") from exc

    try:
        workbook = load_workbook(BytesIO(data), read_only=True, data_only=False, keep_links=False)
    except Exception as exc:
        raise UploadError("XLSX 无法打开；加密或损坏的工作簿请先另存为普通 XLSX") from exc

    sheets: list[tuple[str, ExcelRows]] = []
    warnings: list[str] = []
    truncated = False
    try:
        for worksheet in workbook.worksheets:
            # 不盲信文件声明的 max_row/max_column，重新计算实际使用范围。
            worksheet.reset_dimensions()
            rows: ExcelRows = []
            for row_index, row in enumerate(worksheet.iter_rows(values_only=True), start=1):
                values = [_excel_value(value) for value in row]
                while values and not values[-1]:
                    values.pop()
                if not values or not any(values):
                    continue
                if len(rows) >= settings.UPLOAD_EXCEL_MAX_ROWS:
                    truncated = True
                    warnings.append(
                        f"工作表 {worksheet.title} 超过 "
                        f"{settings.UPLOAD_EXCEL_MAX_ROWS:,} 行，已截断"
                    )
                    break
                rows.append((row_index, values))
            sheets.append((worksheet.title, rows))
    finally:
        workbook.close()

    plain_parts, markdown_parts = _collect_excel_parts(sheets)
    result = _document_result(Path(filename).stem, plain_parts, markdown_parts, warnings)
    if not truncated:
        return result
    return ParsedDocument(result.plain_text, result.markdown, result.warnings, True)


def _parse_xls(filename: str, data: bytes) -> ParsedDocument:
    """提取旧版 XLS 工作簿；日期单元格按工作簿日期系统转换。"""
    if not data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        raise UploadError("XLS 文件头无效，文件可能损坏或扩展名不正确")
    try:
        import xlrd
    except ImportError as exc:
        raise UploadError("旧版 Excel 解析依赖未安装，请安装 xlrd") from exc

    try:
        workbook = xlrd.open_workbook(file_contents=data, on_demand=True)
    except Exception as exc:
        raise UploadError("XLS 无法打开；加密或损坏的工作簿请先另存为 XLSX") from exc

    sheets: list[tuple[str, ExcelRows]] = []
    warnings: list[str] = []
    truncated = False
    try:
        for sheet_index in range(workbook.nsheets):
            sheet = workbook.sheet_by_index(sheet_index)
            rows: ExcelRows = []
            for row_index in range(sheet.nrows):
                if len(rows) >= settings.UPLOAD_EXCEL_MAX_ROWS:
                    truncated = True
                    warnings.append(
                        f"工作表 {sheet.name} 超过 {settings.UPLOAD_EXCEL_MAX_ROWS:,} 行，已截断"
                    )
                    break
                values: list[str] = []
                for column_index in range(sheet.ncols):
                    cell = sheet.cell(row_index, column_index)
                    value = cell.value
                    if cell.ctype == xlrd.XL_CELL_DATE:
                        value = xlrd.xldate.xldate_as_datetime(value, workbook.datemode)
                    values.append(_excel_value(value))
                while values and not values[-1]:
                    values.pop()
                if values and any(values):
                    rows.append((row_index + 1, values))
            sheets.append((sheet.name, rows))
            workbook.unload_sheet(sheet_index)
    finally:
        workbook.release_resources()

    plain_parts, markdown_parts = _collect_excel_parts(sheets)
    result = _document_result(Path(filename).stem, plain_parts, markdown_parts, warnings)
    if not truncated:
        return result
    return ParsedDocument(result.plain_text, result.markdown, result.warnings, True)


# ---------------------------------------------------------------------------
# PDF / 图片 / 文本
# ---------------------------------------------------------------------------
def _parse_pdf(filename: str, data: bytes) -> ParsedDocument:
    """优先读取 PDF 文本层；没有有效文本层的页面再执行 OCR。"""
    try:
        import pymupdf
    except ImportError as exc:
        raise UploadError("PDF 解析依赖未安装，请安装 pymupdf") from exc

    try:
        document = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:
        raise UploadError("PDF 无法打开，文件可能损坏或扩展名不正确") from exc

    title = Path(filename).stem
    plain_parts: list[str] = []
    markdown_parts: list[str] = []
    warnings: list[str] = []
    try:
        if document.needs_pass:
            raise UploadError("暂不支持密码加密的 PDF，请先解除密码保护")
        if document.page_count > settings.UPLOAD_PDF_MAX_PAGES:
            raise UploadError(f"PDF 页数过多，最多支持 {settings.UPLOAD_PDF_MAX_PAGES} 页")

        for page_index, page in enumerate(document):
            page_no = page_index + 1
            text = page.get_text("text", sort=True).strip()
            if len(re.sub(r"\s+", "", text)) < 8:
                try:
                    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False)
                    ocr_text = _ocr_image(pixmap.tobytes("png"))
                    if ocr_text:
                        text = ocr_text
                        warnings.append(f"第 {page_no} 页使用本地 OCR 识别")
                except UploadError as exc:
                    warnings.append(f"第 {page_no} 页 OCR 未完成：{exc}")
                except Exception as exc:
                    # 单页模型/渲染异常不能抹掉其他页面已经成功提取的内容。
                    warnings.append(f"第 {page_no} 页 OCR 异常：{type(exc).__name__}")
            if not text.strip():
                warnings.append(f"第 {page_no} 页未提取到文字")
                continue
            plain_parts.append(f"[第 {page_no} 页]\n{text}")
            markdown_parts.append(f"## 第 {page_no} 页\n\n{text}")
    finally:
        document.close()

    return _document_result(title, plain_parts, markdown_parts, warnings)


def _parse_image(filename: str, data: bytes) -> ParsedDocument:
    """把图片中的文字 OCR 为对话文本和一个 RAG 小节。"""
    text = _ocr_image(data)
    if not text:
        raise UploadError("图片中未识别到文字；当前图片功能用于 OCR，不进行图像内容理解")
    title = Path(filename).stem
    return _document_result(title, [text], [f"## 图片文字\n\n{text}"], ["使用本地 OCR 识别"])


def _parse_text(filename: str, data: bytes) -> ParsedDocument:
    """读取文本、代码或 Markdown，不改动原始代码内容。"""
    text = _clean_text(_decode_text(data))
    title = Path(filename).stem
    if Path(filename).suffix.lower() == ".md":
        markdown = text if text.startswith("# ") else f"# {title}\n\n{text}"
        plain, plain_truncated = _limited(text)
        limited_markdown, markdown_truncated = _limited(markdown)
        warnings = ["文本过长，已截断"] if plain_truncated or markdown_truncated else []
        return ParsedDocument(
            plain,
            limited_markdown,
            warnings,
            plain_truncated or markdown_truncated,
        )
    return _document_result(title, [text], [f"## 内容\n\n{text}"])


# ---------------------------------------------------------------------------
# 对外统一入口
# ---------------------------------------------------------------------------
def parse_document(filename: str, data: bytes) -> ParsedDocument:
    """按扩展名解析附件，并把第三方异常收敛为可读的上传错误。"""
    extension = Path(filename).suffix.lower()
    parsers = {
        ".docx": _parse_docx,
        ".xlsx": _parse_xlsx,
        ".xls": _parse_xls,
        ".pdf": _parse_pdf,
    }
    if extension in {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"}:
        return _parse_image(filename, data)
    if extension in {".txt", ".py", ".md", ".csv", ".json", ".log"}:
        return _parse_text(filename, data)
    parser = parsers.get(extension)
    if parser is None:
        raise UploadError(f"不支持的文件类型：{extension or '无扩展名'}")
    try:
        return parser(filename, data)
    except UploadError:
        raise
    except Exception as exc:
        raise UploadError(f"附件解析失败：{type(exc).__name__}: {exc}") from exc
