"""Identify file labels as editable column names and create an empty XLSX."""
from io import BytesIO
from pathlib import Path
import re
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile

MAX_FIELDS = 16384
MAX_TEXT_BYTES = 2 * 1024 * 1024
NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
KNOWN_LABELS = set("款号 正式款号 款式编号 款式号 产品款号 模特编号 序号 编号 面料 面料编号 面料编码 面料名称 面料颜色 面料色号 使用部位 部位 用途 辅料编号 辅料编码 辅料 辅料名称 辅料颜色 辅料色号 尺寸 尺码 规格 数量 用量 备注 说明 设计师 年份 年度 季节 供应商 供应商1 供应商2 交期 入库要求日 STYLE STYLENO FABRIC FABRICCOLOR COLOR SIZE QTY QUANTITY MEMO TAGCOLOR TAGNO TAGA TAGB TAGC TAGD".split())


def label_key(value):
    return "".join(value.split()).upper().rstrip(":：")


def explicit_label(value):
    match = re.match(r"^([^:：\n]{1,80})\s*[:：]", value.strip())
    if match and match.group(1).strip().lower() not in {"http", "https"}:
        return match.group(1).strip()
    return value.strip() if label_key(value) in KNOWN_LABELS else None


def validate_fields(values):
    if not isinstance(values, list) or not values or len(values) > MAX_FIELDS:
        raise ValueError("请保留 1 至 16384 个字段。")
    fields, seen, size = [], set(), 0
    for value in values:
        if not isinstance(value, str) or not value.strip() or len(value) > 32767:
            raise ValueError("字段名称不能为空，且每个字段最多 32767 个字符。")
        value = value.strip()
        if any(ord(char) < 32 and char not in "\t\n\r" for char in value):
            raise ValueError("字段名称包含无法写入 Excel 的字符。")
        if value in seen:
            raise ValueError(f"字段名称重复：{value[:60]}，请修改或删除重复项。")
        seen.add(value)
        size += len(value.encode("utf-8"))
        if size > MAX_TEXT_BYTES:
            raise ValueError("字段文字总量超过 2 MB，请减少字段。")
        fields.append(value)
    return fields


def collect_labels(filename):
    path = Path(filename)
    if path.suffix.lower() not in {".pptx", ".xlsx"} or not path.is_file():
        raise ValueError("请选择可读取的 PPTX 或 XLSX 文件。")
    fields, seen, text_bytes = [], set(), 0

    def add(value):
        nonlocal text_bytes
        if value is None:
            return
        if not isinstance(value, str):
            return
        value = str(value).strip()
        if not value or value in seen:
            return
        text_bytes += len(value.encode("utf-8"))
        if len(fields) >= MAX_FIELDS or text_bytes > MAX_TEXT_BYTES or len(value) > 32767:
            raise ValueError("文件中的候选字段过多或文字过长，请拆分文件后重试。")
        seen.add(value)
        fields.append(value)

    try:
        with ZipFile(path) as archive:
            if len(archive.infolist()) > 20000:
                raise ValueError("文件结构过大，请精简文件后重试。")
            xml_parts = [info for info in archive.infolist() if info.filename.endswith(".xml")]
            if sum(info.file_size for info in xml_parts) > 64 * 1024 * 1024:
                raise ValueError("文件中的表格或文字数据超过支持范围，请拆分文件。")
            if path.suffix.lower() == ".pptx":
                slides = [info for info in xml_parts if re.fullmatch(r"ppt/slides/slide\d+\.xml", info.filename)]
                slides.sort(key=lambda item: int(re.search(r"slide(\d+)\.xml", item.filename).group(1)))
                for info in slides:
                    raw = archive.read(info)
                    if b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:
                        raise ValueError("文件 XML 结构不受支持。")
                    root = ET.fromstring(raw)
                    table_paragraphs = set()
                    for table in root.iter(f"{{{NS}}}tbl"):
                        rows = []
                        for row in table.findall(f"{{{NS}}}tr"):
                            values = []
                            for cell in row.findall(f"{{{NS}}}tc"):
                                paragraphs = list(cell.iter(f"{{{NS}}}p"))
                                table_paragraphs.update(paragraphs)
                                values.append("\n".join("".join(t.text or "" for t in p.iter(f"{{{NS}}}t")) for p in paragraphs).strip())
                            if any(values):
                                rows.append(values)
                        if not rows:
                            continue
                        if (len(rows[0]) == 2 and rows[0][0] in {"字段", "属性", "项目", "标签"}
                                and rows[0][1] in {"值", "内容", "value", "Value"}):
                            for row in rows[1:]:
                                add(row[0])
                        elif all(len(row) == 2 and explicit_label(row[0]) and not explicit_label(row[1]) for row in rows):
                            for row in rows:
                                add(explicit_label(row[0]))
                        else:
                            # Native tables use the first row as their column labels.
                            for value in rows[0]:
                                add(explicit_label(value) or value)
                    for paragraph in root.iter(f"{{{NS}}}p"):
                        if paragraph not in table_paragraphs:
                            text = "".join(t.text or "" for t in paragraph.iter(f"{{{NS}}}t"))
                            add(explicit_label(text))
        if path.suffix.lower() == ".xlsx":
            from openpyxl import load_workbook
            book = load_workbook(path, read_only=True, data_only=True, keep_links=False)
            try:
                for sheet in book.worksheets:
                    sheet.reset_dimensions()
                    for row in sheet.iter_rows(min_row=1, max_row=1, values_only=True):
                        for value in row:
                            add(value)
            finally:
                book.close()
    except (BadZipFile, ET.ParseError, KeyError, OSError, RuntimeError) as error:
        raise ValueError("无法识别文件内容，请确认文件完整且未加密。") from error
    if not fields:
        raise ValueError("没有识别到字段标签；请检查表格表头或“标签：内容”格式，图片文字暂不支持识别。")
    return fields


def create_template(values):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    fields = validate_fields(values)
    book = Workbook()
    try:
        sheet = book.active
        sheet.title = "提取模板"
        sheet.freeze_panes = "A2"
        for index, name in enumerate(fields, 1):
            header = sheet.cell(1, index, name)
            header.data_type = "s"
            header.font = Font(name="Microsoft YaHei", size=11, bold=True, color="FFFFFF")
            header.fill = PatternFill("solid", fgColor="28785E")
            header.alignment = Alignment(vertical="center", wrap_text=True)
            sheet.cell(2, index).font = Font(name="Microsoft YaHei", size=11)
            sheet.column_dimensions[get_column_letter(index)].width = min(40, max(14, len(name) * 1.5 + 4))
        sheet.row_dimensions[1].height = 32
        sheet.row_dimensions[2].height = 22
        sheet.auto_filter.ref = f"A1:{get_column_letter(len(fields))}1"
        output = BytesIO()
        book.save(output)
        return output.getvalue()
    finally:
        book.close()
