"""Build the required archive from an explicit, credential-checked allowlist."""
from pathlib import Path
import re
import stat
import zipfile

ROOT = Path(__file__).resolve().parents[1]
FILES = [
    "system/run", "system/run.cmd", "system/cli.py", "system/domain.py", "system/engine.py",
    "system/matching.py", "system/model.py", "system/serve.py", "system/verify.py",
    "system/test_system.py", "system/test_evidence_integrity.py", "system/test_model_transport.py",
    "system/README.md", "system/.gitignore", "system/viewer/index.html",
    "system/viewer/app.js", "system/viewer/style.css", "system/reports/verification.json",
    "system/reports/integrity_summary.json", "system/reports/model_verification.json",
    "start-dashboard.cmd", "output/bank.jsonl", "output/emoney.jsonl",
    "output/investment.jsonl", "output/payments_fx.jsonl",
]


def main():
    contents = {}
    for name in FILES:
        contents[name] = (ROOT / name).read_bytes()
        if re.search(rb"\b(?:sk-or-v1-|sk-proj-|xai-)[A-Za-z0-9_-]{20,}", contents[name]):
            raise ValueError("Credential detected in " + name)
        if re.search(rb"https://api\.x\.ai|XAI_API_KEY", contents[name]):
            raise ValueError("Development provider must not enter submission: " + name)
    if b"\r" in contents["system/run"]:
        raise ValueError("Entry point must have LF line endings")
    target = ROOT / "submission.zip"
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data in sorted(contents.items()):
            item = zipfile.ZipInfo(name, (2026, 11, 21, 0, 0, 0))
            item.create_system = 3
            item.external_attr = (stat.S_IFREG | (0o755 if name == "system/run" else 0o644)) << 16
            item.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(item, data)
    with zipfile.ZipFile(target) as archive:
        assert archive.testzip() is None
        assert set(archive.namelist()) == set(FILES)
    print(f"Built {target.name}: {len(FILES)} allowlisted files, {target.stat().st_size:,} bytes")


if __name__ == "__main__":
    main()
