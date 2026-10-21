"""Shared batch schema and file validation; no PDF or SMTP dependencies."""
import csv
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path


EMAIL_RE = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,}")
FIELDS = ["batch_id", "id", "recipient_email", "pdf_file", "original_name", "sha256",
          "source_sha256", "page_count", "generated_at", "generation_result", "error"]
BINDING = FIELDS[:6]
SEND_FIELDS = BINDING + ["recorded_at", "state", "attempt_id", "sender_email", "content_sha256",
                        "message_sha256", "submitted_at", "sent_at", "message_id", "error"]


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def valid_email(value):
    if not EMAIL_RE.fullmatch(value) or len(value) > 254 or ".." in value:
        return False
    local, domain = value.rsplit("@", 1)
    return (0 < len(local) <= 64 and not local.startswith(".") and not local.endswith(".")
            and all(0 < len(label) <= 63 and not label.startswith("-") and not label.endswith("-")
                    for label in domain.split(".")))


def read_csv(path, fields=None):
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream, strict=True)
        if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames):
            raise ValueError("CSV header is missing or duplicated")
        if fields is not None and set(reader.fieldnames) != set(fields):
            raise ValueError("CSV columns do not match the required schema")
        rows = list(reader)
        if any(None in row or any(v is None for v in row.values()) for row in rows):
            raise ValueError("Malformed or incomplete CSV row; do not send")
        return rows


def recipients(path, expected):
    rows = read_csv(path, ["email"])
    if len(rows) != expected or expected < 1:
        raise ValueError(f"Expected {expected} recipient rows, got {len(rows)}")
    emails = [row["email"].strip() for row in rows]
    if any(not valid_email(email) for email in emails):
        raise ValueError("Use one plain ASCII email address per row; no names or lists")
    if len({email.casefold() for email in emails}) != len(emails):
        raise ValueError("Duplicate recipient address; no files generated")
    return emails


def load_mapping(path):
    rows = read_csv(path, FIELDS)
    if not rows:
        raise ValueError("Unsupported mapping schema; email watermark format 2 is required")
    ids = [row["id"] for row in rows]
    emails = [row["recipient_email"] for row in rows]
    if (any(not re.fullmatch(r"[0-9]{3,}", n) for n in ids) or len(set(ids)) != len(ids)
            or any(not valid_email(e) for e in emails)
            or len({e.casefold() for e in emails}) != len(emails)):
        raise ValueError("Invalid or duplicated mapping IDs/emails")
    return rows


def load_batch(batch):
    run = json.loads((batch / "run.json").read_text(encoding="utf-8"))
    rows = load_mapping(batch / "mapping.csv")
    if run.get("format_version") != 2 or run["status"] != "GENERATED" or len(rows) != run["expected"]:
        raise ValueError("Batch incomplete, failed, or obsolete; do not distribute")
    for row in rows:
        name = row["original_name"]
        if (not name or Path(name).name != name or "/" in name or "\\" in name
                or name != run["original_name"] or row["pdf_file"] != f"pdfs/{row['id']}/{name}"
                or row["batch_id"] != run["batch_id"] or row["source_sha256"] != run["source_sha256"]
                or int(row["page_count"]) != run["page_count"] or row["generation_result"] != "GENERATED"):
            raise ValueError("Mapping inconsistency or changed original filename")
    expected = {row["pdf_file"] for row in rows}
    actual = {p.relative_to(batch).as_posix() for p in (batch / "pdfs").rglob("*") if p.is_file()}
    if actual != expected:
        raise ValueError("Extra or missing files in pdfs directory")
    return run, rows
