"""Atomic, readable artifact writers used by all source modules."""

import csv
import gzip
import json
import os


def atomic(path, writer):
    tmp = path.with_name(path.name + ".tmp")
    writer(tmp)
    os.replace(tmp, path)


def save_json(path, value):
    atomic(path, lambda p: p.write_text(json.dumps(value, indent=2)))


def save_csv(path, fields, rows):
    def write(p):
        with p.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for r in rows:
                w.writerow(
                    {
                        k: (
                            json.dumps(r.get(k), ensure_ascii=False)
                            if isinstance(r.get(k), list)
                            else r.get(k)
                        )
                        for k in fields
                    }
                )

    atomic(path, write)


def save_gz(path, rows):
    def write(p):
        with gzip.open(p, "wt", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")

    atomic(path, write)
