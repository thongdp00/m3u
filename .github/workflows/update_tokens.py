import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

import requests
from bs4 import BeautifulSoup


SOURCE_URL = "https://tv.vietanhtv.top/sex/"
M3U_FILE = Path("ht-tv.m3u")

CHANNELS = [f"tv360plus{i}" for i in range(1, 16)]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/139.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://tv.vietanhtv.top/",
}


def log(message):
    print(f"[TV360] {message}", flush=True)


def fetch_source():
    last_error = None

    for attempt in range(1, 4):
        try:
            log(f"Fetching source, attempt {attempt}/3")

            response = requests.get(
                SOURCE_URL,
                headers=HEADERS,
                timeout=30,
            )

            response.raise_for_status()

            log(f"Source HTTP {response.status_code}")
            log(f"Downloaded {len(response.text)} bytes")

            return response.text

        except Exception as exc:
            last_error = exc
            log(f"Fetch failed: {exc}")

            if attempt < 3:
                time.sleep(3)

    raise RuntimeError(f"Cannot fetch source: {last_error}")


def normalize_url(url):
    return url.strip().strip("'\"")


def extract_urls(text):
    """
    Tìm các URL TV360 stream/cleankey trong HTML/JS.
    """
    urls = set()

    patterns = [
        r'https?://[^"\']+tv360\.php\?[^"\']+',
        r'https?://[^"\']+cleankey\.php\?[^"\']+',
    ]

    for pattern in patterns:
        for match in re.findall(pattern, text, flags=re.I):
            urls.add(normalize_url(match))

    return list(urls)


def extract_channel_data(html):
    """
    Trả về:

    {
        "tv360plus1": {
            "stream": "...",
            "license": "..."
        },
        ...
    }
    """

    result = {}

    soup = BeautifulSoup(html, "html.parser")

    # Lấy cả HTML nguyên bản và text/script
    sources = [html]

    for script in soup.find_all("script"):
        if script.string:
            sources.append(script.string)
        elif script.get_text():
            sources.append(script.get_text())

    combined = "\n".join(sources)

    all_urls = extract_urls(combined)

    for channel in CHANNELS:
        result[channel] = {
            "stream": None,
            "license": None,
        }

        # ---------------------------------------------------------
        # 1. Tìm vùng HTML/JS gần tv360plusN
        # ---------------------------------------------------------
        positions = [
            m.start()
            for m in re.finditer(
                re.escape(channel),
                combined,
                flags=re.I
            )
        ]

        contexts = []

        for pos in positions:
            start = max(0, pos - 10000)
            end = min(len(combined), pos + 10000)
            contexts.append(combined[start:end])

        # Nếu không tìm được tên kênh, vẫn thử toàn bộ dữ liệu
        if not contexts:
            contexts.append(combined)

        # ---------------------------------------------------------
        # 2. Tìm cleankey URL chứa đúng channel
        # ---------------------------------------------------------
        for context in contexts:
            urls = extract_urls(context)

            for url in urls:
                lower = url.lower()

                if "cleankey.php" in lower and channel.lower() in lower:
                    result[channel]["license"] = url
                    break

            if result[channel]["license"]:
                break

        # ---------------------------------------------------------
        # 3. Tìm stream URL
        # ---------------------------------------------------------
        for context in contexts:
            urls = extract_urls(context)

            for url in urls:
                lower = url.lower()

                if (
                    "tv360.php" in lower
                    and "token=" in lower
                    and (
                        f"id={channel.replace('tv360plus', '')}" in lower
                        or "expires=" in lower
                    )
                ):
                    result[channel]["stream"] = url
                    break

            if result[channel]["stream"]:
                break

        # ---------------------------------------------------------
        # 4. Nếu context theo channel không tìm được,
        #    thử ghép theo ID kênh hiện tại trong M3U sau này.
        # ---------------------------------------------------------

        log(
            f"{channel}: "
            f"stream={'OK' if result[channel]['stream'] else 'MISS'}, "
            f"license={'OK' if result[channel]['license'] else 'MISS'}"
        )

    return result


def replace_query_parameter(url, parameter, value):
    """
    Thay một query parameter nhưng giữ nguyên các phần khác.
    """
    parsed = urlparse(url)

    query = parse_qs(
        parsed.query,
        keep_blank_values=True
    )

    query[parameter] = [value]

    new_query = urlencode(
        query,
        doseq=True
    )

    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            new_query,
            parsed.fragment,
        )
    )


def update_m3u(m3u_text, fresh_data):
    """
    Chỉ cập nhật URL/token của TV360+1 ... TV360+15.

    Không đụng tới:
    - EXTINF
    - logo
    - group-title
    - user-agent
    - KODIPROP
    - các kênh khác
    """

    lines = m3u_text.splitlines()
    changed = False

    current_channel = None

    for i, line in enumerate(lines):

        # ---------------------------------------------------------
        # Nhận diện EXTINF TV360
        # ---------------------------------------------------------
        match = re.search(
            r'tvg-id="(tv360plus\d+)"',
            line,
            flags=re.I
        )

        if match:
            current_channel = match.group(1).lower()
            continue

        if not current_channel:
            continue

        data = fresh_data.get(current_channel)

        if not data:
            continue

        # ---------------------------------------------------------
        # Cập nhật license_key
        # ---------------------------------------------------------
        if "#KODIPROP:inputstream.adaptive.license_key=" in line:

            old_license = line.split("=", 1)[1].strip()
            new_license = data.get("license")

            if new_license:
                if old_license != new_license:
                    lines[i] = (
                        "#KODIPROP:"
                        "inputstream.adaptive.license_key="
                        + new_license
                    )

                    changed = True

                    log(
                        f"{current_channel}: license token updated"
                    )

            continue

        # ---------------------------------------------------------
        # Cập nhật stream URL
        # ---------------------------------------------------------
        if (
            line.startswith("http://")
            or line.startswith("https://")
        ):
            if "tv360.php" in line:

                new_stream = data.get("stream")

                if new_stream and line.strip() != new_stream:
                    lines[i] = new_stream
                    changed = True

                    log(
                        f"{current_channel}: stream URL updated"
                    )

            # Kết thúc block hiện tại
            current_channel = None

    return "\n".join(lines), changed


def validate_m3u(text):
    if not text.startswith("#EXTM3U"):
        raise RuntimeError("Invalid M3U: missing #EXTM3U")

    if "#EXTINF" not in text:
        raise RuntimeError("Invalid M3U: missing #EXTINF")

    for channel in CHANNELS:
        if f'tvg-id="{channel}"' in text:
            continue

        log(f"WARNING: {channel} not found in M3U")

    return True


def main():
    try:
        if not M3U_FILE.exists():
            raise FileNotFoundError(
                f"Missing file: {M3U_FILE}"
            )

        log(f"Reading {M3U_FILE}")

        old_text = M3U_FILE.read_text(
            encoding="utf-8"
        )

        validate_m3u(old_text)

        html = fetch_source()

        fresh_data = extract_channel_data(html)

        stream_count = sum(
            1
            for item in fresh_data.values()
            if item["stream"]
        )

        license_count = sum(
            1
            for item in fresh_data.values()
            if item["license"]
        )

        log(
            f"Found stream URLs: "
            f"{stream_count}/15"
        )

        log(
            f"Found license URLs: "
            f"{license_count}/15"
        )

        # ---------------------------------------------------------
        # SAFETY:
        # Nếu không lấy được bất kỳ token nào thì KHÔNG sửa M3U.
        # ---------------------------------------------------------
        if stream_count == 0 and license_count == 0:
            raise RuntimeError(
                "No TV360 data extracted. "
                "M3U was NOT modified."
            )

        new_text, changed = update_m3u(
            old_text,
            fresh_data
        )

        validate_m3u(new_text)

        if not changed:
            log("No changes detected.")
            return 0

        M3U_FILE.write_text(
            new_text + "\n",
            encoding="utf-8"
        )

        log(f"Updated {M3U_FILE}")

        return 0

    except Exception as exc:
        log(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
