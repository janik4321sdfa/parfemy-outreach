# -*- coding: utf-8 -*-
"""Sifrovani stavu: state.sqlite <-> state.enc (gzip + Fernet/AES, klic v env STATE_KEY).

  python statebox.py pack     state.sqlite -> state.enc
  python statebox.py unpack   state.enc    -> state.sqlite
"""
import gzip, os, sqlite3, sys, tempfile
from cryptography.fernet import Fernet

DB = os.environ.get("STATE_DB", "state.sqlite")
ENC = os.environ.get("STATE_ENC", "state.enc")


def key():
    k = os.environ.get("STATE_KEY", "").strip()
    if not k:
        sys.exit("Chybi STATE_KEY")
    return Fernet(k.encode())


def pack():
    # konzistentni snimek i kdyby DB nekdo zrovna drzel otevrenou
    fd, tmp = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    src, dst = sqlite3.connect(DB), sqlite3.connect(tmp)
    src.backup(dst)
    dst.execute("VACUUM")
    src.close()
    dst.close()
    raw = open(tmp, "rb").read()
    os.remove(tmp)
    blob = key().encrypt(gzip.compress(raw, 9))
    open(ENC + ".tmp", "wb").write(blob)
    os.replace(ENC + ".tmp", ENC)
    print(f"pack: {len(raw)} B -> {len(blob)} B")


def unpack():
    raw = gzip.decompress(key().decrypt(open(ENC, "rb").read()))
    open(DB + ".tmp", "wb").write(raw)
    os.replace(DB + ".tmp", DB)
    print(f"unpack: {len(raw)} B")


if __name__ == "__main__":
    {"pack": pack, "unpack": unpack}[sys.argv[1]]()
