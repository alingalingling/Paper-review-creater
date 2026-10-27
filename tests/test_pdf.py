import importlib.util
import tempfile
import unittest
from pathlib import Path

from trace_review.core import check_evidence, read_documents


@unittest.skipUnless(importlib.util.find_spec("pypdf"), "安装 requirements.txt 后运行 PDF 集成检查")
class PdfTests(unittest.TestCase):
    def test_real_pdf_extraction_and_page_position(self):
        from pypdf import PdfWriter
        from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "sample.pdf"
            writer = PdfWriter()
            page = writer.add_blank_page(width=595, height=842)
            font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                                     NameObject("/Subtype"): NameObject("/Type1"),
                                     NameObject("/BaseFont"): NameObject("/Helvetica")})
            page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
            stream = DecodedStreamObject()
            stream.set_data(b"BT /F1 12 Tf 40 760 Td (Feedback does not prove learning gains.) Tj ET")
            page[NameObject("/Contents")] = writer._add_object(stream)
            writer.write(target)
            corpus = read_documents([target])
            document = corpus["documents"][0]
            self.assertIn("does not prove", document["pages"][0]["text"])
            evidence = {"source_id": document["id"], "page": 1, "quote": "Feedback does not prove learning gains."}
            self.assertTrue(check_evidence(corpus, evidence)["valid"])

    def test_image_only_or_empty_pdf_not_silently_accepted(self):
        from pypdf import PdfWriter
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "blank.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=595, height=842)
            writer.write(target)
            with self.assertRaisesRegex(ValueError, "没有可用文字"):
                read_documents([target])
