"""Download verified manuals into bounded ZIP assets on a GitHub release."""
import argparse
import json
import os
import re
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path

from vault import fetch, parse_html, sha256, load_catalog

MAX_ARCHIVE = 900 * 1024 * 1024


def gh(*args):
    return subprocess.check_output(["gh", *map(str, args)], text=True)


def upload(tag, path):
    for attempt in range(5):
        try:
            gh("release", "upload", tag, path, "--clobber")
            return
        except subprocess.CalledProcessError:
            if attempt == 4:
                raise
            time.sleep(5 * (attempt + 1))


def validate(path, fmt, expected_size=None):
    size = path.stat().st_size
    if not size:
        raise ValueError("Empty file")
    if expected_size and size != expected_size:
        raise ValueError(f"Size mismatch: {size} != {expected_size}")
    if fmt == "pdf":
        with path.open("rb") as f:
            if b"%PDF-" not in f.read(1024):
                raise ValueError("Not a PDF")
    elif fmt == "epub":
        with zipfile.ZipFile(path) as z:
            if z.read("mimetype").strip() != b"application/epub+zip":
                raise ValueError("Not an EPUB")
            bad = z.testzip()
            if bad:
                raise ValueError(f"Corrupt EPUB member: {bad}")
    return {"bytes": size, "sha256": sha256(path)}


class Bundles:
    def __init__(self, directory, shard, tag):
        self.directory, self.shard, self.tag = directory, shard, tag
        self.part, self.z, self.path, self.bytes = 0, None, None, 0
        self.assets = []

    def add(self, path, arcname):
        size = path.stat().st_size
        if size >= MAX_ARCHIVE:
            raise ValueError(f"File exceeds configured archive limit: {arcname}")
        if self.z and self.bytes + size > MAX_ARCHIVE:
            self.flush()
        if not self.z:
            self.part += 1
            self.path = self.directory / f"manuals-{self.shard:02}-part-{self.part:03}.zip"
            self.z = zipfile.ZipFile(self.path, "w", compression=zipfile.ZIP_STORED)
        self.z.write(path, arcname)
        self.bytes += size
        return self.path.name

    def flush(self):
        if self.z is None:
            return
        self.z.close()
        item = {"name": self.path.name, "bytes": self.path.stat().st_size,
                "sha256": sha256(self.path)}
        upload(self.tag, self.path)
        self.assets.append(item)
        print(f"Uploaded {self.path.name}: {item['bytes']:,} bytes", flush=True)
        self.path.unlink()
        self.z, self.bytes = None, 0


def archive(args):
    catalog = load_catalog(args.catalog)
    fingerprint = sha256(args.catalog)
    selected = catalog["manuals"][args.shard::args.shards]
    expected_ids = sorted(r["id"] for r in selected)
    report_name = f"manifest-{args.shard:02}.json"
    with tempfile.TemporaryDirectory() as temp:
        directory = Path(temp)
        old = directory / report_name
        try:
            gh("release", "download", args.tag, "--pattern", report_name, "--dir", directory)
            prior = json.loads(old.read_text())
            if (prior.get("complete") and prior.get("catalog_sha256") == fingerprint
                    and sorted(r["id"] for r in prior["manuals"]) == expected_ids):
                print(f"Shard {args.shard} already complete", flush=True)
                return
        except (subprocess.CalledProcessError, ValueError, KeyError):
            pass
        report = {"schema_version": 1, "catalog_sha256": fingerprint, "shard": args.shard,
                  "shards": args.shards, "expected_manuals": len(selected),
                  "complete": False, "manuals": [], "errors": [], "archives": []}
        bundles = Bundles(directory, args.shard, args.tag)
        for number, row in enumerate(selected, 1):
            pub_id = row["id"]
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", pub_id):
                raise ValueError(f"Unsafe publication ID: {pub_id}")
            try:
                pub = parse_html(fetch(row["source_url"]).decode())["pub"]
                if pub["id"] != pub_id:
                    raise ValueError(f"Unexpected manual ID: {pub['id']}")
                item = dict(pub, source_url=row["source_url"], files=[])
                # Download and validate both before adding either file to the archive.
                downloaded = []
                for fmt in ("pdf", "epub"):
                    url = pub.get(fmt + "_url")
                    if not url:
                        raise ValueError(f"Missing {fmt} URL")
                    path = directory / f"{pub_id}.{fmt}"
                    source_size = next((x.get("file_size") for x in pub.get("assets", [])
                                        if x["format"] == fmt), None)
                    headers = directory / "response-headers.txt"
                    for attempt in range(3):
                        try:
                            fetch(url, path, headers)
                            final_headers = headers.read_text().strip().split("\n\n")[-1]
                            length = re.search(r"(?im)^content-length:\s*(\d+)", final_headers)
                            expected = int(length[1]) if length else None
                            checks = validate(path, fmt, expected)
                            checks["source_reported_bytes"] = source_size
                            checks["source_size_matches"] = source_size is None or source_size == checks["bytes"]
                            break
                        except (subprocess.SubprocessError, ValueError, zipfile.BadZipFile, KeyError):
                            path.unlink(missing_ok=True)
                            if attempt == 2:
                                raise
                            time.sleep(5 * (attempt+1))
                    downloaded.append((fmt, url, path, checks))
                for fmt, url, path, checks in downloaded:
                    member = f"manuals/{pub_id}/original.{fmt}"
                    archive_name = bundles.add(path, member)
                    item["files"].append(dict(checks, format=fmt, source_url=url,
                                              archive=archive_name, member=member))
                    path.unlink()
                metadata = directory / f"{pub_id}.json"
                metadata.write_text(json.dumps(item, ensure_ascii=False, indent=2)+"\n")
                bundles.add(metadata, f"manuals/{pub_id}/metadata.json")
                metadata.unlink()
                report["manuals"].append(item)
            except Exception as exc:
                report["errors"].append({"id": pub_id, "source_url": row["source_url"],
                                         "error": str(exc)})
                print(f"ERROR {pub_id}: {exc}", flush=True)
                for path in directory.glob(f"{pub_id}.*"):
                    path.unlink(missing_ok=True)
            if number % 5 == 0 or number == len(selected):
                print(f"Shard {args.shard}: {number}/{len(selected)} processed, "
                      f"{len(report['errors'])} failed", flush=True)
            time.sleep(0.3)
        bundles.flush()
        report["archives"] = bundles.assets
        report["complete"] = not report["errors"] and len(report["manuals"]) == len(selected)
        old.write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n")
        upload(args.tag, old)
        if not report["complete"]:
            raise SystemExit(f"Incomplete shard: {len(report['errors'])} errors")


def finalize(args):
    catalog = load_catalog(args.catalog)
    fingerprint = sha256(args.catalog)
    with tempfile.TemporaryDirectory() as temp:
        directory = Path(temp)
        gh("release", "download", args.tag, "--pattern", "manifest-*.json", "--dir", directory)
        manifests = [json.loads(p.read_text()) for p in sorted(directory.glob("manifest-*.json"))]
        if (len(manifests) != args.shards or
                {m["shard"] for m in manifests} != set(range(args.shards)) or
                any(not m["complete"] or m["catalog_sha256"] != fingerprint for m in manifests)):
            raise ValueError("Missing, stale or incomplete download manifests")
        records = [m for manifest in manifests for m in manifest["manuals"]]
        expected_ids = {m["id"] for m in catalog["manuals"]}
        if len(records) != len(expected_ids) or {r["id"] for r in records} != expected_ids:
            raise ValueError("Downloaded manuals do not match the catalog")
        assets = json.loads(gh("release", "view", args.tag, "--json", "assets"))["assets"]
        actual = {a["name"]: a for a in assets}
        archives = [a for m in manifests for a in m["archives"]]
        for archive in archives:
            stored = actual.get(archive["name"], {})
            if stored.get("size") != archive["bytes"] or stored.get("state") != "uploaded":
                raise ValueError(f"Missing or incorrect release asset: {archive['name']}")
            digest = stored.get("digest")
            if digest and digest != "sha256:" + archive["sha256"]:
                raise ValueError(f"GitHub checksum mismatch: {archive['name']}")
        total_bytes = sum(f["bytes"] for r in records for f in r["files"])
        file_count = sum(len(r["files"]) for r in records)
        result = {"schema_version": 1, "complete": True, "source": catalog["source"],
                  "catalog_sha256": fingerprint, "manual_count": len(records),
                  "file_count": file_count, "file_bytes": total_bytes, "archives": archives,
                  "manuals": sorted(records, key=lambda r:r["id"])}
        path = directory / "index.json"
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n")
        upload(args.tag, path)
        checksums = directory / "SHA256SUMS"
        checksums.write_text("".join(f"{a['sha256']}  {a['name']}\n" for a in archives)
                             + f"{sha256(path)}  index.json\n")
        upload(args.tag, checksums)
        notes = directory / "release-notes.md"
        notes.write_text(f"Verified archive of {len(records):,} manuals from the "
                         f"[T.REX ARMS Field Manual Vault]({catalog['source']}).\n\n"
                         f"- {file_count:,} PDF and EPUB files\n"
                         f"- {total_bytes:,} original file bytes\n"
                         f"- {len(archives)} independent ZIP bundles\n"
                         "- `index.json` maps every manual to its files, checksums, metadata, "
                         "and source URLs\n"
                         "- `SHA256SUMS` verifies the ZIP bundles and index\n\n"
                         "Extract all ZIPs into the same directory. Each manual has its own "
                         "folder under `manuals/`. Historical manuals may be superseded. "
                         "Source distribution and copyright notices are preserved.\n")
        gh("release", "edit", args.tag, "--draft=false", "--notes-file", notes)
        print(f"COMPLETE: {len(records)} manuals; {file_count} files; {total_bytes} bytes", flush=True)
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
                f.write(notes.read_text())


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("command", choices=["download", "finalize"])
    p.add_argument("--catalog", type=Path, default=Path("catalog/manuals.json.gz"))
    p.add_argument("--tag", required=True)
    p.add_argument("--shard", type=int, default=0)
    p.add_argument("--shards", type=int, default=32)
    a = p.parse_args()
    if not 0 <= a.shard < a.shards:
        p.error("shard must be in [0, shards)")
    (archive if a.command == "download" else finalize)(a)
