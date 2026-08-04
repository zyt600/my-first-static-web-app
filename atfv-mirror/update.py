from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from pypdf import PdfReader

PDF_URL = "https://www.alger.com/AlgerETFDailyHoldings/Daily_Holdings_Alger_35_ETF.pdf"
CSV_URL = "https://www.alger.com/AlgerETFDailyHoldings/Daily_Holdings_Alger_35_ETF.csv"
ROOT = Path(__file__).resolve().parent
ARCHIVE = ROOT / "archive"

REQUIRED_COLUMNS = {
    "Product Short Name",
    "Effective Date",
    "Ticker",
    "CUSIP",
    "Security Description",
    "Quantity",
    "Market Value",
    "Percentage Weight",
}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def download(url: str, attempts: int = 5) -> tuple[bytes, dict[str, str]]:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        separator = "&" if "?" in url else "?"
        cache_busted = f"{url}{separator}mirror_ts={int(time.time())}-{attempt}"
        request = urllib.request.Request(
            cache_busted,
            headers={
                "User-Agent": "Mozilla/5.0 ATFVOfficialMirror/1.0",
                "Accept": "*/*",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                body = response.read()
                headers = {k.lower(): v for k, v in response.headers.items()}
                if response.status != 200:
                    raise RuntimeError(f"HTTP {response.status}")
                return body, headers
        except (urllib.error.URLError, TimeoutError, RuntimeError) as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(3 * attempt)
    raise RuntimeError(f"Failed to download {url}: {last_error}")


def stable_download(url: str) -> tuple[bytes, dict[str, str]]:
    first, first_headers = download(url)
    second, second_headers = download(url)
    if sha256(first) == sha256(second):
        return second, second_headers

    third, third_headers = download(url)
    hashes = [sha256(first), sha256(second), sha256(third)]
    for candidate_hash in hashes:
        if hashes.count(candidate_hash) >= 2:
            if sha256(third) == candidate_hash:
                return third, third_headers
            if sha256(second) == candidate_hash:
                return second, second_headers
            return first, first_headers
    raise RuntimeError(f"Three downloads produced three different hashes: {hashes}")


def parse_number(value: str) -> float:
    cleaned = value.strip().replace(",", "").replace("$", "").replace("%", "")
    if cleaned in {"", "-", "—"}:
        return 0.0
    return float(cleaned)


def validate_pdf(pdf_bytes: bytes) -> tuple[str, int, str]:
    if not pdf_bytes.startswith(b"%PDF-"):
        raise RuntimeError("PDF response does not start with %PDF-")
    if len(pdf_bytes) < 1_000:
        raise RuntimeError(f"PDF is unexpectedly small: {len(pdf_bytes)} bytes")

    reader = PdfReader(io.BytesIO(pdf_bytes))
    if reader.is_encrypted:
        raise RuntimeError("PDF is encrypted")
    if len(reader.pages) < 1:
        raise RuntimeError("PDF has no pages")

    page_texts: list[str] = []
    dates: list[str] = []
    for index, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        page_texts.append(text)
        if "Product Short Name: ATFV" not in text:
            raise RuntimeError(f"Page {index} does not identify Product Short Name: ATFV")
        matches = re.findall(r"Effective Date:\s*(\d{2}/\d{2}/\d{4})", text)
        if not matches:
            raise RuntimeError(f"Page {index} has no Effective Date")
        dates.extend(matches)

    if len(set(dates)) != 1:
        raise RuntimeError(f"PDF pages disagree on Effective Date: {dates}")
    return dates[0], len(reader.pages), "\n".join(page_texts)


def parse_csv(csv_bytes: bytes) -> tuple[list[dict[str, str]], str, float]:
    text = csv_bytes.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise RuntimeError("CSV has no header")

    normalized_names = [name.strip() for name in reader.fieldnames]
    missing = REQUIRED_COLUMNS - set(normalized_names)
    if missing:
        raise RuntimeError(f"CSV is missing required columns: {sorted(missing)}")

    rows: list[dict[str, str]] = []
    dates: set[str] = set()
    total_weight = 0.0
    for raw_row in reader:
        row = {(key or "").strip(): (value or "").strip() for key, value in raw_row.items()}
        if not any(row.values()):
            continue
        if row["Product Short Name"] != "ATFV":
            raise RuntimeError(f"Unexpected Product Short Name: {row['Product Short Name']}")
        dates.add(row["Effective Date"])
        total_weight += parse_number(row["Percentage Weight"])
        parse_number(row["Quantity"])
        parse_number(row["Market Value"])
        rows.append(row)

    if len(rows) < 30:
        raise RuntimeError(f"CSV contains only {len(rows)} holdings rows")
    if len(dates) != 1:
        raise RuntimeError(f"CSV rows disagree on Effective Date: {sorted(dates)}")
    if not 99.5 <= total_weight <= 100.5:
        raise RuntimeError(f"Weight total is outside tolerance: {total_weight:.4f}%")
    return rows, next(iter(dates)), total_weight


def write_outputs(
    pdf_bytes: bytes,
    csv_bytes: bytes,
    holdings: list[dict[str, str]],
    effective_date_us: str,
    page_count: int,
    total_weight: float,
    pdf_headers: dict[str, str],
    csv_headers: dict[str, str],
) -> None:
    effective_date = datetime.strptime(effective_date_us, "%m/%d/%Y").date().isoformat()
    fetched_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    pdf_hash = sha256(pdf_bytes)
    csv_hash = sha256(csv_bytes)

    latest_pdf = ROOT / "latest.pdf"
    if latest_pdf.exists() and sha256(latest_pdf.read_bytes()) == pdf_hash:
        print(f"Official holdings unchanged: {effective_date}, PDF SHA-256 {pdf_hash}")
        return

    payload = {
        "fund": "ATFV",
        "effective_date": effective_date,
        "effective_date_source_format": effective_date_us,
        "fetched_at_utc": fetched_at,
        "source_pdf": PDF_URL,
        "source_csv": CSV_URL,
        "page_count": page_count,
        "holding_count": len(holdings),
        "total_weight_percent": round(total_weight, 4),
        "pdf_sha256": pdf_hash,
        "csv_sha256": csv_hash,
        "pdf_content_type": pdf_headers.get("content-type", ""),
        "csv_content_type": csv_headers.get("content-type", ""),
        "holdings": holdings,
    }

    ROOT.mkdir(parents=True, exist_ok=True)
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    latest_pdf.write_bytes(pdf_bytes)
    (ROOT / "latest.csv").write_bytes(csv_bytes)
    (ROOT / "latest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    (ARCHIVE / f"{effective_date}.pdf").write_bytes(pdf_bytes)
    (ARCHIVE / f"{effective_date}.csv").write_bytes(csv_bytes)
    (ARCHIVE / f"{effective_date}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"Mirrored ATFV holdings for {effective_date}: "
        f"{len(holdings)} rows, {total_weight:.2f}% total weight, {page_count} PDF pages"
    )


def main() -> int:
    pdf_bytes, pdf_headers = stable_download(PDF_URL)
    csv_bytes, csv_headers = stable_download(CSV_URL)

    pdf_date, page_count, _ = validate_pdf(pdf_bytes)
    holdings, csv_date, total_weight = parse_csv(csv_bytes)
    if pdf_date != csv_date:
        raise RuntimeError(f"PDF date {pdf_date} does not match CSV date {csv_date}")

    write_outputs(
        pdf_bytes=pdf_bytes,
        csv_bytes=csv_bytes,
        holdings=holdings,
        effective_date_us=pdf_date,
        page_count=page_count,
        total_weight=total_weight,
        pdf_headers=pdf_headers,
        csv_headers=csv_headers,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ATFV mirror update failed: {exc}", file=sys.stderr)
        raise
