"""Material upload limits, ownership, extraction and local retrieval."""

import asyncio
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZipFile

from fastapi import HTTPException, UploadFile
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session as DBSession, create_engine, select

os.environ.setdefault("MOONSHOT_API_KEY", "test-key")

from backend.api import materials as materials_api, sessions as sessions_api
from backend.materials import context, extract as extraction, files
from backend.materials.extract import MaterialError, extract_material
from backend.storage.models import Session as SessionModel, SessionMaterial, User


class MaterialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "materials"
        root_patch = patch.object(files, "MATERIALS_DIR", self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)

        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        SQLModel.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)
        engine_patch = patch.object(context, "engine", self.engine)
        engine_patch.start()
        self.addCleanup(engine_patch.stop)
        self.db = DBSession(self.engine)
        self.addCleanup(self.db.close)
        self.owner = User(username="owner", password_hash="test")
        self.other = User(username="other", password_hash="test")
        self.db.add_all([self.owner, self.other])
        self.db.commit()
        self.session = SessionModel(name="Lecture", user_id=self.owner.id, recording_state="recording")
        self.db.add(self.session)
        self.db.commit()

    def upload(self, filename: str, contents: bytes, user=None):
        file = UploadFile(file=io.BytesIO(contents), filename=filename)
        return asyncio.run(materials_api.upload_material(
            self.session.id, file, self.db, user or self.owner
        ))

    def test_upload_list_download_and_immediate_context_while_recording(self):
        created = self.upload("lesson.md", "광합성은 빛을 이용합니다.\n\n세포 호흡도 배웁니다.".encode())
        self.assertEqual(created["extraction_status"], "ready")
        self.assertEqual(created["filename"], "lesson.md")
        self.assertEqual(len(materials_api.list_materials(self.session.id, self.db, self.owner)["materials"]), 1)
        response = materials_api.download_material(self.session.id, created["id"], self.db, self.owner)
        self.assertEqual(Path(response.path).read_bytes(), "광합성은 빛을 이용합니다.\n\n세포 호흡도 배웁니다.".encode())
        matched = context.get_relevant_material_context(self.session.id, "광합성이 어떻게 되나요?", 100)
        self.assertIn("광합성", matched)
        self.assertIn("lesson.md", matched)
        self.assertLessEqual(len(matched), 100)
        self.assertEqual(context.get_relevant_material_context(self.session.id, "quantum entanglement", 100), "")

    def test_foreign_account_cannot_access_or_add_materials(self):
        created = self.upload("lesson.txt", b"Safe text")
        calls = (
            lambda: materials_api.list_materials(self.session.id, self.db, self.other),
            lambda: materials_api.download_material(self.session.id, created["id"], self.db, self.other),
            lambda: asyncio.run(materials_api.upload_material(
                self.session.id, UploadFile(file=io.BytesIO(b"x"), filename="x.txt"), self.db, self.other
            )),
        )
        for call in calls:
            with self.subTest(call=call), self.assertRaises(HTTPException) as denied:
                call()
            self.assertEqual(denied.exception.status_code, 404)

    def test_scanned_pdf_has_explicit_ocr_status(self):
        from pypdf import PdfWriter

        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        data = io.BytesIO()
        writer.write(data)
        created = self.upload("scan.pdf", data.getvalue())
        self.assertEqual(created["extraction_status"], "needs_ocr")
        self.assertEqual(created["extracted_char_count"], 0)
        self.assertIn("OCR", created["message"])

    def test_office_text_and_legacy_ppt_rejection(self):
        from docx import Document
        from pptx import Presentation

        document = Document()
        document.add_paragraph("광합성 강의 노트")
        data = io.BytesIO()
        document.save(data)
        self.assertEqual(self.upload("notes.docx", data.getvalue())["extraction_status"], "ready")

        deck = Presentation()
        slide = deck.slides.add_slide(deck.slide_layouts[6])
        textbox = slide.shapes.add_textbox(0, 0, 1000000, 1000000)
        textbox.text = "광합성 발표 슬라이드"
        data = io.BytesIO()
        deck.save(data)
        self.assertEqual(self.upload("slides.pptx", data.getvalue())["extraction_status"], "ready")

        with self.assertRaises(HTTPException) as rejected:
            self.upload("old.ppt", b"legacy")
        self.assertEqual(rejected.exception.status_code, 415)

    def test_zip_bomb_and_path_traversal_are_rejected(self):
        bomb = Path(self.temp.name) / "bomb.docx"
        with ZipFile(bomb, "w", ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", "<Types/>")
            archive.writestr("word/document.xml", b"A" * 1_000_000)
        with self.assertRaises(MaterialError) as oversized:
            extract_material(bomb, ".docx")
        self.assertEqual(oversized.exception.status_code, 413)

        unsafe = Path(self.temp.name) / "unsafe.docx"
        with ZipFile(unsafe, "w", ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", "<Types/>")
            archive.writestr("word/document.xml", "<document/>")
            archive.writestr("../outside", "x")
        with self.assertRaises(MaterialError):
            extract_material(unsafe, ".docx")

    def test_stage_restore_and_finalize_touch_only_temp_material(self):
        created = self.upload("lesson.txt", b"lecture notes")
        path = files.material_path(self.session.id, created["id"], ".txt")
        staged = materials_api.stage_session_materials(self.db, self.session.id)
        self.assertFalse(path.exists())
        self.db.rollback()
        staged.restore()
        self.assertTrue(path.exists())
        self.assertIsNotNone(self.db.get(SessionMaterial, created["id"]))

        staged = materials_api.stage_session_materials(self.db, self.session.id)
        self.db.commit()
        staged.finalize()
        self.assertFalse(path.exists())
        self.assertEqual(self.db.exec(select(SessionMaterial)).all(), [])

    def test_owner_can_delete_one_material_but_foreign_user_cannot(self):
        created = self.upload("lesson.txt", b"lecture notes")
        path = files.material_path(self.session.id, created["id"], ".txt")
        with self.assertRaises(HTTPException) as denied:
            materials_api.delete_material(self.session.id, created["id"], self.db, self.other)
        self.assertEqual(denied.exception.status_code, 404)
        self.assertTrue(path.exists())

        materials_api.delete_material(self.session.id, created["id"], self.db, self.owner)
        self.assertIsNone(self.db.get(SessionMaterial, created["id"]))
        self.assertFalse(path.exists())

    def test_session_delete_cleans_material_rows_and_files(self):
        created = self.upload("lesson.txt", b"lecture notes")
        path = files.material_path(self.session.id, created["id"], ".txt")
        self.session.recording_state = "ended"
        self.db.commit()

        sessions_api.delete_session(self.session.id, self.db, self.owner)

        self.assertIsNone(self.db.get(SessionModel, self.session.id))
        self.assertEqual(self.db.exec(select(SessionMaterial)).all(), [])
        self.assertFalse(path.exists())

    def test_filename_and_size_limits(self):
        with self.assertRaises(HTTPException) as name_error:
            self.upload("../bad.exe", b"bad")
        self.assertEqual(name_error.exception.status_code, 415)
        with patch.object(materials_api, "MAX_FILE_BYTES", 3):
            with self.assertRaises(HTTPException) as size_error:
                self.upload("too-large.txt", b"four")
        self.assertEqual(size_error.exception.status_code, 413)
        with patch.object(extraction, "MAX_EXTRACTED_CHARS", 10):
            with self.assertRaises(HTTPException) as text_error:
                self.upload("too-much-text.md", b"eleven chars")
        self.assertEqual(text_error.exception.status_code, 413)
        self.assertEqual(self.db.exec(select(SessionMaterial)).all(), [])
        with self.assertRaises(ValueError):
            files.material_path("../../outside", self.owner.id, ".txt")

    def test_pdf_page_limit(self):
        from pypdf import PdfWriter

        writer = PdfWriter()
        for _ in range(extraction.MAX_PDF_PAGES + 1):
            writer.add_blank_page(width=200, height=200)
        data = io.BytesIO()
        writer.write(data)
        with self.assertRaises(HTTPException) as rejected:
            self.upload("too-many-pages.pdf", data.getvalue())
        self.assertEqual(rejected.exception.status_code, 413)


if __name__ == "__main__":
    unittest.main()
