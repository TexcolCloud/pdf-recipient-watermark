"""One recipient / one verified attachment: offline previews and QQ SMTP delivery."""
import argparse
import csv
import getpass
import json
import math
import os
import secrets
import smtplib
import ssl
import sys
import time
import warnings
from collections import Counter
from contextlib import contextmanager, nullcontext
from datetime import datetime
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import formatdate, make_msgid
from pathlib import Path

import batch as records
import watermark as wm

TRANSITIONS = {
    "NOT_SENT": {"IN_PROGRESS"},
    "IN_PROGRESS": {"ACCEPTED", "FAILED", "UNKNOWN"},
    "ACCEPTED": {"SENT", "NOT_SENT", "FAILED"},
    "FAILED": {"NOT_SENT", "SENT"},
    "UNKNOWN": {"NOT_SENT", "SENT", "FAILED"},
    "SENT": set(),
}


@contextmanager
def batch_lock(batch):
    """OS lock is released even after a crash; the small lock file is retained."""
    with (batch / ".delivery.lock").open("a+b") as stream:
        if not stream.tell():
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def validate_time(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Time must be ISO 8601 with an explicit timezone")


def latest_events(batch, rows):
    by_id = {row["id"]: row for row in rows}
    latest = {}
    for event in records.read_csv(batch / "send_log.csv", records.SEND_FIELDS):
        row = by_id.get(event["id"])
        if row is None or any(event[k] != row[k] for k in records.BINDING):
            raise ValueError("Log binding differs from mapping; do not send")
        previous = latest.get(event["id"], {}).get("state", "NOT_SENT")
        if event["state"] not in TRANSITIONS.get(previous, set()):
            raise ValueError(f"Invalid send state transition: {previous} -> {event['state']}")
        validate_time(event["recorded_at"])
        if event["submitted_at"]:
            validate_time(event["submitted_at"])
        if event["sent_at"]:
            validate_time(event["sent_at"])
        if event["state"] == "SENT" and not event["sent_at"]:
            raise ValueError("SENT requires a separately verified sent_at")
        latest[event["id"]] = event
    return latest


def append_event(batch, event):
    event = dict(event, recorded_at=records.now())
    with (batch / "send_log.csv").open("a", encoding="utf-8", newline="") as stream:
        csv.DictWriter(stream, fieldnames=records.SEND_FIELDS).writerow(event)
        stream.flush()
        os.fsync(stream.fileno())
    return event


def recover_inflight(batch, rows):
    latest = latest_events(batch, rows)
    for key, event in list(latest.items()):
        if event["state"] == "IN_PROGRESS":
            latest[key] = append_event(batch, dict(event, state="UNKNOWN",
                                                   error="Interrupted attempt; check QQ records before retrying"))
    return latest


def content_hash(sender, subject, body):
    return records.digest(json.dumps([sender, subject, body], ensure_ascii=False).encode("utf-8"))


def check_content(sender, subject, body, known):
    if not records.valid_email(sender) or not subject.strip() or any(c in subject for c in "\r\n"):
        raise ValueError("Use a plain sender email and a nonempty one-line subject")
    if {e.casefold() for e in records.EMAIL_RE.findall(subject + "\n" + body)} & {e.casefold() for e in known}:
        raise ValueError("Shared subject/body contains a recipient address; remove recipient lists")


def build_message(batch, row, sender, subject, body, known):
    # Exactly one file read; verify and attach this same byte buffer.
    data = wm.verified_attachment(batch, row, known)
    message = EmailMessage(policy=policy.SMTP)
    message["From"] = sender
    message["To"] = row["recipient_email"]
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=False, usegmt=True)
    message["Message-ID"] = make_msgid(domain=sender.rsplit("@", 1)[1])
    message.set_content(body)
    message.add_attachment(data, maintype="application", subtype="pdf", filename=row["original_name"])
    raw = message.as_bytes()
    # Inspect the actual serialized MIME, including decoded Unicode filename.
    decoded = BytesParser(policy=policy.default).parsebytes(raw)
    attachments = list(decoded.iter_attachments())
    addresses = [a.addr_spec for header in decoded.get_all("To", []) for a in header.addresses]
    if (addresses != [row["recipient_email"]] or decoded.get_all("Cc") or decoded.get_all("Bcc")
            or len(attachments) != 1 or attachments[0].get_filename() != row["original_name"]
            or attachments[0].get_payload(decode=True) != data):
        raise ValueError("Serialized MIME recipient/attachment validation failed")
    return raw, str(message["Message-ID"])


def blank_event(row):
    return dict.fromkeys(records.SEND_FIELDS, "") | {k: row[k] for k in records.BINDING}


def preview(args):
    rows = wm.verify_batch(args.batch)
    known = {row["recipient_email"] for row in rows}
    subject, body = read_content(args)
    check_content(args.sender, subject, body, known)
    args.out.mkdir(parents=True, exist_ok=False)
    with (args.out / "preview.csv").open("x", encoding="utf-8-sig", newline="") as stream:
        table = csv.DictWriter(stream, fieldnames=records.BINDING + ["eml_file", "message_sha256"])
        table.writeheader()
        for row in rows:
            raw, _ = build_message(args.batch, row, args.sender, subject, body, known)
            filename = f"{row['id']}.eml"
            (args.out / filename).write_bytes(raw)
            table.writerow({k: row[k] for k in records.BINDING} |
                           {"eml_file": filename, "message_sha256": records.digest(raw)})
    print(f"PREVIEW ONLY: {len(rows)} EML files; nothing sent; send_log unchanged.")


def send_batch(batch, sender, subject, body, smtp=None, interval=10, max_bytes=20 * 1024 * 1024):
    """One preflight and one send path for QQ and the offline test transport."""
    if not math.isfinite(interval) or interval < 0 or not math.isfinite(max_bytes) or max_bytes <= 0:
        raise ValueError("Invalid interval or message-size ceiling")
    with batch_lock(batch):
        rows = wm.verify_batch(batch)
        known = {row["recipient_email"] for row in rows}
        check_content(sender, subject, body, known)
        latest = recover_inflight(batch, rows)
        if any(e["state"] in {"FAILED", "UNKNOWN"} for e in latest.values()):
            raise ValueError("FAILED/UNKNOWN items require review before resuming")
        fingerprint = content_hash(sender, subject, body)
        if any(e["content_sha256"] != fingerprint for e in latest.values()):
            raise ValueError("Sender/subject/body differs from the earlier attempt")
        pending = [row for row in rows if latest.get(row["id"], {}).get("state", "NOT_SENT") == "NOT_SENT"]
        if not pending:
            print("All items already ACCEPTED/SENT; no login or further submission.")
            return 0
        if smtp is None:
            if sender.rsplit("@", 1)[1].casefold() != "qq.com" or interval < 3:
                raise ValueError("Use a qq.com sender and an interval of at least 3 seconds")
            if any(email.casefold().endswith(".invalid") for email in known):
                raise ValueError("Placeholder .invalid recipients cannot be sent")
        with (nullcontext(smtp) if smtp is not None else qq_connection(sender)) as client:
            advertised = client.esmtp_features.get("size", "")
            if advertised.isdigit():
                max_bytes = min(max_bytes, int(advertised))
            for index, row in enumerate(pending):
                # Preflight checks the whole batch; reread immediately before each
                # submission and attach exactly the byte buffer just verified.
                raw, message_id = build_message(batch, row, sender, subject, body, known)
                if len(raw) > max_bytes:
                    raise ValueError(f"Message {row['id']} exceeds the configured/server SIZE ceiling")
                if index:
                    time.sleep(interval)
                event = blank_event(row) | dict(state="IN_PROGRESS", sender_email=sender,
                        attempt_id=secrets.token_hex(12), content_sha256=fingerprint,
                        message_sha256=records.digest(raw), message_id=message_id)
                event = append_event(batch, event)
                try:
                    refused = client.sendmail(sender, [row["recipient_email"]], raw)
                    if refused:
                        raise smtplib.SMTPRecipientsRefused(refused)
                except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused,
                        smtplib.SMTPDataError, smtplib.SMTPNotSupportedError) as exc:
                    append_event(batch, dict(event, state="FAILED", error=str(exc)))
                    raise ValueError(f"SMTP rejected item {row['id']}; stopped without retry") from exc
                except Exception as exc:
                    append_event(batch, dict(event, state="UNKNOWN", error=f"{type(exc).__name__}: {exc}"))
                    raise ValueError(f"Unknown outcome for {row['id']}; review before retrying") from exc
                append_event(batch, dict(event, state="ACCEPTED", submitted_at=records.now()))
                print(f"{row['id']}: ACCEPTED (SMTP submission only; sent_at is empty)")
        return len(pending)


def read_content(args):
    return (args.subject_file.read_text(encoding="utf-8-sig").strip(),
            args.body_file.read_text(encoding="utf-8-sig"))


@contextmanager
def qq_connection(sender):
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        secret = getpass.getpass("QQ SMTP authorization code (hidden input; not saved): ")
    if not secret or any(c.isspace() for c in secret):
        raise ValueError("Authorization code must be nonempty and contain no whitespace")
    smtp = None
    try:
        smtp = smtplib.SMTP_SSL("smtp.qq.com", 465, timeout=60, context=ssl.create_default_context())
        smtp.login(sender, secret)
        secret = ""
        smtp.password = None
        yield smtp
    finally:
        secret = ""
        if smtp is not None:
            try:
                smtp.quit()
            except Exception:
                smtp.close()


def send_qq(args):
    subject, body = read_content(args)
    count = send_batch(args.batch, args.sender, subject, body,
                       interval=args.interval, max_bytes=args.max_message_mb * 1024 * 1024)
    print(f"Run complete: {count} accepted submissions; no delivery/read receipt claimed.")


def status(args):
    _, rows = records.load_batch(args.batch)
    with batch_lock(args.batch):
        latest = latest_events(args.batch, rows)
    events = [latest.get(row["id"], blank_event(row) | {"state": "NOT_SENT"}) for row in rows]
    print(json.dumps(dict(Counter(event["state"] for event in events)), indent=2))
    if args.out:
        with args.out.open("x", encoding="utf-8-sig", newline="") as stream:
            table = csv.DictWriter(stream, fieldnames=records.SEND_FIELDS)
            table.writeheader()
            table.writerows(events)


def recover(args):
    _, rows = records.load_batch(args.batch)
    with batch_lock(args.batch):
        latest = recover_inflight(args.batch, rows)
    print(json.dumps(dict(Counter(event["state"] for event in latest.values())), indent=2))
    print("Interrupted attempts are UNKNOWN; no messages submitted.")


def resolve(args):
    if not args.reason.strip():
        raise ValueError("A review reason is required")
    _, rows = records.load_batch(args.batch)
    with batch_lock(args.batch):
        latest = recover_inflight(args.batch, rows)
        event = latest.get(args.id)
        if event is None or event["state"] not in {"ACCEPTED", "FAILED", "UNKNOWN"}:
            raise ValueError("Only ACCEPTED/FAILED/UNKNOWN attempts can be manually resolved")
        if args.outcome not in TRANSITIONS[event["state"]]:
            raise ValueError("Resolution is not valid for this state")
        if args.outcome == "SENT":
            if not args.sent_at:
                raise ValueError("SENT requires --sent-at from a verified service record")
            validate_time(args.sent_at)
        elif args.sent_at:
            raise ValueError("Only SENT may set sent_at")
        append_event(args.batch, dict(event, state=args.outcome, sent_at=args.sent_at or "",
                                      error="MANUAL REVIEW: " + args.reason))
    print(f"{args.id}: {args.outcome}; no message submitted by this command")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name, action in (("preview", preview), ("send-qq", send_qq)):
        command = commands.add_parser(name)
        command.add_argument("--batch", type=Path, required=True)
        command.add_argument("--from", dest="sender", required=True)
        command.add_argument("--subject-file", type=Path, required=True)
        command.add_argument("--body-file", type=Path, required=True)
        if name == "preview":
            command.add_argument("--out", type=Path, required=True)
        else:
            command.add_argument("--interval", type=float, default=10)
            command.add_argument("--max-message-mb", type=int, default=20)
        command.set_defaults(action=action)
    view = commands.add_parser("status")
    view.add_argument("--batch", type=Path, required=True)
    view.add_argument("--out", type=Path)
    view.set_defaults(action=status)
    fix = commands.add_parser("recover")
    fix.add_argument("--batch", type=Path, required=True)
    fix.set_defaults(action=recover)
    review = commands.add_parser("resolve")
    review.add_argument("--batch", type=Path, required=True)
    review.add_argument("--id", required=True)
    review.add_argument("--outcome", choices=["NOT_SENT", "SENT", "FAILED"], required=True)
    review.add_argument("--reason", required=True)
    review.add_argument("--sent-at")
    review.set_defaults(action=resolve)
    args = parser.parse_args()
    try:
        args.action(args)
    except (Exception, KeyboardInterrupt) as exc:
        print(f"STOPPED: {type(exc).__name__}: {exc}. Inspect status before resuming.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
