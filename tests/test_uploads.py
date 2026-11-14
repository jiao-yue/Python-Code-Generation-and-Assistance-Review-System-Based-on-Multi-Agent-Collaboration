"""附件解析、MD5 去重、知识库隔离与 multipart 接口测试。

OCR 单元测试使用可重复的文字识别桩，避免模型下载/初始化拖慢回归套件；
真实 RapidOCR 识别由独立端到端验证覆盖。
"""

from __future__ import annotations

from io import BytesIO

import pytest
from fastapi.testclient import TestClient

from config import settings
from services import document_parser
from services.document_parser import UploadError, parse_document
from services.upload_service import UploadService


@pytest.fixture
def upload_store(tmp_path, monkeypatch) -> UploadService:
    """每个测试使用独立上传目录，绝不读写真实 manifest 或 Chroma。"""
    root = tmp_path / "uploads"
    monkeypatch.setattr(settings, "UPLOAD_DIR", root)
    monkeypatch.setattr(settings, "UPLOAD_MANIFEST_PATH", root / "manifest.json")
    return UploadService()


def test_docx_extracts_paragraph_table_header_and_footer() -> None:
    """DOCX 正文顺序、表格和页眉页脚都应进入提取结果。"""
    from docx import Document

    document = Document()
    document.add_heading("上传需求", level=1)
    document.add_paragraph("实现一个安全的加法函数。")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "输入"
    table.cell(0, 1).text = "输出"
    table.cell(1, 0).text = "1, 2"
    table.cell(1, 1).text = "3"
    document.sections[0].header.paragraphs[0].text = "毕业设计"
    buffer = BytesIO()
    document.save(buffer)

    result = parse_document("requirement.docx", buffer.getvalue())
    assert "安全的加法函数" in result.plain_text
    assert "1, 2" in result.plain_text
    assert "毕业设计" in result.plain_text
    assert "## 上传需求" in result.markdown
    assert "| 输入 | 输出 |" in result.markdown


def test_xlsx_preserves_sheet_rows_and_formula() -> None:
    """XLSX 保留工作表名、行号、空列表述和公式文本。"""
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "需求表"
    sheet.append(["字段", "规则"])
    sheet.append([None, None])  # 空白第 2 行不应让后续原始行号错位
    sheet.append(["总额", "=SUM(C2:C4)"])
    buffer = BytesIO()
    workbook.save(buffer)
    workbook.close()

    result = parse_document("requirements.xlsx", buffer.getvalue())
    assert "工作表：需求表" in result.plain_text
    assert "行 3：" in result.plain_text
    assert "行 2：" not in result.plain_text
    assert "=SUM(C2:C4)" in result.plain_text
    assert "## 工作表：需求表" in result.markdown


def test_legacy_xls_extracts_real_workbook() -> None:
    """旧版 XLS 也通过真实 BIFF 工作簿验证，不只是检查扩展名。"""
    xlwt = pytest.importorskip("xlwt")
    workbook = xlwt.Workbook()
    sheet = workbook.add_sheet("旧版需求")
    sheet.write(0, 0, "名称")
    sheet.write(0, 1, "说明")
    sheet.write(2, 0, "average")
    sheet.write(2, 1, "空列表返回 0")
    buffer = BytesIO()
    workbook.save(buffer)

    result = parse_document("legacy.xls", buffer.getvalue())
    assert "旧版需求" in result.plain_text
    assert "average" in result.plain_text
    assert "空列表返回 0" in result.plain_text
    assert "行 3：" in result.plain_text


def test_pdf_reads_text_layer() -> None:
    """有文本层的 PDF 不应触发 OCR，并保留页码。"""
    import pymupdf

    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), "RAG upload knowledge")
    data = document.tobytes()
    document.close()

    result = parse_document("knowledge.pdf", data)
    assert "[第 1 页]" in result.plain_text
    assert "RAG upload knowledge" in result.plain_text
    assert "## 第 1 页" in result.markdown
    assert not any("OCR" in warning for warning in result.warnings)


def test_pdf_ocr_failure_on_one_page_preserves_other_pages(monkeypatch) -> None:
    """单页 OCR 运行异常要记录警告，不能丢弃其他页面的文本层。"""
    import pymupdf

    class BrokenOCR:
        def extract(self, _image_array) -> str:
            raise RuntimeError("simulated OCR failure")

    monkeypatch.setattr(document_parser, "_get_ocr", lambda: BrokenOCR())
    document = pymupdf.open()
    first_page = document.new_page()
    first_page.insert_text((72, 72), "Readable text layer on first page")
    document.new_page()  # 第二页没有文本层，会进入 OCR 分支
    result = parse_document("partial.pdf", document.tobytes())
    document.close()

    assert "Readable text layer on first page" in result.plain_text
    assert "[第 1 页]" in result.plain_text
    assert any("第 2 页 OCR 异常：RuntimeError" in item for item in result.warnings)


def test_scanned_pdf_and_image_use_ocr(monkeypatch) -> None:
    """扫描页进入 OCR；普通图片也复用同一识别适配器。"""
    import pymupdf
    from PIL import Image, ImageDraw, ImageFont

    class FakeOCR:
        def extract(self, _image_array) -> str:
            return "扫描附件中的需求文字"

    monkeypatch.setattr(document_parser, "_get_ocr", lambda: FakeOCR())
    image = Image.new("RGB", (900, 240), "white")
    draw = ImageDraw.Draw(image)
    draw.text((30, 60), "SCANNED REQUIREMENT", fill="black", font=ImageFont.load_default(size=36))
    image_buffer = BytesIO()
    image.save(image_buffer, format="PNG")

    image_result = parse_document("scan.png", image_buffer.getvalue())
    assert image_result.plain_text == "扫描附件中的需求文字"
    assert "## 图片文字" in image_result.markdown

    document = pymupdf.open()
    page = document.new_page(width=900, height=240)
    page.insert_image(page.rect, stream=image_buffer.getvalue())
    pdf_result = parse_document("scan.pdf", document.tobytes())
    document.close()
    assert "扫描附件中的需求文字" in pdf_result.plain_text
    assert any("OCR" in warning for warning in pdf_result.warnings)


def test_md5_deduplicates_same_bytes_not_filename(upload_store) -> None:
    """相同字节即使改名也复用；同名不同内容必须生成不同 ID。"""
    data = b"# Requirement\n\nUse parameterized queries.\n"
    first = upload_store.save_upload("first.md", data)
    renamed = upload_store.save_upload("renamed.md", data)
    changed = upload_store.save_upload("first.md", data + b"Different content.\n")

    assert first.duplicate is False
    assert renamed.duplicate is True
    assert renamed.record["id"] == first.record["id"]
    assert renamed.record["filename"] == "first.md"
    assert changed.record["id"] != first.record["id"]
    assert len(upload_store.list_records()) == 2


def test_chat_only_and_later_knowledge_import_are_separate(upload_store) -> None:
    """聊天附件不入库；相同文件之后可以补入库且只变化一次。"""
    data = b"# Upload rule\n\nValidate empty input before division.\n"
    chat = upload_store.save_upload("rule.md", data, import_to_knowledge=False)
    assert upload_store.knowledge_files() == []

    duplicate_chat = upload_store.save_upload("rule.md", data, import_to_knowledge=False)
    assert duplicate_chat.duplicate is True
    assert duplicate_chat.knowledge_changed is False
    assert upload_store.knowledge_files() == []

    imported = upload_store.save_upload("rule.md", data, import_to_knowledge=True)
    assert imported.duplicate is True
    assert imported.knowledge_changed is True
    assert len(upload_store.knowledge_files()) == 1

    repeated = upload_store.save_upload("rule.md", data, import_to_knowledge=True)
    assert repeated.knowledge_changed is False
    assert len(upload_store.knowledge_files()) == 1
    assert chat.record["id"] == imported.record["id"]


def test_manifest_persists_and_corruption_is_not_overwritten(upload_store) -> None:
    """新实例可恢复清单；损坏清单必须报错而不是被空清单覆盖。"""
    saved = upload_store.save_upload("code.py", b"def add(a, b):\n    return a + b\n")
    reloaded = UploadService()
    records = reloaded.list_records()
    assert [record["id"] for record in records] == [saved.record["id"]]
    assert reloaded.attachments_for_chat([saved.record["id"]])[0].text.startswith("def add")

    settings.UPLOAD_MANIFEST_PATH.write_text("{broken", encoding="utf-8")
    broken = UploadService()
    with pytest.raises(UploadError, match="附件清单无法读取"):
        broken.list_records()
    assert settings.UPLOAD_MANIFEST_PATH.read_text(encoding="utf-8") == "{broken"


def test_missing_text_is_reported_and_restored_by_reupload(upload_store) -> None:
    """提取文本丢失时给出可读错误；重传相同字节可修复而不新建记录。"""
    data = b"def add(a, b):\n    return a + b\n"
    saved = upload_store.save_upload("restore.py", data)
    text_path = settings.UPLOAD_DIR / "text" / f"{saved.record['id']}.txt"
    text_path.unlink()
    with pytest.raises(UploadError, match="提取文本缺失"):
        upload_store.attachments_for_chat([saved.record["id"]])

    restored = upload_store.save_upload("restore.py", data)
    assert restored.duplicate is True
    assert restored.record["id"] == saved.record["id"]
    assert text_path.exists()


def test_duplicate_without_changes_does_not_rewrite_manifest(upload_store, monkeypatch) -> None:
    """纯重复上传不修改元数据，就不应再次写磁盘清单。"""
    data = b"same bytes"
    upload_store.save_upload("same.txt", data)

    def unexpected_save() -> None:
        raise AssertionError("无变化的重复上传不应调用 _save_locked")

    monkeypatch.setattr(upload_store, "_save_locked", unexpected_save)
    duplicate = upload_store.save_upload("same-renamed.txt", data)
    assert duplicate.duplicate is True
    assert duplicate.knowledge_changed is False


def test_failed_manifest_save_rolls_back_knowledge_flag(upload_store, monkeypatch) -> None:
    """入库清单写失败后，内存和磁盘都必须保持未入库状态。"""
    data = b"# Rule\n\nCheck empty input."
    saved = upload_store.save_upload("rollback.md", data)

    def fail_manifest_save() -> None:
        raise OSError("disk full")

    with monkeypatch.context() as patch:
        patch.setattr(upload_store, "_save_locked", fail_manifest_save)
        with pytest.raises(OSError, match="disk full"):
            upload_store.save_upload("rollback.md", data, import_to_knowledge=True)
        assert upload_store.list_records()[0]["in_knowledge"] is False
        assert UploadService().list_records()[0]["in_knowledge"] is False

    imported = upload_store.save_upload(saved.record["filename"], data, import_to_knowledge=True)
    assert imported.knowledge_changed is True
    assert imported.record["in_knowledge"] is True


def test_chat_text_budget_includes_truncation_marker(upload_store, monkeypatch) -> None:
    """附件正文与截断提示合计也不能超过配置的总字符预算。"""
    monkeypatch.setattr(settings, "UPLOAD_CHAT_FILE_CHARS", 25)
    monkeypatch.setattr(settings, "UPLOAD_CHAT_TOTAL_CHARS", 40)
    first = upload_store.save_upload("a.txt", b"a" * 100)
    second = upload_store.save_upload("b.txt", b"b" * 100)

    attachments = upload_store.attachments_for_chat([first.record["id"], second.record["id"]])
    assert all(item.truncated for item in attachments)
    assert sum(len(item.text) for item in attachments) <= 40
    assert all(len(item.text) <= 25 for item in attachments)


def test_generation_and_review_keep_code_separate_from_markdown(upload_store) -> None:
    """审核附件使用 Python 注释分隔，不混入 RAG 的 Markdown 表格标记。"""
    from api.server import _generation_input, _review_input

    saved = upload_store.save_upload("sample.py", b"def add(a, b):\n    return a + b\n")
    attachments = upload_store.attachments_for_chat([saved.record["id"]])
    generation = _generation_input("请补充边界测试", attachments)
    review = _review_input("", attachments)
    assert "【附件：sample.py】" in generation
    assert review.startswith("# ===== 附件：sample.py =====")
    assert "## 内容" not in review
    compile(review, "sample.py", "exec")


def test_multipart_upload_refreshes_rag_once_for_duplicates(upload_store, monkeypatch) -> None:
    """multipart 接口可用；重复知识文件不会触发第二次索引刷新。"""
    from api import server

    class FakeRag:
        def __init__(self) -> None:
            self.calls = 0
            self.chunk_count = 48

        def refresh(self) -> bool:
            self.calls += 1
            return True

    fake_rag = FakeRag()
    monkeypatch.setattr(server, "upload_service", upload_store)
    monkeypatch.setattr(server, "rag_retriever", fake_rag)
    client = TestClient(server.app)
    content = b"# API upload\n\nUnique knowledge for endpoint testing.\n"

    first = client.post(
        "/api/uploads",
        files=[("files", ("api.md", content, "text/markdown"))],
        data={"import_to_knowledge": "true"},
    )
    assert first.status_code == 200
    assert first.json()["index_refreshed"] is True
    assert first.json()["results"][0]["knowledge_changed"] is True

    second = client.post(
        "/api/uploads",
        files=[("files", ("api-renamed.md", content, "text/markdown"))],
        data={"import_to_knowledge": "true"},
    )
    assert second.status_code == 200
    assert second.json()["results"][0]["status"] == "duplicate"
    assert second.json()["results"][0]["knowledge_changed"] is False
    assert fake_rag.calls == 1


def test_failed_index_refresh_can_retry_with_duplicate_file(upload_store, monkeypatch) -> None:
    """首次刷新失败后，重传 MD5 重复文件仍会重试，而不是被去重逻辑挡住。"""
    from api import server

    class FlakyRag:
        def __init__(self) -> None:
            self.calls = 0
            self.chunk_count = 49

        def refresh(self) -> bool:
            self.calls += 1
            return self.calls > 1

    fake_rag = FlakyRag()
    monkeypatch.setattr(server, "upload_service", upload_store)
    monkeypatch.setattr(server, "rag_retriever", fake_rag)
    monkeypatch.setattr(server, "_knowledge_refresh_pending", False)
    client = TestClient(server.app)
    content = b"# Retry upload\n\nRetry index refresh after a transient failure.\n"

    first = client.post(
        "/api/uploads",
        files=[("files", ("retry.md", content, "text/markdown"))],
        data={"import_to_knowledge": "true"},
    )
    assert first.json()["index_refreshed"] is False
    assert server._knowledge_refresh_pending is True

    second = client.post(
        "/api/uploads",
        files=[("files", ("retry.md", content, "text/markdown"))],
        data={"import_to_knowledge": "true"},
    )
    assert second.json()["results"][0]["status"] == "duplicate"
    assert second.json()["index_refreshed"] is True
    assert fake_rag.calls == 2
    assert server._knowledge_refresh_pending is False


def test_multipart_reports_file_error_without_rolling_back_success(
    upload_store, monkeypatch
) -> None:
    """不支持旧 .doc 时给出明确错误，同批合法文件仍可成功。"""
    from api import server

    monkeypatch.setattr(server, "upload_service", upload_store)
    client = TestClient(server.app)
    response = client.post(
        "/api/uploads",
        files=[
            ("files", ("old.doc", b"not-a-docx", "application/msword")),
            ("files", ("notes.txt", b"valid notes", "text/plain")),
        ],
    )
    assert response.status_code == 200
    results = response.json()["results"]
    assert results[0]["status"] == "error"
    assert ".doc" in results[0]["message"]
    assert results[1]["status"] == "uploaded"
