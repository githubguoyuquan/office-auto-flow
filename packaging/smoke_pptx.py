"""Exercise the packaged Go executable with an actual PPTX and XLSX template."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

from openpyxl import Workbook, load_workbook


def main():
    binary = Path(sys.argv[1]).resolve()
    with tempfile.TemporaryDirectory(prefix="aioa-pptx-smoke-") as tmp:
        root = Path(tmp)
        template = root / "模板.xlsx"
        book = Workbook()
        book.active.append(["款号", "面料", "面料颜色", "使用部位"])
        book.save(template)
        book.close()
        source = root / "中文 样本.pptx"
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr("ppt/presentation.xml", '<p:presentation xmlns:p="p" xmlns:r="r"><p:sldIdLst><p:sldId r:id="one"/></p:sldIdLst></p:presentation>')
            archive.writestr("ppt/_rels/presentation.xml.rels", '<Relationships><Relationship Id="one" Target="slides/slide1.xml"/></Relationships>')
            rows = [["序号", "面料", "颜色", "使用部位"], ["1", "FAB001", "黑色", "大身"]]
            table = ''.join('<a:tr>' + ''.join('<a:tc><a:txBody><a:p><a:r><a:t>' + value + '</a:t></a:r></a:p></a:txBody></a:tc>' for value in row) + '</a:tr>' for row in rows)
            archive.writestr("ppt/slides/slide1.xml", '<p:sld xmlns:p="p" xmlns:a="a"><a:p><a:r><a:t>TSOW270702</a:t></a:r></a:p><a:tbl>' + table + '</a:tbl></p:sld>')
        request = {"action":"extract", "template":str(template), "inputs":[str(source)], "output_dir":str(root)}
        result = subprocess.run([str(binary)], input=json.dumps(request).encode("utf-8"),
                                capture_output=True, timeout=30, check=True)
        events = [json.loads(line) for line in result.stdout.decode("utf-8").splitlines()]
        final = events[-1]
        assert final["type"] == "done" and final["success"] == 1, events
        book = load_workbook(final["output"])
        try:
            assert list(book.active.values) == [("款号", "面料", "面料颜色", "使用部位"),
                                                ("TSOW270702", "FAB001", "黑色", "大身")]
        finally:
            book.close()
    print("Packaged PPTX component smoke test OK")


if __name__ == "__main__":
    main()
