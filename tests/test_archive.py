import json
import sys
import tempfile
import unittest
import zipfile
import subprocess
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from vault import parse_html, balanced, load_catalog
from archive import validate, Bundles, missing_epub, archive
from vault import sha256


class ArchiveTests(unittest.TestCase):
    def test_only_confirmed_missing_epubs_are_optional(self):
        with tempfile.TemporaryDirectory() as temp:
            headers = Path(temp)/"headers"
            exc = subprocess.CalledProcessError(22, ["curl"])
            headers.write_text("HTTP/1.1 200 Connection established\n\nHTTP/2 404\n")
            self.assertTrue(missing_epub(exc, "epub", headers))
            self.assertFalse(missing_epub(exc, "pdf", headers))
            headers.write_text("HTTP/2 503\n")
            self.assertFalse(missing_epub(exc, "epub", headers))
            headers.write_text("HTTP/2 404\n")
            self.assertFalse(missing_epub(ValueError("corrupt file"), "epub", headers))

    def test_resume_keeps_archived_manual_and_recovers_pdf_when_epub_missing(self):
        with tempfile.TemporaryDirectory() as temp:
            catalog = Path(temp)/"catalog.json"
            catalog.write_text(json.dumps({"manuals":[{"id":"done","source_url":"done"},{"id":"recover","source_url":"recover"}]}))
            prior = {"complete":False,"catalog_sha256":sha256(catalog),"shards":1,
                     "manuals":[{"id":"done","files":[{"format":"pdf"}]}],
                     "archives":[{"name":"manuals-00-part-001.zip","bytes":123,"sha256":"old"}]}
            captured = {}
            def mock_gh(*args):
                directory = Path(args[args.index("--dir")+1])
                (directory/"manifest-00.json").write_text(json.dumps(prior))
                return ""
            def mock_fetch(url, dest=None, headers=None):
                self.assertNotEqual(url,"done")
                if dest is None:
                    return b"fixture"
                if url == "epub":
                    headers.write_text("HTTP/2 404\n")
                    raise subprocess.CalledProcessError(22,["curl"])
                dest.write_bytes(b"%PDF-1.7\nfixture")
                headers.write_text(f"HTTP/2 200\ncontent-length: {dest.stat().st_size}\n")
            def mock_upload(tag,path):
                if path.suffix==".json":captured.update(json.loads(path.read_text()))
            with patch("archive.gh",mock_gh), patch("archive.fetch",mock_fetch), patch("archive.parse_html",return_value={"pub":{"id":"recover","pdf_url":"pdf","epub_url":"epub"}}), patch("archive.upload",mock_upload), patch("archive.time.sleep"):
                archive(SimpleNamespace(catalog=catalog,shard=0,shards=1,tag="test"))
            self.assertTrue(captured["complete"])
            self.assertEqual(len(captured["manuals"]),2)
            recovered = captured["manuals"][1]
            self.assertEqual([f["format"] for f in recovered["files"]],["pdf"])
            self.assertEqual(recovered["files"][0]["archive"],"manuals-00-part-002.zip")
            self.assertEqual(recovered["unavailable_formats"][0]["format"],"epub")
            self.assertEqual(captured["archives"][0],prior["archives"][0])

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

    def test_large_manual_uses_its_own_bundle(self):
        with tempfile.TemporaryDirectory() as temp, patch("archive.MAX_ARCHIVE", 15), patch("archive.MAX_SINGLE_FILE", 100), patch("archive.upload"):
            directory = Path(temp)
            path = directory/"file"
            bundles = Bundles(directory,0,"test")
            path.write_bytes(b"x"*10)
            first = bundles.add(path,"small.pdf")
            path.write_bytes(b"x"*30)
            large = bundles.add(path,"large.pdf")
            path.write_bytes(b"x"*10)
            last = bundles.add(path,"last.pdf")
            bundles.flush()
            self.assertEqual(len({first,large,last}),3)
            path.write_bytes(b"x"*100)
            with self.assertRaises(ValueError):
                bundles.add(path,"too-large.pdf")

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
