"""Official VisA archive fetch/extract and spot-diff 1cls split parsing for one category.

Train-side helpers return image identities only. Test labels are read solely by
``test_labels``, which the pipeline calls after a verified freeze.
"""

import csv
import hashlib
import json
import os
import pathlib
import posixpath
import tarfile
import urllib.request

HEADER = ["object", "split", "label", "image", "mask"]


def sha256_file(path, block=1 << 20):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(block), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _check_relpath(rel, category):
    if not rel or rel.startswith(("/", "\\")) or "\\" in rel or ":" in rel:
        raise ValueError(f"unsafe split path {rel!r}")
    norm = posixpath.normpath(rel)
    if norm != rel or ".." in rel.split("/") or not rel.startswith(category + "/"):
        raise ValueError(f"unsafe split path {rel!r}")
    return rel


def _rows(csv_path, category):
    with open(csv_path, newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        if next(reader, None) != HEADER:
            raise ValueError("unexpected split CSV header")
        rows = [row for row in reader if row and row[0] == category]
    if not rows:
        raise ValueError(f"category {category!r} absent from split CSV")
    for row in rows:
        if len(row) != 5:
            raise ValueError("malformed split CSV row")
    return rows


def split_identities(csv_path, category):
    """Return (train_ids, test_ids) with no labels for test rows."""
    train, test = [], []
    for _, split, label, image, _mask in _rows(csv_path, category):
        _check_relpath(image, category)
        if split == "train":
            if label != "normal":
                raise ValueError("1cls train split must contain normal images only")
            train.append(image)
        elif split == "test":
            test.append(image)
        else:
            raise ValueError(f"unknown split {split!r}")
    for name, ids in (("train", train), ("test", test)):
        if len(ids) != len(set(ids)):
            raise ValueError(f"duplicate {name} image ids")
    if set(train) & set(test):
        raise ValueError("train/test image overlap")
    return sorted(train), sorted(test)


def test_labels(csv_path, category):
    """Post-freeze only: {test image id: 1 for anomaly, 0 for normal}."""
    labels = {}
    for _, split, label, image, _mask in _rows(csv_path, category):
        if split != "test":
            continue
        if label not in ("normal", "anomaly"):
            raise ValueError(f"unknown label {label!r}")
        labels[_check_relpath(image, category)] = int(label == "anomaly")
    return labels


def fetch_archive(url, dest, expected_bytes, max_bytes):
    """Download with a HEAD size guard; refuses to overwrite a mismatching existing file."""
    dest = pathlib.Path(dest)
    request = urllib.request.Request(url, method="HEAD")
    with urllib.request.urlopen(request, timeout=60) as response:
        length = int(response.headers["Content-Length"])
        etag = response.headers.get("ETag")
    if length > max_bytes or length != expected_bytes:
        raise RuntimeError(f"archive HEAD length {length} violates guard/expectation")
    if dest.exists():
        if dest.stat().st_size != expected_bytes:
            raise RuntimeError(f"existing {dest} has unexpected size; remove it manually")
        return {"url": url, "bytes": length, "etag": etag, "downloaded": False}
    part = dest.with_suffix(dest.suffix + ".part")
    written = 0
    with urllib.request.urlopen(url, timeout=60) as response, open(part, "wb") as handle:
        for chunk in iter(lambda: response.read(1 << 20), b""):
            written += len(chunk)
            if written > max_bytes:
                raise RuntimeError("download exceeded size guard")
            handle.write(chunk)
    if written != expected_bytes:
        raise RuntimeError(f"downloaded {written} bytes, expected {expected_bytes}")
    os.replace(part, dest)
    return {"url": url, "bytes": length, "etag": etag, "downloaded": True}


def safe_extract(archive, dest, prefixes, max_total_bytes=1 << 31):
    """Extract only regular files/dirs under ``prefixes`` with filter='data' and path checks."""
    prefixes = tuple(prefixes)
    dest = pathlib.Path(dest).resolve()
    dest.mkdir(parents=True, exist_ok=True)
    manifest, total = {}, 0
    with tarfile.open(archive, "r:") as tar:
        for member in tar:
            name = member.name
            if not name.startswith(prefixes):
                continue
            if name.startswith(("/", "\\")) or ".." in name.replace("\\", "/").split("/"):
                raise ValueError(f"unsafe member {name!r}")
            if not (member.isfile() or member.isdir()):
                raise ValueError(f"refusing non-regular member {name!r}")
            target = (dest / name).resolve()
            if target != dest and dest not in target.parents:
                raise ValueError(f"member escapes destination: {name!r}")
            total += member.size
            if total > max_total_bytes:
                raise ValueError("extraction exceeds size bound")
            tar.extract(member, dest, filter="data")
            if member.isfile():
                manifest[name] = {"bytes": member.size, "sha256": sha256_file(target)}
    if not manifest:
        raise ValueError(f"no members under {prefixes!r}")
    return manifest


def write_json(path, payload):
    path = pathlib.Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)
