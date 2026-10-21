"""Offline per-recipient email watermarks with unchanged PDF basenames."""
import argparse
import csv
import io
import json
import math
import os
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

from pypdf import PdfReader, PdfWriter, Transformation
from pypdf.generic import ContentStream, NameObject, TextStringObject
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfgen import canvas

import batch as records

DEFAULT_OPACITY = 0.18


def preflight(reader):
    if reader.is_encrypted:
        raise ValueError("Encrypted input unsupported; use an authorized unencrypted copy")
    if not reader.pages:
        raise ValueError("Input has no pages")
    root = reader.trailer["/Root"]
    if root.get("/AcroForm") or root.get("/Perms") or root.get("/Collection") or reader.attachments:
        raise ValueError("Forms, signatures, portfolios or embedded files unsupported")
    if any((reader.metadata or {}).get(key) for key in ("/RecipientEmail", "/TraceID")):
        raise ValueError("Previously marked input; start from the unmarked original")
    for page in reader.pages:
        if page.get("/RecipientEmail") or page.get("/TraceID"):
            raise ValueError("Previously marked page; start from the unmarked original")
        if page.rotation % 90 or min(float(page.cropbox.width), float(page.cropbox.height)) < 72:
            raise ValueError("Unsupported rotation or CropBox smaller than 72 units")
        for ref in page.get("/Annots", []):
            if ref.get_object().get("/Subtype") in ("/Widget", "/FileAttachment"):
                raise ValueError("Widget or attached file found; use a plain PDF")


def wrap_email(email, width, size):
    """Display lines concatenate back to the exact original address."""
    lines, rest = [], email
    while pdfmetrics.stringWidth(rest, "Helvetica", size) > width:
        end = 1
        while end < len(rest) and pdfmetrics.stringWidth(rest[:end + 1], "Helvetica", size) <= width:
            end += 1
        preferred = max((i + 1 for i, char in enumerate(rest[:end]) if char in "@._-+"), default=0)
        end = preferred if preferred >= end * 0.55 else end
        lines.append(rest[:end])
        rest = rest[end:]
    return lines + [rest]


def draw_visible(c, lines, size, x, y, angle, opacity):
    c.addLiteral("/RecipientVisible BMC")
    c.saveState()
    c.setFillGray(82 / 255 if angle else 102 / 255)
    c.setFillAlpha(opacity)
    c.translate(x, y)
    c.rotate(angle)
    c.setFont("Helvetica", size)
    for index, line in enumerate(lines):
        baseline = ((len(lines) - 1) / 2 - index) * size * 1.25 - size * 0.3
        c.drawCentredString(0, baseline, line)
    c.restoreState()
    c.addLiteral("EMC")


def overlay(width, height, email, opacity):
    stream = io.BytesIO()
    c = canvas.Canvas(stream, pagesize=(width, height), pageCompression=1)
    size = 12
    lines = wrap_email(email, min(width * 0.78, height * 0.6), size)
    block_w = max(pdfmetrics.stringWidth(line, "Helvetica", size) for line in lines)
    block_h = len(lines) * size * 1.25
    factor = min(1, (width - 16) / (block_w * math.cos(math.pi / 6) + block_h * 0.5),
                 height * 0.28 / (block_w * 0.5 + block_h * math.cos(math.pi / 6)))
    for fraction in (0.35, 0.68):
        draw_visible(c, lines, size * factor, width / 2, height * fraction, 30, opacity)
    footer_size = min(8, (width - 16) / 10)
    footer = wrap_email(email, width - 24, footer_size)
    footer_size *= min(1, height * 0.2 / (len(footer) * footer_size * 1.25))
    draw_visible(c, footer, footer_size, width / 2,
                 15 + (len(footer) - 1) * footer_size * 0.625 + footer_size * 0.3, 0, 1)
    c.addLiteral("/RecipientInvisible BMC")
    c.saveState()
    text = c.beginText(8, height - 12)
    text.setFont("Helvetica", 6)
    text.setTextRenderMode(3)
    text.textOut(email)
    c.drawText(text)
    c.restoreState()
    c.addLiteral("EMC")
    c.showPage()
    c.save()
    return PdfReader(io.BytesIO(stream.getvalue())).pages[0]


def stamp(source_bytes, destination, email, opacity=DEFAULT_OPACITY):
    reader = PdfReader(io.BytesIO(source_bytes))
    writer = PdfWriter(clone_from=reader)
    writer.add_metadata({"/RecipientEmail": email})
    for page in writer.pages:
        box = page.cropbox
        x, y, w, h = map(float, (box.left, box.bottom, box.width, box.height))
        rotation = page.rotation % 360
        display_w, display_h = (h, w) if rotation in (90, 270) else (w, h)
        offset = {0: (x, y), 90: (x + w, y), 180: (x + w, y + h), 270: (x, y + h)}[rotation]
        page.merge_transformed_page(overlay(display_w, display_h, email, opacity),
                                    Transformation().rotate(rotation).translate(*offset))
        page[NameObject("/RecipientEmail")] = TextStringObject(email)
        page.compress_content_streams()
    with Path(destination).open("xb") as stream:
        writer.write(stream)
    writer.close()


def page_signals(page, reader):
    """Read complete multiline groups and their actual PDF rendering modes."""
    visible, hidden, text_emails = [], [], set()
    mode, graphics, marked = 0, [], []
    content = page.get_contents()
    if content is None:
        return visible, hidden, text_emails
    for args, op in ContentStream(content, reader).operations:
        if op == b"q":
            graphics.append(mode)
        elif op == b"Q":
            mode = graphics.pop() if graphics else 0
        elif op == b"Tr":
            mode = int(args[0])
        elif op in (b"BMC", b"BDC"):
            marked.append([str(args[0]), [], []])
        elif op == b"EMC" and marked:
            tag, strings, modes = marked.pop()
            value = "".join(strings)
            if tag == "/RecipientVisible":
                visible.append(value if modes and all(m in (0, 1, 2, 4, 5, 6) for m in modes) else "INVALID_RENDER_MODE")
            elif tag == "/RecipientInvisible":
                hidden.append(value if modes and all(m == 3 for m in modes) else "INVALID_RENDER_MODE")
        elif op in (b"Tj", b"TJ", b"'", b'"'):
            value = args[0] if op == b"TJ" else args[-1]
            strings = value if isinstance(value, list) else [value]
            raw = "".join(str(v) for v in strings if isinstance(v, str))
            text_emails.update(records.EMAIL_RE.findall(raw))
            if marked:
                marked[-1][1].append(raw)
                marked[-1][2].append(mode)
    return visible, hidden, text_emails


def verify_pdf(data_or_path, email, page_count, known_emails=None):
    reader = PdfReader(io.BytesIO(data_or_path) if isinstance(data_or_path, bytes) else data_or_path)
    if len(reader.pages) != int(page_count) or (reader.metadata or {}).get("/RecipientEmail") != email:
        raise ValueError("Page count or document RecipientEmail mismatch")
    for index, page in enumerate(reader.pages, 1):
        visible, hidden, text_emails = page_signals(page, reader)
        if page.get("/RecipientEmail") != email or visible != [email] * 3 or hidden != [email]:
            raise ValueError(f"Page {index}: visible/hidden/page email missing or mismatched")
        all_text = set(records.EMAIL_RE.findall(page.extract_text() or "")) | text_emails | set(visible + hidden)
        if known_emails and ({e.casefold() for e in all_text} & {e.casefold() for e in known_emails}) != {email.casefold()}:
            raise ValueError(f"Page {index}: another recipient's email found")


def generate(args):
    emails = records.recipients(args.recipients, args.expected)
    if not math.isfinite(args.opacity) or not 0 < args.opacity <= 1:
        raise ValueError("Opacity must be greater than 0 and at most 1")
    if args.source.suffix.lower() != ".pdf":
        raise ValueError("Source filename must end in .pdf or .PDF")
    source_bytes = args.source.read_bytes()
    reader = PdfReader(io.BytesIO(source_bytes))
    preflight(reader)
    args.out.mkdir(parents=True, exist_ok=False)
    (args.out / "pdfs").mkdir()
    run = {"batch_id": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3),
           "status": "GENERATING", "expected": len(emails), "source_sha256": records.digest(source_bytes),
           "original_name": args.source.name, "page_count": len(reader.pages), "started_at": records.now(),
           "opacity": args.opacity, "watermark_style": "EMAIL_TWO_DIAGONALS", "format_version": 2}
    run_path = args.out / "run.json"
    run_path.write_text(json.dumps(run, indent=2, ensure_ascii=False), encoding="utf-8")
    with (args.out / "send_log.csv").open("x", encoding="utf-8-sig", newline="") as log:
        csv.DictWriter(log, fieldnames=records.SEND_FIELDS).writeheader()
    failed = 0
    with (args.out / "mapping.csv").open("x", encoding="utf-8-sig", newline="") as stream:
        table = csv.DictWriter(stream, fieldnames=records.FIELDS)
        table.writeheader()
        for index, email in enumerate(emails, 1):
            number = f"{index:03}"
            rel = f"pdfs/{number}/{args.source.name}"
            final = args.out / rel
            final.parent.mkdir()
            part = final.parent / ".generating.part"
            row = dict.fromkeys(records.FIELDS, "")
            row.update(batch_id=run["batch_id"], id=number, recipient_email=email, pdf_file=rel,
                       original_name=args.source.name, source_sha256=run["source_sha256"],
                       page_count=len(reader.pages))
            try:
                stamp(source_bytes, part, email, args.opacity)
                data = part.read_bytes()
                verify_pdf(data, email, len(reader.pages), set(emails))
                os.replace(part, final)
                row.update(sha256=records.digest(data), generated_at=records.now(), generation_result="GENERATED")
            except Exception as exc:
                failed += 1
                part.unlink(missing_ok=True)
                row.update(generation_result="FAILED", error=str(exc))
            table.writerow(row)
            stream.flush()
            os.fsync(stream.fileno())
            print(f"[{index}/{len(emails)}] {number}: {row['generation_result']}")
    run.update(status="FAILED" if failed else "GENERATED", failed=failed, finished_at=records.now())
    run_path.write_text(json.dumps(run, indent=2, ensure_ascii=False), encoding="utf-8")
    if failed:
        raise ValueError(f"{failed} files failed; do not distribute this batch; inspect mapping.csv")
    print(f"Generated {len(emails)} same-named PDFs. Nothing sent. Run verify next.")


def verified_attachment(batch, row, known_emails):
    path = (batch / row["pdf_file"]).resolve()
    if not path.is_relative_to(batch.resolve()) or path.name != row["original_name"]:
        raise ValueError("Attachment escaped batch or changed filename")
    data = path.read_bytes()
    if records.digest(data) != row["sha256"]:
        raise ValueError(f"Hash mismatch for recipient {row['id']}")
    verify_pdf(data, row["recipient_email"], row["page_count"], known_emails)
    return data


def verify_batch(batch):
    _, rows = records.load_batch(batch)
    known = {row["recipient_email"] for row in rows}
    for row in rows:
        verified_attachment(batch, row, known)
    return rows


def trace(args):
    rows = records.load_mapping(args.mapping)
    by_email = {row["recipient_email"].casefold(): row for row in rows}
    hashes = {row["sha256"]: row["recipient_email"].casefold() for row in rows if row["sha256"]}
    paths = sorted(args.target.rglob("*")) if args.target.is_dir() else [args.target]
    paths = [p for p in paths if p.is_file() and p.suffix.lower() == ".pdf"]
    if not paths:
        raise ValueError("No PDFs found")
    with args.out.open("x", encoding="utf-8-sig", newline="") as stream:
        fields = ["file", "sha256", "result", "candidate_ids", "candidate_emails", "evidence", "error"]
        table = csv.DictWriter(stream, fieldnames=fields)
        table.writeheader()
        for path in paths:
            result = dict.fromkeys(fields, "")
            result["file"] = str(path.resolve())
            try:
                data = path.read_bytes()
                file_hash = records.digest(data)
                candidates, evidence, markers = set(), set(), set()
                if file_hash in hashes:
                    candidates.add(hashes[file_hash])
                    evidence.add("SHA256")
                reader = PdfReader(io.BytesIO(data))
                tag = (reader.metadata or {}).get("/RecipientEmail")
                if tag:
                    markers.add(str(tag).casefold())
                    evidence.add("DOCUMENT_EMAIL")
                for page in reader.pages:
                    if page.get("/RecipientEmail"):
                        markers.add(str(page["/RecipientEmail"]).casefold())
                        evidence.add("PAGE_EMAIL")
                    visible, hidden, text_emails = page_signals(page, reader)
                    if visible:
                        markers.update(e.casefold() for e in visible)
                        evidence.add("VISIBLE_EMAIL_GROUP")
                    if hidden:
                        markers.update(e.casefold() for e in hidden)
                        evidence.add("INVISIBLE_EMAIL_GROUP")
                    text_emails.update(records.EMAIL_RE.findall(page.extract_text() or ""))
                    candidates.update({e.casefold() for e in text_emails} & by_email.keys())
                candidates.update(markers & by_email.keys())
                if len(candidates) > 1 or bool(markers - by_email.keys()):
                    state = "CONFLICT_REVIEW"
                elif "SHA256" in evidence:
                    state = "EXACT_FILE"
                elif candidates and markers:
                    state = "MARKER_CANDIDATE"
                elif candidates:
                    state = "TEXT_CANDIDATE"
                else:
                    state = "NO_MATCH"
                ordered = sorted(candidates)
                result.update(sha256=file_hash, result=state,
                              candidate_ids=";".join(by_email[e]["id"] for e in ordered),
                              candidate_emails=";".join(by_email[e]["recipient_email"] for e in ordered),
                              evidence=";".join(sorted(evidence)))
            except Exception as exc:
                result.update(result="ERROR", error=str(exc))
            table.writerow(result)
    print(f"Trace report: {args.out} ({len(paths)} files). Candidates require human review.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    gen = commands.add_parser("generate")
    gen.add_argument("--source", type=Path, required=True)
    gen.add_argument("--recipients", type=Path, required=True)
    gen.add_argument("--out", type=Path, required=True)
    gen.add_argument("--expected", type=int, default=70)
    gen.add_argument("--opacity", type=float, default=DEFAULT_OPACITY)
    gen.set_defaults(action=generate)
    verify = commands.add_parser("verify")
    verify.add_argument("--batch", type=Path, required=True)
    verify.set_defaults(action=lambda a: print(f"PASS: {len(verify_batch(a.batch))} PDFs; names, emails and hashes verified"))
    find = commands.add_parser("trace")
    find.add_argument("target", type=Path)
    find.add_argument("--mapping", type=Path, required=True)
    find.add_argument("--out", type=Path, required=True)
    find.set_defaults(action=trace)
    args = parser.parse_args()
    try:
        args.action(args)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
