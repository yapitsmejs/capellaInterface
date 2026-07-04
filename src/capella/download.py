"""SLC downloader + listing helpers.

Downloads the SLC bundle (complex COG + extended-metadata JSON) for an item to a flat
``<dest_root>/<item_id>/`` layout, with **resumable downloads**: a file already complete
is skipped; a partial file is resumed via HTTP ``Range``; and transient connection errors
(mid-stream ``ChunkedEncodingError``/``ConnectionError``/``Timeout``) are retried by
resuming from the bytes already on disk. All HTTP I/O is isolated so tests can inject a
fake session.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

import requests
from tqdm import tqdm

from .catalog import item_assets, item_download_size, item_mode, item_polarization

CHUNK = 64 * 1024
MAX_RETRIES = 5

# Transient errors that justify a resume retry (not HTTPError — those are not retried).
_RETRY_EXC = (
    requests.exceptions.ChunkedEncodingError,
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
)


class IncompleteDownloadError(IOError):
    """Raised when a file could not reach its expected size after all resume retries."""

    def __init__(self, path: str, have: int, expected: int | None) -> None:
        self.path = path
        self.have = have
        self.expected = expected
        super().__init__(f"{path}: incomplete after {MAX_RETRIES} retries ({have} bytes")


def parse_range(expr: str, n: int) -> list[int]:
    """Parse a 1-indexed selection expression over a list of length `n`.

    Accepts ``3-8`` (inclusive), ``all``, or a single index ``5``. Raises ``ValueError``
    with a friendly message on out-of-range or unparseable input.
    """
    if n <= 0:
        raise ValueError("no items to select from")
    raw = expr.strip().lower()
    if raw == "all":
        return list(range(1, n + 1))
    if "-" in raw:
        a_str, b_str = raw.split("-", 1)
        try:
            a, b = int(a_str), int(b_str)
        except ValueError as e:
            raise ValueError(f"invalid range {expr!r}") from e
        if a < 1 or b > n or a > b:
            raise ValueError(f"range {expr!r} out of bounds (1..{n})")
        return list(range(a, b + 1))
    try:
        v = int(raw)
    except ValueError as e:
        raise ValueError(f"invalid range {expr!r}") from e
    if v < 1 or v > n:
        raise ValueError(f"index {v} out of bounds (1..{n})")
    return [v]


def _human_size(n: int) -> str:
    if n <= 0:
        return "?"
    units = ["B", "KB", "MB", "GB", "TB"]
    f = float(n)
    for u in units:
        if f < 1024 or u == units[-1]:
            return f"{f:.1f} {u}" if u != "B" else f"{int(f)} B"
        f /= 1024
    return f"{n} B"


def _pol(item) -> str:  # type: ignore[no-untyped-def]
    pol = item_polarization(item)
    return pol if pol else "?"


def format_item_table(items: list) -> str:  # type: ignore[no-untyped-def]
    """Render a numbered listing: #, ID, datetime, mode, pol, size."""
    header = f"{'#':>3}  {'ID':40}  {'datetime':19}  {'mode':16}  {'pol':3}  {'size':>9}"
    lines = [header]
    for i, item in enumerate(items, 1):
        dt = _item_dt_str(item)
        mode = str(item_mode(item) or "?")
        pol = _pol(item)
        size = _human_size(item_download_size(item))
        lines.append(f"{i:>3}  {item.id[:40]:40}  {dt:19}  {mode:16}  {pol:3}  {size:>9}")
    return "\n".join(lines)


def _item_dt_str(item) -> str:  # type: ignore[no-untyped-def]
    dt = item.datetime
    if dt is None:
        return "?"
    return dt.isoformat()[:19]


def total_size(items: list) -> int:  # type: ignore[no-untyped-def]
    return sum(item_download_size(i) for i in items)


def _remote_size(session, href: str) -> int | None:  # type: ignore[no-untyped-def]
    try:
        r = session.head(href, allow_redirects=True)
    except Exception:
        return None
    cl = r.headers.get("content-length") if hasattr(r, "headers") else None
    if getattr(r, "status_code", None) == 200 and cl:
        try:
            return int(cl)
        except (TypeError, ValueError):
            return None
    return None


def _stream_asset(
    sess,
    href: str,
    out_path: str,
    remote_size: int | None,
    show_progress: bool,
) -> str:
    """Stream `href` to `out_path`, resuming from any partial file and retrying transient
    connection errors. Returns ``"skipped"`` (already complete), ``"resumed"`` (completed
    from a partial file), or ``"downloaded"`` (fresh full write)."""
    local_size = os.path.getsize(out_path) if os.path.exists(out_path) else 0

    # Already complete -> skip without any GET.
    if remote_size is not None and local_size == remote_size and local_size > 0:
        return "skipped"

    # Resume only when we know the expected size and have a valid partial. Otherwise
    # truncate and start fresh (an unknown target size makes a Range append unsafe).
    can_resume = remote_size is not None and 0 < local_size < remote_size
    if not can_resume and os.path.exists(out_path):
        open(out_path, "wb").close()
        local_size = 0

    did_resume = False
    for attempt in range(MAX_RETRIES + 1):  # 1 initial attempt + MAX_RETRIES retries
        start = os.path.getsize(out_path) if os.path.exists(out_path) else 0
        if remote_size is not None and start >= remote_size:
            # nothing left to fetch (covers a completed retry)
            return "skipped" if (start == remote_size and local_size == remote_size) else "resumed"
        headers = {"Range": f"bytes={start}-"} if start > 0 and remote_size is not None else {}

        try:
            resp = sess.get(href, stream=True, headers=headers)
        except _RETRY_EXC:
            if attempt >= MAX_RETRIES:
                raise
            continue

        sc = getattr(resp, "status_code", 200)
        if sc == 206:
            mode = "ab"
            did_resume = did_resume or start > 0
        elif sc == 200:
            if start > 0:
                # server ignored the Range header -> restart from scratch
                start = 0
            mode = "wb"
        elif sc >= 400:
            resp.raise_for_status()  # type: ignore[no-untyped-call]
            mode = "wb"
        else:
            mode = "wb" if start == 0 else "ab"

        bar_total = remote_size
        bar_initial = start if mode == "ab" else 0
        if bar_total is None:
            cl = resp.headers.get("content-length") if hasattr(resp, "headers") else None
            bar_total = int(cl) if cl else None

        try:
            with open(out_path, mode) as f, tqdm(
                total=bar_total,
                initial=bar_initial,
                unit="B",
                unit_scale=True,
                desc=os.path.basename(out_path),
                disable=not show_progress,
            ) as bar:
                for chunk in resp.iter_content(chunk_size=CHUNK):
                    if chunk:
                        f.write(chunk)
                        bar.update(len(chunk))
        except _RETRY_EXC:
            if attempt >= MAX_RETRIES:
                raise
            continue  # retry resumes from the current file size

        final = os.path.getsize(out_path)
        if remote_size is None:
            return "downloaded"
        if final == remote_size:
            return "resumed" if did_resume else "downloaded"
        if final > remote_size:
            # overshoot (shouldn't happen with Range) -> restart fresh
            open(out_path, "wb").close()
            if attempt >= MAX_RETRIES:
                raise IncompleteDownloadError(out_path, final, remote_size)
            continue
        # short read -> retry resume
        if attempt >= MAX_RETRIES:
            raise IncompleteDownloadError(out_path, final, remote_size)

    # unreachable: loop either returns or raises
    return "downloaded"


def download_item(
    item,
    dest_root: str,
    *,
    session=None,
    show_progress: bool = True,
) -> tuple[str, list[tuple[str, str]]]:
    """Stream the SLC bundle for `item` to ``<dest_root>/<item_id>/<basename>``.

    Resumable: a file already complete is skipped; a partial file is resumed via HTTP
    ``Range``; transient connection errors are retried by resuming. Returns
    ``(out_dir, [(filename, status), ...])`` where status is ``"downloaded"``,
    ``"resumed"``, or ``"skipped"``.
    """
    sess = session if session is not None else requests
    out_dir = os.path.join(dest_root, item.id)
    os.makedirs(out_dir, exist_ok=True)

    results: list[tuple[str, str]] = []
    for key, asset in item_assets(item).items():
        href = asset.href
        filename = os.path.basename(urlparse(href).path) or f"{key}.bin"
        out_path = os.path.join(out_dir, filename)
        remote_size = _remote_size(sess, href)
        status = _stream_asset(sess, href, out_path, remote_size, show_progress)
        results.append((filename, status))

    return out_dir, results
