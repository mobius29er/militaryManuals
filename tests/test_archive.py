import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from vault import parse_html, balanced, load_catalog
from archive import validate, Bundles


class ArchiveTests(unittest.TestCase):
    def test_data_is_parsed_without_running_javascript(self):
        html = '<script>throw new Error("do not execute"); {type:"data",data:{pub:{id:"x",title:"Braces } and \\\"quotes\\\""}}}</script>'
        self.assertEqual(parse_html(html)["pub"]["id"], "x")

    def test_shared_svelte_strings_and_edition_metadata(self):
        html = '(function(a,b){return {type:"data",data:{list:{results:[{id:"a",title:a,description:b},{id:"b",title:a}]}}}}("A title",null))'
        rows = parse_html(html)["list"]["results"]
        self.assertEqual(rows[0], {"id":"a", "title":"A title", "description":None})
        self.assertEqual(rows[1]["title"], "A title")

    def test_html_error_cannot_be_stored_as_pdf(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/"x.pdf"
            path.write_bytes(b"<html>Access denied</html>")
            with self.assertRaises(ValueError):
                validate(path, "pdf")
            path.write_bytes(b"%PDF-1.7\nfixture")
            with self.assertRaises(ValueError):
                validate(path, "pdf", 100)
            result = validate(path, "pdf", path.stat().st_size)
            self.assertEqual(len(result["sha256"]), 64)

    def test_epub_mimetype_and_integrity(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/"x.epub"
            with zipfile.ZipFile(path, "w") as z:
                z.writestr("mimetype", "application/epub+zip")
                z.writestr("text.xhtml", "<html>test</html>")
            self.assertGreater(validate(path, "epub")["bytes"], 0)

    def test_bundles_flush_before_size_limit(self):
        with tempfile.TemporaryDirectory() as temp, patch("archive.MAX_ARCHIVE", 15), patch("archive.upload") as upload:
            directory = Path(temp)
            path = directory / "file"
            path.write_bytes(b"1234567890")
            bundles = Bundles(directory, 0, "test")
            first = bundles.add(path, "one.pdf")
            second = bundles.add(path, "two.pdf")
            bundles.flush()
            self.assertNotEqual(first, second)
            self.assertEqual(upload.call_count, 2)
            self.assertEqual(len(bundles.assets), 2)

    def test_checked_in_catalog_has_every_unique_manual(self):
        path = Path(__file__).resolve().parents[1]/"catalog/manuals.json.gz"
        if not path.exists():
            self.skipTest("Catalog discovery is still in progress")
        catalog = load_catalog(path)
        rows = catalog["manuals"]
        self.assertEqual(len(rows), catalog["expected_manuals"])
        self.assertEqual(len({r["id"] for r in rows}), len(rows))
        self.assertGreaterEqual(len(rows), 4200)
        self.assertTrue(all(r["source_url"].startswith(catalog["source"]+"/") for r in rows))


if __name__ == "__main__":
    unittest.main()
