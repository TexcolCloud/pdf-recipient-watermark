"""Run a 70-recipient offline end-to-end check using synthetic documents."""
import argparse
import csv
import io
import json
import os
import subprocess
import sys
import tempfile
import shutil
import smtplib
import ssl
from types import SimpleNamespace
from unittest.mock import patch
from email import policy
from email.parser import BytesParser
from pathlib import Path

from PIL import Image, ImageDraw
from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject, NumberObject, RectangleObject
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader

import batch as records
import watermark
from contextlib import redirect_stdout
import delivery


def make_sample(path):
    content = io.BytesIO()
    c = canvas.Canvas(content)
    sizes = [(595, 842)] * 4 + [(842, 595), (595, 842)]
    for index, (w, h) in enumerate(sizes):
        c.setPageSize((w, h))
        if index == 5:
            image = Image.new("RGB", (850, 1200), "white")
            draw = ImageDraw.Draw(image)
            draw.text((60, 80), "SCANNED PAGE - SYNTHETIC DEMONSTRATION", fill="black", font_size=26)
            for line in range(20):
                draw.text((60, 160 + line * 40), f"Sample document line {line + 1:02}. No real recipient data.",
                             fill="#444444", font_size=20)
            c.drawImage(ImageReader(image), 0, 0, width=w, height=h)
        else:
            c.setFillColorRGB(0.08, 0.15, 0.23)
            c.setFont("Helvetica-Bold", 22)
            c.drawString(60, h - 90, "PDF distribution demo")
            c.setFont("Helvetica", 11)
            c.drawString(60, h - 116, "Synthetic source. No email addresses are embedded.")
            c.setFont("Helvetica-Bold", 12)
            c.drawString(60, h - 166, f"Page {index + 1} / coordinate and rotation check")
            c.setFont("Helvetica", 11)
            for line in range(13):
                c.drawString(60, h - 200 - line * 24,
                             f"{line + 1:02}   Example body text for checking watermark readability.")
            c.setFillColorRGB(0.93, 0.95, 0.97)
            c.rect(60, 74, w - 120, 90, fill=1, stroke=0)
            c.setFillColorRGB(0.16, 0.22, 0.28)
            c.drawString(76, 130, "Preserve the original document and separate mapping table.")
        c.showPage()
    c.save()
    reader = PdfReader(io.BytesIO(content.getvalue()))
    writer = PdfWriter(clone_from=reader)
    # Nonzero media/crop origins plus four page rotations.
    for index, page in enumerate(writer.pages[:4]):
        page.mediabox = RectangleObject([-20, -30, 600, 850])
        page.cropbox = RectangleObject([30, 40, 565, 812])
        page[NameObject("/Rotate")] = NumberObject(index * 90)
    with Path(path).open("wb") as stream:
        writer.write(stream)


def run_checks(root):
    root.mkdir(parents=True, exist_ok=True)
    source = root / "培训 资料（内部）.PDF"
    make_sample(source)
    original_source = source.read_bytes()
    long_email = "l" * 64 + "@" + "d" * 63 + "." + "e" * 63 + "." + "f" * 53 + ".invalid"
    assert len(long_email) == 254 and records.valid_email(long_email)
    tiny = PdfWriter()
    tiny.add_blank_page(width=72, height=72)
    tiny_stream = io.BytesIO()
    tiny.write(tiny_stream)
    watermark.stamp(tiny_stream.getvalue(), root / "tiny.pdf", long_email)
    watermark.verify_pdf(root / "tiny.pdf", long_email, 1)
    emails = [f"person{i:03}@example.invalid" for i in range(1, 71)]
    emails[1] = long_email
    csv_path = root / "recipients.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as stream:
        table = csv.writer(stream)
        table.writerow(["email"])
        table.writerows([email] for email in emails)
    script = Path(__file__).with_name("watermark.py")

    def cli(*args, ok=True, name=script):
        completed = subprocess.run([sys.executable, "-B", str(name), *map(str, args)],
                                   capture_output=True, text=True, encoding="utf-8",
                                   env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        if (completed.returncode == 0) != ok:
            raise AssertionError(completed.stdout + completed.stderr)
        return completed

    batch = root / "batch"
    cli("generate", "--source", source, "--recipients", csv_path, "--out", batch)
    cli("verify", "--batch", batch)
    rows = records.load_mapping(batch / "mapping.csv")
    run = json.loads((batch / "run.json").read_text(encoding="utf-8"))
    assert len(rows) == 70 and len({row["sha256"] for row in rows}) == 70
    assert run["format_version"] == 2 and run["original_name"] == source.name
    assert source.read_bytes() == original_source
    assert delivery.latest_events(batch, rows) == {}
    source_reader = PdfReader(source)
    for row in rows:
        file = batch / row["pdf_file"]
        assert file.name == source.name
        assert file.parent.name == row["id"]
        reader = PdfReader(file)
        for original, page in zip(source_reader.pages, reader.pages):
            assert page.rotation == original.rotation
            assert list(page.cropbox) == list(original.cropbox)
            assert list(page.mediabox) == list(original.mediabox)
            visible, hidden, _ = watermark.page_signals(page, reader)
            assert visible == [row["recipient_email"]] * 3
            assert hidden == [row["recipient_email"]]
    first, second = [batch / row["pdf_file"] for row in rows[:2]]
    cli("generate", "--source", source, "--recipients", csv_path, "--out", batch, ok=False)
    duplicate = root / "duplicate.csv"
    duplicate.write_text("email\na@example.invalid\nA@example.invalid\n", encoding="utf-8")
    cli("generate", "--source", source, "--recipients", duplicate, "--expected", 2,
        "--out", root / "bad-duplicate", ok=False)
    cli("generate", "--source", source, "--recipients", csv_path, "--expected", 69,
        "--out", root / "bad-count", ok=False)
    encrypted = PdfWriter(clone_from=PdfReader(source))
    encrypted.encrypt("test-only")
    encrypted.write(root / "encrypted.pdf")
    cli("generate", "--source", root / "encrypted.pdf", "--recipients", csv_path,
        "--out", root / "bad-encrypted", ok=False)
    cli("generate", "--source", first, "--recipients", csv_path, "--out", root / "bad-marked", ok=False)
    first_bytes = first.read_bytes()
    first.write_bytes(second.read_bytes())
    cli("verify", "--batch", batch, ok=False)
    # Even a recomputed hash cannot hide the recipient mismatch.
    altered = dict(rows[0], sha256=records.digest(first.read_bytes()))
    try:
        watermark.verified_attachment(batch, altered, set(emails))
        raise AssertionError("Swapped recipient accepted")
    except ValueError:
        pass
    first.write_bytes(first_bytes + b"\n")
    cli("verify", "--batch", batch, ok=False)
    first.write_bytes(first_bytes)
    cli("trace", batch / "pdfs", "--mapping", batch / "mapping.csv", "--out", root / "trace-all.csv")
    assert all(row["result"] == "EXACT_FILE" for row in records.read_csv(root / "trace-all.csv"))

    leaks = root / "leaks"
    leaks.mkdir()
    excerpt = PdfWriter()
    excerpt.add_page(PdfReader(first).pages[-1])
    excerpt.write(leaks / "excerpt.pdf")
    mixed = PdfWriter()
    mixed.add_page(PdfReader(first).pages[0])
    mixed.add_page(PdfReader(second).pages[0])
    mixed.write(leaks / "mixed.pdf")
    invisible = PdfWriter(clone_from=PdfReader(first))
    invisible.add_metadata({"/RecipientEmail": ""})
    for page in invisible.pages:
        del page["/RecipientEmail"]
        contents = page.get_contents()
        retained, depth = [], 0
        for operands, op in contents.operations:
            if op in (b"BMC", b"BDC") and (depth or str(operands[0]) == "/RecipientVisible"):
                depth += 1
                continue
            if depth:
                if op == b"EMC":
                    depth -= 1
                continue
            retained.append((operands, op))
        contents.operations = retained
        page.replace_contents(contents)
    invisible.write(leaks / "invisible-only.pdf")
    shutil.copyfile(source, leaks / "original.pdf")
    cli("trace", leaks, "--mapping", batch / "mapping.csv", "--out", root / "trace-leaks.csv")
    traced = {Path(row["file"]).name: row for row in records.read_csv(root / "trace-leaks.csv")}
    assert traced["excerpt.pdf"]["result"] == "MARKER_CANDIDATE"
    assert traced["mixed.pdf"]["result"] == "CONFLICT_REVIEW"
    assert traced["invisible-only.pdf"]["result"] == "MARKER_CANDIDATE"
    assert traced["invisible-only.pdf"]["evidence"] == "INVISIBLE_EMAIL_GROUP"
    assert traced["original.pdf"]["result"] == "NO_MATCH"

    subject, body = "资料分发测试", "请查收附件。本次为合成材料测试。"
    subject_path, body_path = root / "subject.txt", root / "body.txt"
    subject_path.write_text(subject, encoding="utf-8")
    body_path.write_text(body, encoding="utf-8")
    delivery_script = script.with_name("delivery.py")
    preview = root / "preview"
    cli("preview", "--batch", batch, "--from", "synthetic-sender@qq.com",
        "--subject-file", subject_path, "--body-file", body_path, "--out", preview, name=delivery_script)
    assert len(list(preview.glob("*.eml"))) == 70
    plan = records.read_csv(preview / "preview.csv")
    assert len(plan) == 70 and all(set(r) == set(records.BINDING + ["eml_file", "message_sha256"]) for r in plan)
    assert delivery.latest_events(batch, rows) == {}
    rejected = cli("send-qq", "--batch", batch, "--from", "synthetic-sender@qq.com",
                   "--subject-file", subject_path, "--body-file", body_path,
                   name=delivery_script, ok=False)
    assert "Placeholder .invalid recipients cannot be sent" in rejected.stderr

    by_email = {row["recipient_email"]: row for row in rows}
    class FakeSMTP:
        esmtp_features = {"size": "50000000"}
        def __init__(self, failure=None):
            self.calls = []
            self.failure = failure
        def sendmail(self, sender, to, raw):
            self.calls.append((sender, to, raw))
            assert len(to) == 1
            msg = BytesParser(policy=policy.default).parsebytes(raw)
            row = by_email.get(to[0])
            assert [a.addr_spec for a in msg["To"].addresses] == to
            assert not msg.get_all("Cc") and not msg.get_all("Bcc")
            attachments = list(msg.iter_attachments())
            assert len(attachments) == 1 and attachments[0].get_filename() == source.name
            if row:
                assert records.digest(attachments[0].get_payload(decode=True)) == row["sha256"]
            if self.failure:
                raise self.failure
            return {}

    transport = FakeSMTP()
    assert delivery.send_batch(batch, "synthetic-sender@qq.com", subject, body, transport, interval=0) == 70
    latest = delivery.latest_events(batch, rows)
    assert len(transport.calls) == 70
    assert all(e["state"] == "ACCEPTED" and e["submitted_at"] and not e["sent_at"] for e in latest.values())
    assert delivery.send_batch(batch, "synthetic-sender@qq.com", subject, body, transport, interval=0) == 0
    assert len(transport.calls) == 70

    def scenario(name):
        folder = root / name
        folder.mkdir()
        cloned = [dict(r, batch_id=name) for r in rows[:2]]
        for row in cloned:
            destination = folder / row["pdf_file"]
            destination.parent.mkdir(parents=True)
            shutil.copyfile(batch / row["pdf_file"], destination)
        (folder / "run.json").write_text(json.dumps(dict(run, batch_id=name, expected=2)), encoding="utf-8")
        for filename, fields, items in (("mapping.csv", records.FIELDS, cloned),
                                        ("send_log.csv", records.SEND_FIELDS, [])):
            with (folder / filename).open("w", encoding="utf-8-sig", newline="") as stream:
                table = csv.DictWriter(stream, fieldnames=fields)
                table.writeheader()
                table.writerows(items)
        return folder, cloned

    for name, failure, expected in [
        ("timeout", TimeoutError("simulated DATA reply timeout"), "UNKNOWN"),
        ("refused", smtplib.SMTPDataError(450, b"simulated rate limit"), "FAILED"),
        ("crashed", KeyboardInterrupt(), "IN_PROGRESS"),
    ]:
        folder, items = scenario(name)
        fake = FakeSMTP(failure)
        try:
            delivery.send_batch(folder, "synthetic-sender@qq.com", subject, body, fake, interval=0)
            raise AssertionError("Expected simulated interruption")
        except (ValueError, KeyboardInterrupt):
            pass
        assert delivery.latest_events(folder, items)["001"]["state"] == expected
        next_fake = FakeSMTP()
        try:
            delivery.send_batch(folder, "synthetic-sender@qq.com", subject, body, next_fake, interval=0)
            raise AssertionError("Unreviewed retry accepted")
        except ValueError:
            pass
        assert not next_fake.calls
        if name == "crashed":
            assert delivery.latest_events(folder, items)["001"]["state"] == "UNKNOWN"
            delivery.resolve(SimpleNamespace(batch=folder, id="001", outcome="NOT_SENT",
                                              reason="SYNTHETIC REVIEW: confirmed no submission", sent_at=None))
            assert delivery.send_batch(folder, "synthetic-sender@qq.com", subject, body,
                                       FakeSMTP(), interval=0) == 2
        if name == "timeout":
            delivery.resolve(SimpleNamespace(batch=folder, id="001", outcome="SENT",
                                              reason="SYNTHETIC service record, not a real email", sent_at=records.now()))
            only_pending = FakeSMTP()
            assert delivery.send_batch(folder, "synthetic-sender@qq.com", subject, body,
                                       only_pending, interval=0) == 1
            assert only_pending.calls[0][1] == [items[1]["recipient_email"]]

    # Exercise the actual QQ wrapper with all network methods replaced.
    qq_source = root / "QQ 接入测试.PDF"
    shutil.copyfile(source, qq_source)
    qq_list = root / "qq-test.csv"
    qq_list.write_text("email\nreceiver@example.com\n", encoding="utf-8")
    qq_batch = root / "qq-wrapper"
    cli("generate", "--source", qq_source, "--recipients", qq_list,
        "--expected", 1, "--out", qq_batch)
    class FakeQQ:
        esmtp_features = {}
        password = None
        closed = False
        def login(self, username, secret):
            assert username == "synthetic-sender@qq.com" and secret == "test-secret-12345"
        def sendmail(self, sender, to, raw):
            msg = BytesParser(policy=policy.default).parsebytes(raw)
            assert to == ["receiver@example.com"] and len(list(msg.iter_attachments())) == 1
            assert list(msg.iter_attachments())[0].get_filename() == qq_source.name
            return {}
        def quit(self):
            raise OSError("simulated post-accept QUIT failure")
        def close(self):
            self.closed = True
    qq = FakeQQ()
    def factory(host, port, timeout, context):
        assert (host, port, timeout) == ("smtp.qq.com", 465, 60)
        assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
        return qq
    with patch.object(delivery.smtplib, "SMTP_SSL", factory), patch.object(delivery.getpass, "getpass",
                                                                        return_value="test-secret-12345"):
        delivery.send_qq(SimpleNamespace(batch=qq_batch, sender="synthetic-sender@qq.com",
                         subject_file=subject_path, body_file=body_path, interval=10, max_message_mb=20))
    assert qq.closed
    qq_rows = records.load_mapping(qq_batch / "mapping.csv")
    assert delivery.latest_events(qq_batch, qq_rows)["001"]["state"] == "ACCEPTED"
    with patch.object(delivery.smtplib, "SMTP_SSL", side_effect=AssertionError("Duplicate login")):
        delivery.send_qq(SimpleNamespace(batch=qq_batch, sender="synthetic-sender@qq.com",
                         subject_file=subject_path, body_file=body_path, interval=10, max_message_mb=20))
    assert "test-secret-12345" not in (qq_batch / "send_log.csv").read_text(encoding="utf-8-sig")



if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", type=Path, help="Retain synthetic artifacts in a new directory")
    args = parser.parse_args()
    if args.keep:
        if args.keep.exists():
            parser.error("--keep directory must not already exist")
        with redirect_stdout(io.StringIO()):
            run_checks(args.keep)
    else:
        with tempfile.TemporaryDirectory(prefix="pdf-email-check-") as temporary:
            with redirect_stdout(io.StringIO()):
                run_checks(Path(temporary))

    print("PASS: 70 x 6 pages; unchanged Chinese/space/uppercase basenames; 254-character email;")
    print("visible and hidden emails, crop/rotation/scan pages; hashes and recipient mismatch rejection;")
    print("batch/excerpt/mixed/invisible-only tracing; 70 isolated MIME previews and fake submissions;")
    print("ACCEPTED/SENT skip; refusal/timeout/crash recovery; manual review before retry; QQ TLS/login wrapper.")
    print("NO REAL NETWORK CONNECTIONS OR EMAILS WERE MADE.")
