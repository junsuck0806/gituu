"""한국 정치 뉴스 알리미
GitHub Actions에서 주기적으로 실행되어, 새로 올라온 기사(제목+링크)만 텔레그램으로 보낸다.
"""
import calendar
import html
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import quote

import feedparser
import requests

BASE = Path(__file__).parent
CONFIG_FILE = BASE / "config.json"
SEEN_FILE = BASE / "seen.json"

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

MAX_SEEN = 3000      # seen.json에 보관할 최대 기사 수 (오래된 것부터 삭제)
MAX_PER_RUN = 30     # 한 번 실행에 보낼 최대 기사 수 (남은 건 다음 실행 때 전송)
CHUNK = 10           # 텔레그램 메시지 1개에 담을 기사 수
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; political-news-alert/1.0)"}


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_seen(seen):
    SEEN_FILE.write_text(
        json.dumps(seen[-MAX_SEEN:], ensure_ascii=False, indent=0), encoding="utf-8"
    )


def feed_sources(cfg):
    """config.json에서 (이름, RSS 주소) 목록을 만든다."""
    for q in cfg.get("google_news_queries", []):
        url = (
            "https://news.google.com/rss/search?q="
            + quote(f"{q} when:1d")
            + "&hl=ko&gl=KR&ceid=KR:ko"
        )
        yield f"구글뉴스 '{q}'", url
    for name, url in cfg.get("rss_feeds", {}).items():
        yield name, url


def fetch_entries(name, url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"[경고] {name} 가져오기 실패: {type(e).__name__}", file=sys.stderr)
        return []
    return feedparser.parse(r.content).entries


def published_ts(entry):
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    return calendar.timegm(t) if t else 0


def collect(cfg, seen_set):
    """아직 안 보낸 새 기사 목록을 반환한다."""
    include = [k.lower() for k in cfg.get("include_keywords", [])]
    exclude = [k.lower() for k in cfg.get("exclude_keywords", [])]
    max_age = cfg.get("max_age_hours", 24)
    now = time.time()

    items, picked = [], set()
    for name, url in feed_sources(cfg):
        for e in fetch_entries(name, url):
            key = e.get("id") or e.get("link")
            title = (e.get("title") or "").strip()
            link = e.get("link")
            if not (key and title and link):
                continue
            if key in seen_set or key in picked:
                continue
            ts = published_ts(e)
            if ts and (now - ts) / 3600 > max_age:
                continue
            low = title.lower()  # 키워드 필터는 제목만 본다 (요약에는 관련 기사 제목이 섞여 있을 수 있음)
            if include and not any(k in low for k in include):
                continue
            if any(k in low for k in exclude):
                continue
            picked.add(key)
            items.append({"key": key, "title": title, "link": link, "ts": ts})
    return items


def send_telegram(text):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True},
    }
    for _ in range(3):
        try:
            r = requests.post(url, json=payload, timeout=20)
        except requests.RequestException as e:
            # 예외 메시지에는 토큰이 들어간 URL이 있을 수 있어서 종류만 출력한다.
            raise RuntimeError(f"네트워크 오류: {type(e).__name__}") from None
        if r.status_code == 429:  # 너무 빠르게 보냄 -> 안내된 시간만큼 기다렸다가 재시도
            try:
                wait = r.json().get("parameters", {}).get("retry_after", 5)
            except ValueError:
                wait = 5
            time.sleep(wait + 1)
            continue
        if r.ok:
            return
        raise RuntimeError(f"텔레그램 오류 {r.status_code}: {r.text[:200]}")
    raise RuntimeError("텔레그램 전송 재시도 초과 (429)")


def format_chunk(chunk):
    lines = ["📰 <b>정치 뉴스 새 소식</b>"]
    for it in chunk:
        href = html.escape(it["link"], quote=True)
        lines.append(f'• <a href="{href}">{html.escape(it["title"])}</a>')
    return "\n".join(lines)


def main():
    if not TOKEN or not CHAT_ID:
        sys.exit("환경변수 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 가 없습니다.")

    cfg = load_json(CONFIG_FILE, {})
    first_run = not SEEN_FILE.exists()
    seen = load_json(SEEN_FILE, [])
    items = collect(cfg, set(seen))

    # 첫 실행: 기존 기사 수십 건이 한꺼번에 쏟아지지 않도록 '이미 본 것'으로만 기록한다.
    if first_run:
        if not items:
            print("[경고] 첫 실행인데 가져온 기사가 없습니다. config.json의 검색어/주소를 확인하세요.")
            return
        save_seen([i["key"] for i in items])
        send_telegram("✅ 정치 뉴스 알리미가 시작되었습니다. 지금부터 올라오는 새 기사만 알려드립니다.")
        print(f"초기화 완료: {len(items)}건을 기준선으로 저장")
        return

    items.sort(key=lambda i: i["ts"])  # 오래된 것 -> 최신 순 (텔레그램 맨 아래가 최신)
    batch = items[:MAX_PER_RUN]
    print(f"새 기사 {len(items)}건 중 {len(batch)}건 전송 시도")

    for i in range(0, len(batch), CHUNK):
        chunk = batch[i : i + CHUNK]
        try:
            send_telegram(format_chunk(chunk))
        except RuntimeError as e:
            print(f"[오류] {e}", file=sys.stderr)
            sys.exit(1)  # 이미 보낸 건 아래에서 기록돼 있으므로 다음 실행 때 실패분만 재시도됨
        seen.extend(x["key"] for x in chunk)
        save_seen(seen)  # 전송에 성공한 기사만 '본 것'으로 기록
        time.sleep(1)


if __name__ == "__main__":
    main()
