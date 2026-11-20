"""
Back up the control database and every account's archive database, ENCRYPTED, into one dated folder - and check an existing backup can still be read.

Why this exists
    The archive is the only copy of messages that were deleted on Telegram, so losing the database loses exactly the thing TeleVault is for.
    A plain `pg_dump` file is also the easiest way for private conversations to leak (a forgotten copy on a USB stick, a synced cloud folder, a stolen laptop),
    so this script never writes a readable dump to disk: pg_dump's output is piped straight into `age` (https://age-encryption.org) and only the encrypted result is stored.

Why `age` and not the Fernet key the app already uses
    age is PUBLIC-key encryption.
    This script (and so the server) holds only the PUBLIC key ("age1..."): it can encrypt, but it can never decrypt.
    The matching private key lives somewhere else (your password manager, an offline drive),
    so someone who breaks into the server cannot read the backups sitting next to the database.
    Reusing FERNET_KEY would tie both to one secret: leak it and backups AND stored Telegram sessions fall together.
    Never put the private key ("AGE-SECRET-KEY-...") on the machine that runs this script - the script refuses to run if it is handed one.

One-time setup (on a machine you trust, NOT the server)
    age-keygen -o televault-backup-key.txt      # prints the public key "age1..."; keep the FILE safe and offline - without it no backup can ever be opened
    # then give the server only the public key, in .env or on the command line:
    TELEVAULT_BACKUP_RECIPIENT=age1...

Usage (from the repo root, with the same .env the API server uses; the virtualenv active; `age` and PostgreSQL's `pg_dump` installed)
    python scripts/backup.py                                     # one new folder backups/<UTC timestamp>/, oldest ones beyond --keep are removed
    python scripts/backup.py --out /mnt/other-disk/televault     # somewhere else (a backup on the SAME disk as the database does not survive that disk dying)
    python scripts/backup.py --docker-service postgres           # run pg_dump inside the compose service (no PostgreSQL client needed on the host)
    python scripts/backup.py --verify backups/20261120T020000Z --identity televault-backup-key.txt
                                                                  # on the machine that has the private key: checks every file against MANIFEST.sha256,
                                                                  # decrypts it and makes sure pg_restore can read it.
                                                                  # Run this now and then - an unchecked backup is a hope.

What a backup folder holds
    control.dump.age           the control database (accounts, settings; Telegram sessions and archive passwords inside are ALSO Fernet-encrypted)
    archive_<database>.dump.age one per account archive (the messages)
    MANIFEST.sha256            SHA-256 of each file, in `sha256sum -c` format.
                               It is written LAST, so a folder without it is an unfinished or failed run.

What it does NOT back up
    - .env / FERNET_KEY.
      Without the original FERNET_KEY a restored control database cannot decrypt a single Telegram session (RUNBOOK.md, "Backups").
      Store it separately.
    - Downloaded media files (data/).
      Not needed yet: media archiving is still being built.

Safety notes
    - Database passwords go to pg_dump through the PGPASSWORD environment variable, never on a command line (where `ps` and shell history would show them),
      and are never printed.
    - Each file is written as <name>.partial with owner-only permissions and renamed only after BOTH pg_dump and age exited cleanly,
      so a half-written backup never looks like a good one.
    - Old folders are removed only after a run in which every database succeeded, and only folders that this script made (timestamp name + MANIFEST.sha256 inside).
      A failed run therefore never costs you an older good backup.
    - Exit status 0 = everything backed up, 1 = at least one database failed (the others are still saved, but that folder has no MANIFEST and is not counted as a backup).
"""

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# This script lives in scripts/, one level below the repo root - same sys.path fix as scripts/manage_admin.py, for the same reason.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy.engine import URL, make_url

import control_db
import db
from config import settings
from control_db import queries as cq
from utils.archive_login import archive_login_from_row

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT_DIR = REPO_ROOT / "backups"
DEFAULT_KEEP = 14
RECIPIENT_ENV = "TELEVAULT_BACKUP_RECIPIENT"
MANIFEST_NAME = "MANIFEST.sha256"

# Folder names this script creates; rotation touches nothing else.
_RUN_DIR_RE = re.compile(r"^\d{8}T\d{6}Z$")
# A database name becomes part of a file name.
# Names come from our own control database, but a path built from a stored string is a path-traversal bug waiting for the day that string is wrong,
# so anything outside this set is refused instead of sanitised.
_SAFE_DB_NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,63}$")

# How much of a tool's error output to show.
# Enough to see "database does not exist" or "age: error: ..."; short so a surprise never dumps pages.
_ERROR_LINE_LIMIT = 300


class BackupError(Exception):
    """One database could not be backed up.
    The message is safe to print (no URL, no password)."""


# ---------------------------------------------------------------------------
# Pure helpers (no database, no subprocess) - these carry the rules the tests pin down.
# ---------------------------------------------------------------------------

def run_dir_name(now: datetime) -> str:
    """The folder name for a run started at `now`: UTC, sortable as text (so 'oldest first' is just sorted()), and free of ':' which Windows file names cannot contain."""
    return now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def check_recipients(recipients: list[str]) -> list[str]:
    """
    Validate the age PUBLIC keys given by the operator and return them unchanged.

    Refuses an empty list (a backup nobody can read, or worse, one that silently falls back to plaintext) and refuses anything that is not an "age1..." public key.
    The case worth a loud message is someone pasting the PRIVATE key ("AGE-SECRET-KEY-...") here: the server would then hold the one secret that opens every backup,
    which defeats the reason for using public-key encryption at all.
    """
    cleaned = [r.strip() for r in recipients if r and r.strip()]
    if not cleaned:
        raise BackupError(f"No age recipient given. Pass --recipient age1... (or set {RECIPIENT_ENV}); see this script's docstring for the one-time setup.")
    for recipient in cleaned:
        if recipient.upper().startswith("AGE-SECRET-KEY-"):
            raise BackupError(
                "That is an age PRIVATE key. Keep it off this machine: generate a key pair elsewhere and give the server only the public key (starts with 'age1')."
            )
        if not recipient.startswith("age1"):
            raise BackupError(
                "Not an age public key (they start with 'age1'). SSH keys and passphrases are not accepted here, "
                "so this script can only ever hold a key that encrypts and cannot decrypt."
            )
    return cleaned


def backup_file_name(kind: str, db_name: str) -> str:
    """'control.dump.age' for the control database, 'archive_<database>.dump.age' for an archive.
    Raises BackupError for a database name that is not a plain identifier."""
    if not _SAFE_DB_NAME_RE.match(db_name):
        raise BackupError("the database name is not a plain identifier (letters, digits, underscore), so it cannot be used in a file name")
    return "control.dump.age" if kind == "control" else f"archive_{db_name}.dump.age"


def pg_dump_command(url: URL, docker_service: str | None) -> list[str]:
    """
    The pg_dump command line for `url`.
    Custom format (-Fc): compressed, and the only format pg_restore can restore selectively and `--list` can inspect.
    No password anywhere in it - it travels in PGPASSWORD (see _pg_env()).

    With `docker_service`, pg_dump runs INSIDE that compose service: it then talks to the server over the container's own socket,
    so host and port are left out (they would be the host's view of the container, which is wrong from inside it).
    `-e PGPASSWORD` with no value tells Docker to pass on our environment's value.
    """
    base = ["pg_dump", "-Fc", "-U", url.username or "postgres", "-d", url.database or "postgres"]
    if docker_service:
        return ["docker", "compose", "exec", "-T", "-e", "PGPASSWORD", docker_service, *base]
    if url.host:
        base += ["-h", url.host]
    if url.port:
        base += ["-p", str(url.port)]
    return base


def age_encrypt_command(recipients: list[str]) -> list[str]:
    """`age -r <key> -r <key> ...`: reads the dump on stdin, writes the encrypted file on stdout.
    Public keys only, so the command can never decrypt."""
    command = ["age"]
    for recipient in recipients:
        command += ["-r", recipient]
    return command


def manifest_text(hashes: dict[str, str]) -> str:
    """MANIFEST.sha256 content in the format `sha256sum -c` reads ('<hash>  <file>'), sorted by file name so two runs diff cleanly."""
    return "".join(f"{digest}  {name}\n" for name, digest in sorted(hashes.items()))


def parse_manifest(text: str) -> dict[str, str]:
    """Inverse of manifest_text().
    Raises BackupError on a line that is not '<64 hex>  <name>' - a damaged manifest must not be half-trusted."""
    result: dict[str, str] = {}
    for line in text.splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([^/\\]+)", line)
        if not match:
            raise BackupError(f"{MANIFEST_NAME} is damaged (unreadable line)")
        result[match.group(2)] = match.group(1)
    return result


def runs_to_delete(existing: list[str], complete: set[str], keep: int) -> list[str]:
    """
    Which folders (names) rotation should remove.

    Considers only names that look like one of our runs AND are in `complete` (have a MANIFEST) - an unfinished folder might be a run that is still going,
    and a folder this script did not make is never ours to delete.
    Of those, the newest `keep` survive.
    keep < 1 is rejected upstream.
    """
    ours = sorted(name for name in existing if _RUN_DIR_RE.match(name) and name in complete)
    return ours[: max(0, len(ours) - keep)]


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Running the tools
# ---------------------------------------------------------------------------

def _pg_env(url: URL) -> dict[str, str]:
    """Our environment plus PGPASSWORD for this one child process.
    The password is handed over the environment so it never appears in `ps` output or shell history."""
    env = dict(os.environ)
    if url.password:
        env["PGPASSWORD"] = url.password
    else:
        env.pop("PGPASSWORD", None)  # do not let a stale value from the shell be tried against the wrong database
    return env


def _last_error_line(stderr_file) -> str:
    """The last non-empty line a tool wrote to stderr, shortened - usually the actual reason ('database "x" does not exist')."""
    stderr_file.seek(0)
    lines = [line.strip() for line in stderr_file.read().decode("utf-8", "replace").splitlines() if line.strip()]
    return (lines[-1] if lines else "no error message")[:_ERROR_LINE_LIMIT]


def dump_encrypted(url: URL, destination: Path, recipients: list[str], docker_service: str | None) -> None:
    """
    pg_dump `url` -> age -> `destination`, without the readable dump ever touching the disk.

    The two programs are connected by a pipe: pg_dump's stdout is age's stdin, age's stdout is the file.
    Written to '<destination>.partial' with 0600 permissions first;
    renamed into place only if BOTH exited with status 0 and the file is not empty.
    Raises BackupError otherwise, leaving no partial file behind.
    """
    partial = destination.with_name(destination.name + ".partial")
    fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # owner-only from the first byte, not chmod'ed afterwards
    try:
        with os.fdopen(fd, "wb") as out, tempfile.TemporaryFile() as dump_err, tempfile.TemporaryFile() as age_err:
            try:
                dump = subprocess.Popen(pg_dump_command(url, docker_service), stdout=subprocess.PIPE, stderr=dump_err, env=_pg_env(url))
            except FileNotFoundError as exc:
                raise BackupError(f"'{exc.filename}' is not installed or not on PATH") from exc
            try:
                encrypt = subprocess.Popen(age_encrypt_command(recipients), stdin=dump.stdout, stdout=out, stderr=age_err)
            except FileNotFoundError as exc:
                dump.kill()
                dump.wait()
                raise BackupError("'age' is not installed or not on PATH (https://age-encryption.org)") from exc
            finally:
                # Let pg_dump see SIGPIPE if age dies early, instead of this process keeping the pipe open and pg_dump blocked forever.
                if dump.stdout:
                    dump.stdout.close()
            encrypt_status = encrypt.wait()
            dump_status = dump.wait()
            if dump_status != 0:
                raise BackupError(f"pg_dump failed: {_last_error_line(dump_err)}")
            if encrypt_status != 0:
                raise BackupError(f"age failed: {_last_error_line(age_err)}")
        if partial.stat().st_size == 0:
            raise BackupError("the encrypted file came out empty")
        os.replace(partial, destination)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise


def _database_targets() -> list[tuple[str, str, URL]]:
    """
    [(file name, label for messages, connection URL)] for the control database and every archive.

    An archive with its own Postgres login (db/provisioning.py) is dumped AS that login - the least privilege that can read it;
    an older one that has none yet uses the server's login, exactly like the rest of the app.
    A login that cannot be decrypted (lost FERNET_KEY) is reported for that archive alone and never silently swapped for the server's login
    (utils/archive_login.py explains why).
    """
    control_url = make_url(settings.control_database_url)
    targets: list[tuple[str, str, URL]] = [(backup_file_name("control", control_url.database or ""), "control database", control_url)]
    control_db.init_control_db(settings.control_database_url)
    db.init_server(settings.control_database_url)
    with control_db.get_connection() as conn:
        archives = cq.list_archive_databases(conn)
        login_rows = {a["id"]: cq.get_archive_login_row(conn, a["id"]) or {} for a in archives}
    for archive in archives:
        label = f"{archive['username']} (account {archive['id']}, database {archive['archive_db_ref']})"
        try:
            name = backup_file_name("archive", archive["archive_db_ref"])
            url = db.server_database_url(archive["archive_db_ref"], archive_login_from_row(login_rows[archive["id"]]))
        except Exception as exc:  # noqa: BLE001 - reported per archive, the others still run
            targets.append(("", label + " - " + _short(exc), control_url))  # empty file name = "could not even be prepared"; handled in run_backup()
            continue
        targets.append((name, label, url))
    return targets


def _short(exc: BaseException) -> str:
    first_line = (str(exc).strip().splitlines() or [""])[0]
    return f"{type(exc).__name__}: {first_line}"[:_ERROR_LINE_LIMIT]


def run_backup(out_dir: Path, recipients: list[str], keep: int, docker_service: str | None) -> int:
    """Make one new run folder under `out_dir`.
    Returns the exit status (0 all good, 1 something failed)."""
    now = datetime.now(timezone.utc)
    run_dir = out_dir / run_dir_name(now)
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        run_dir.mkdir(mode=0o700)  # exist_ok=False on purpose: never write into (and mix with) a folder that already exists
    except FileExistsError:
        print(f"ERROR: {run_dir} already exists (two runs started within the same second?). Nothing was changed; try again in a moment.")
        return 1
    try:
        targets = _database_targets()
    finally:
        db.close_tenant_engines()
        db.close_server()
        control_db.close_db()

    hashes: dict[str, str] = {}
    failed = 0
    for file_name, label, url in targets:
        if not file_name:
            failed += 1
            print(f"  FAILED   {label}")
            continue
        try:
            dump_encrypted(url, run_dir / file_name, recipients, docker_service)
            hashes[file_name] = sha256_of(run_dir / file_name)
            print(f"  ok       {label}: {file_name} ({(run_dir / file_name).stat().st_size:,} bytes)")
        except BackupError as exc:
            failed += 1
            print(f"  FAILED   {label}: {exc}")

    if failed:
        print(f"{len(targets) - failed} of {len(targets)} database(s) saved in {run_dir}, but {failed} failed.")
        print("That folder has no MANIFEST and is NOT counted as a backup; older backups were left untouched. Fix the cause and run again.")
        return 1

    (run_dir / MANIFEST_NAME).write_text(manifest_text(hashes), encoding="utf-8", newline="\n")
    print(f"Backup complete: {run_dir}")
    _rotate(out_dir, keep)
    return 0


def _rotate(out_dir: Path, keep: int) -> None:
    existing = [p.name for p in out_dir.iterdir() if p.is_dir()]
    complete = {name for name in existing if (out_dir / name / MANIFEST_NAME).is_file()}
    for name in runs_to_delete(existing, complete, keep):
        shutil.rmtree(out_dir / name)
        print(f"  removed old backup {name} (keeping the newest {keep})")


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------

def verify_backup(run_dir: Path, identity: Path) -> int:
    """
    Check a finished backup folder end to end, WITHOUT restoring anything: every file must match MANIFEST.sha256, decrypt with `identity`,
    and be a dump pg_restore can read (`pg_restore --list` parses the whole table of contents).
    Run it where the private key is, not on the server.
    Returns the exit status: 0 if every file passed.
    """
    manifest_path = run_dir / MANIFEST_NAME
    if not manifest_path.is_file():
        print(f"{run_dir} has no {MANIFEST_NAME}: an unfinished or failed run, not a usable backup.")
        return 1
    try:
        expected = parse_manifest(manifest_path.read_text(encoding="utf-8"))
    except BackupError as exc:
        print(str(exc))
        return 1
    on_disk = {p.name for p in run_dir.glob("*.age")}
    problems = 0
    for name in sorted(on_disk - set(expected)):
        problems += 1
        print(f"  UNLISTED {name}: not in the manifest")
    for name, digest in sorted(expected.items()):
        path = run_dir / name
        if not path.is_file():
            problems += 1
            print(f"  MISSING  {name}")
        elif sha256_of(path) != digest:
            problems += 1
            print(f"  CORRUPT  {name}: checksum differs from the manifest")
        else:
            try:
                _check_readable(path, identity)
                print(f"  ok       {name}")
            except BackupError as exc:
                problems += 1
                print(f"  FAILED   {name}: {exc}")
    print("Backup verified." if not problems else f"{problems} problem(s) found.")
    return 1 if problems else 0


def _check_readable(path: Path, identity: Path) -> None:
    """age -d -i <identity> <file> | pg_restore --list > nowhere.
    Nothing is written to disk and no database is touched."""
    with tempfile.TemporaryFile() as age_err, tempfile.TemporaryFile() as restore_err, open(path, "rb") as source:
        try:
            decrypt = subprocess.Popen(["age", "-d", "-i", str(identity)], stdin=source, stdout=subprocess.PIPE, stderr=age_err)
            restore = subprocess.Popen(["pg_restore", "--list"], stdin=decrypt.stdout, stdout=subprocess.DEVNULL, stderr=restore_err)
        except FileNotFoundError as exc:
            raise BackupError(f"'{exc.filename}' is not installed or not on PATH") from exc
        if decrypt.stdout:
            decrypt.stdout.close()
        restore_status = restore.wait()
        decrypt_status = decrypt.wait()
        if decrypt_status != 0:
            raise BackupError(f"could not decrypt: {_last_error_line(age_err)}")
        if restore_status != 0:
            raise BackupError(f"pg_restore cannot read it: {_last_error_line(restore_err)}")


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT_DIR, help=f"Folder that holds the dated backup folders (default: {DEFAULT_OUT_DIR}).")
    parser.add_argument("--recipient", action="append", default=[], metavar="AGE_PUBLIC_KEY",
                        help=f"age public key to encrypt to; repeat for several (or set {RECIPIENT_ENV}, comma-separated).")
    parser.add_argument("--keep", type=int, default=DEFAULT_KEEP, help=f"How many complete backups to keep (default {DEFAULT_KEEP}); older ones are removed after a fully successful run.")
    parser.add_argument("--docker-service", metavar="NAME", help="Run pg_dump inside this docker compose service (e.g. 'postgres') instead of on the host.")
    parser.add_argument("--verify", type=Path, metavar="RUN_DIR", help="Check an existing backup folder instead of making one (needs --identity).")
    parser.add_argument("--identity", type=Path, metavar="KEY_FILE", help="With --verify: the age PRIVATE key file. Use it on the machine that keeps the key, not on the server.")
    args = parser.parse_args(argv)

    if args.verify:
        if not args.identity:
            parser.error("--verify needs --identity (the file made by age-keygen)")
        return verify_backup(args.verify, args.identity)

    if args.keep < 1:
        parser.error("--keep must be at least 1 (0 would delete the backup this very run just made)")
    env_recipients = [r for r in os.environ.get(RECIPIENT_ENV, "").split(",") if r.strip()]
    try:
        recipients = check_recipients(args.recipient + env_recipients)
    except BackupError as exc:
        print(f"ERROR: {exc}")
        return 1
    return run_backup(args.out, recipients, args.keep, args.docker_service)


if __name__ == "__main__":
    sys.exit(main())