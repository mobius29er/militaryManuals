# Military Manuals

An archive and analysis-ready catalog of the publications in the
[T.REX ARMS Field Manual Vault](https://www.trex-arms.com/field-manual-vault).

## Download status

The [archive workflow](https://github.com/mobius29er/militaryManuals/actions/workflows/archive.yml)
downloads the full catalog. The release stays a draft until all publications,
every PDF and all available EPUB versions pass verification. A catalog alone is not a completed
download.

After completion, the files are available under
[Releases](https://github.com/mobius29er/militaryManuals/releases). Download the
`manuals-XX-part-XXX.zip` assets and extract them into the same directory. Each
publication has `original.pdf`, `original.epub`, and `metadata.json` under
`manuals/<publication-id>/`.

`index.json` maps every manual to its archive members, SHA-256 hashes, byte sizes,
source URLs, title, designation, publication date, service, category, table of
contents, references and source distribution statement where provided.
`SHA256SUMS` verifies the archive bundles and index. The JSON data can seed a
database or search index for a future app.

## Storage and verification

The repository holds code and the complete discovery catalog in
`catalog/manuals.json.gz` (gzip-compressed JSON). Large binaries are
stored as independent ZIP release assets below GitHub's per-asset size limit.
Four download workers run at a time, with retries and delays. Completed manuals and their archive bundles are reused on reruns of the same
catalog. Incomplete shards retry only their missing manuals. A confirmed HTTP
404 for an EPUB is recorded as unavailable; the PDF is still required and retained.
Every file is checked against the HTTP response length when supplied, its format,
and SHA-256. Catalog byte sizes can be stale; discrepancies are recorded in the
manifest without discarding a complete, valid download.
EPUB ZIP integrity is also checked. Publication requires all catalog IDs to be
present and the release assets to have the expected sizes and uploaded state.

The source's public HTML supplies download URLs and metadata. Discovery expands
grouped editions and reconciles the unique count against the source's collection
counts. No JavaScript from the source is executed and no private API is used.

## Run locally

Requires Python 3.12+, curl, and the authenticated GitHub CLI for release uploads.

```sh
pip install -r requirements.txt
python -m unittest discover -s tests -v
python scripts/vault.py --output catalog/manuals.json
```

The workflow runs on relevant pushes to `main` or from its **Run workflow** button.
Use **Re-run failed jobs** to retry an interrupted download. A new catalog snapshot
should use a new `ARCHIVE_TAG` in the workflow.

## Provenance

The Vault describes the collection as public-domain U.S. military publications.
Original files and their notices are preserved. Collection metadata is attributed
to the Vault. Historical and superseded editions remain distinct. Any app should
retain publication dates, source citations and distribution notices, and review
third-party material and applicable reuse rights before republishing excerpts.

This project is not affiliated with T.REX ARMS or the U.S. Department of Defense.
