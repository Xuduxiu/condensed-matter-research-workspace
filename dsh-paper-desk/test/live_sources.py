"""Opt-in live check: three metadata sources, one arXiv PDF and a Zotero package."""

import json
import os
from hashlib import sha256
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from zipfile import ZipFile


def main():
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root / "condmat-trend-radar"))
    with TemporaryDirectory(prefix="paper-desk-live-") as temporary:
        data = Path(temporary)
        os.environ.update({
            "CONDMAT_RADAR_DATA_DIR": temporary,
            "CONDMAT_RADAR_DB": str(data / "papers.sqlite"),
            "CONDMAT_RADAR_EXPORT": str(data / "exports"),
            "CONDMAT_RADAR_CACHE": str(data / "cache"),
            "CONDMAT_RADAR_LOCKS": str(data / "locks"),
            "CONDMAT_RADAR_LOGS": str(data / "logs"),
            "CONDMAT_RADAR_INSTITUTIONAL_ACCESS": "disabled",
        })
        from backend.api.main import initialize_database
        from backend.api.workbench_routes import (
            ImmediateDownloadRequest, RemoteImportRequest, download_now, import_remote, remote_search,
        )

        initialize_database()
        found = remote_search("topological insulators", "all", 6)
        # Error messages from upstream clients may contain signed URLs. Report only status and counts.
        statuses = {name: {"status": item["status"], "count": item["count"]}
                    for name, item in found["sources"].items()}
        print(json.dumps({"sources": statuses}, ensure_ascii=False), flush=True)
        record = {
            "source": "arxiv", "arxiv_id": "1002.3895", "title": "Colloquium: Topological Insulators",
            "url": "https://arxiv.org/abs/1002.3895", "pdf_url": "https://arxiv.org/pdf/1002.3895",
            "is_open_access": True, "categories": ["cond-mat.mes-hall"],
        }
        saved = import_remote(RemoteImportRequest(record=record))["paper"]
        result = download_now(saved["canonical_paper_id"], ImmediateDownloadRequest(
            paper_version_id=saved["paper_version_id"], zotero=True))
        download, package = result["download"], result["zotero"]
        with ZipFile(package["zip_path"]) as archive:
            files = archive.namelist()
            pdfs = [name for name in files if name.endswith(".pdf")]
            valid = (archive.testzip() is None and len(pdfs) == 1
                     and sha256(archive.read(pdfs[0])).hexdigest() == download.get("sha256")
                     and any(name.endswith(".ris") for name in files))
        print(json.dumps({"arxiv": "1002.3895", "download": download["status"],
                          "attachments": package["attachment_count"], "zip_with_pdf_and_ris": valid}))
        return 0 if valid and all(s["count"] > 0 for s in statuses.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
