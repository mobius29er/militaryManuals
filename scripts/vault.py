"""Read the Vault's public HTML without executing JavaScript or using private APIs."""
import concurrent.futures
import hashlib
import gzip
import json
import math
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import urlencode, urlparse

import json5

BASE = "https://www.trex-arms.com/field-manual-vault"
COLLECTIONS = ("current", "recent-archive", "lessons-learned", "historic")
USER_AGENT = "MilitaryManualsArchive/1.0 (+https://github.com/mobius29er/militaryManuals)"


def fetch(url, dest=None, headers=None):
    host = urlparse(url).hostname
    if host not in {"www.trex-arms.com", "files.trex-arms.com"}:
        raise ValueError(f"Unexpected source host: {host}")
    cmd = ["curl", "--fail", "--silent", "--show-error", "--location",
           "--proto", "=https", "--proto-redir", "=https", "--connect-timeout", "30",
           "--max-time", "1200", "--retry", "5", "--retry-delay", "5",
           "--retry-max-time", "1800", "--user-agent", USER_AGENT]
    if dest:
        cmd += ["--output", str(dest)]
    if headers:
        cmd += ["--dump-header", str(headers)]
    cmd.append(url)
    return subprocess.check_output(cmd, timeout=2400)


def balanced(text, start, opening="{", closing="}"):
    depth, quoted, escaped = 0, None, False
    for i in range(start, len(text)):
        ch = text[i]
        if quoted:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quoted:
                quoted = None
        elif ch in ('"', "'"):
            quoted = ch
        elif ch == opening:
            depth += 1
        elif ch == closing:
            depth -= 1
            if depth == 0:
                return i + 1
    raise ValueError("Unterminated serialized data")


def parse_html(text, marker='{type:"data",data:'):
    start = text.rfind(marker)
    if start < 0:
        raise ValueError("Public HTML data was not found")
    end = balanced(text, start)
    raw = text[start:end]
    wrapper = re.search(r"\(function\(([^)]*)\)\{return\s*$", text[:start])
    if wrapper:
        names = wrapper.group(1).split(",")
        if text[end:end+2] != "}(":
            raise ValueError("Unexpected data wrapper")
        args_end = balanced(text, end+1, "(", ")")
        args = json5.loads("[" + text[end+2:args_end-1] + "]")
        if len(names) != len(args):
            raise ValueError("Mismatched data wrapper arguments")
        bindings = dict(zip(names, args))
        # Only substitute identifier tokens outside string literals. Never eval source HTML.
        tokens = re.split(r'("(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\')', raw)
        for i in range(0, len(tokens), 2):
            tokens[i] = re.sub(r"\b[A-Za-z_$][\w$]*\b",
                               lambda m: json.dumps(bindings[m[0]]) if m[0] in bindings else m[0],
                               tokens[i])
        raw = "".join(tokens)
    return json5.loads(raw)["data"]


def listing(collection, page, cache):
    path = cache / f"{collection}-{page:03}.json"
    if path.exists():
        return json.loads(path.read_text())
    url = BASE + "?" + urlencode({"coll": collection, "page": page})
    data = parse_html(fetch(url).decode())["list"]
    if data["page"] != page:
        raise ValueError(f"Wrong page at {url}")
    path.write_text(json.dumps(data, ensure_ascii=False))
    return data


def discover(output, cache, workers=3):
    cache.mkdir(parents=True, exist_ok=True)
    pages, tasks, totals = [], [], {}
    for collection in COLLECTIONS:
        data = listing(collection, 1, cache)
        totals[collection] = data["searched"]
        pages.append(data)
        tasks.extend((collection, p) for p in range(2, math.ceil(data["total"]/data["size"])+1))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(listing, c, p, cache) for c, p in tasks]
        for i, future in enumerate(concurrent.futures.as_completed(futures), 1):
            pages.append(future.result())
            if i % 10 == 0:
                print(f"Catalog pages: {i+4}/{len(tasks)+4}", flush=True)
    records = {}
    for page in pages:
        for row in page["results"]:
            records[row["id"]] = row
    # Search cards group editions. Include every linked edition, even when it has no card.
    for page in pages:
        for row in page["results"]:
            for edition in row.get("editions", []):
                if edition["id"] not in records:
                    records[edition["id"]] = {**edition, "designation": row["designation"],
                                              "title": row["title"], "edition_metadata_pending": True}
    expected = sum(totals.values())
    if len(records) != expected:
        raise ValueError(f"Catalog incomplete: {len(records)} unique IDs, expected {expected}")
    result = {"source": BASE, "retrieved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "expected_manuals": expected, "collection_totals": totals,
              "manuals": [dict(row, source_url=f"{BASE}/{row['slug']}")
                          for row in sorted(records.values(), key=lambda r:r["id"])]}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, ensure_ascii=False)+"\n")
    print(f"Catalog complete: {len(records)} unique manuals", flush=True)
    return result


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024*1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_catalog(path):
    raw = path.read_bytes()
    if path.suffix == ".gz":
        raw = gzip.decompress(raw)
    return json.loads(raw)


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--output", type=Path, default=Path("catalog/manuals.json"))
    p.add_argument("--cache", type=Path, default=Path(".cache/catalog"))
    p.add_argument("--workers", type=int, default=3)
    a = p.parse_args()
    discover(a.output, a.cache, a.workers)
