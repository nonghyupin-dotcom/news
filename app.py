"""
자동 뉴스 수집 및 요약기 v2.0
- UI: Streamlit 웹 애플리케이션 (3탭 구조)
- 뉴스: 네이버 뉴스 포털(news.naver.com) 전체 본문 크롤링
- 요약: 빈도 기반 추출 요약 (외부 AI API 미사용)
- 알림: 텔레그램 봇 API 연동
- 저장: 일자별 폴더 / CSV(전체원문) + TXT(종합요약) 자동 생성
"""

import os
import re
import csv
import sys
import json
import time
import logging
import threading
from datetime import datetime
from collections import Counter
from io import StringIO

import requests
import schedule
import urllib3
from bs4 import BeautifulSoup
import streamlit as st

# ── SSL 경고 억제 (기업망 자체 인증서 환경 대응) ─────────────────────────────
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ══════════════════════════════════════════════════════════════
# 1. 로깅 설정 (메모리 내 로그 버퍼 → Streamlit 탭 3에 출력)
# ══════════════════════════════════════════════════════════════
log_stream = StringIO()

logger = logging.getLogger("NewsSummarizer")
logger.setLevel(logging.DEBUG)
if not logger.handlers:
    # 파일 핸들러
    fh = logging.FileHandler("error.log", encoding="utf-8")
    fh.setLevel(logging.ERROR)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(fh)
    # 메모리 스트림 핸들러 (탭3 로그 패널용)
    sh = logging.StreamHandler(log_stream)
    sh.setLevel(logging.INFO)
    sh.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S"))
    logger.addHandler(sh)
    # 콘솔 핸들러
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("%(asctime)s - %(message)s"))
    logger.addHandler(ch)

# ══════════════════════════════════════════════════════════════
# 2. 설정 파일 (config.json) 읽기/쓰기
# ══════════════════════════════════════════════════════════════
CONFIG_FILE = "config.json"
DEFAULT_CONFIG = {
    "keywords": ["ai", "생성형", "llm"],
    "telegram_token": "",
    "telegram_chat_id": "",
    "schedule_hour": 8,
    "schedule_minute": 0,
    "limit_per_keyword": 8,
}

def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            for k, v in DEFAULT_CONFIG.items():
                cfg.setdefault(k, v)
            return cfg
        except Exception:
            pass
    return DEFAULT_CONFIG.copy()

def save_config(cfg: dict):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"설정 저장 실패: {e}")

# ══════════════════════════════════════════════════════════════
# 3. 텍스트 추출 요약 알고리즘 (Extractive Summarizer)
# ══════════════════════════════════════════════════════════════
def extract_summary(text: str, num_sentences: int = 3) -> str:
    """
    한글 형태소 분석기 없이도 작동하는 빈도 기반 추출 요약기.
    문장 내 단어들의 빈도수를 측정하여 핵심 문장들을 순서대로 추출합니다.
    """
    if not text or len(text.strip()) < 10:
        return "기사 본문 내용이 없거나 요약하기에 너무 짧습니다."

    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    sentences = [s.strip() for s in sentences if len(s.strip()) > 5]

    if len(sentences) <= num_sentences:
        return " ".join(sentences)

    postpositions = ['은', '는', '이', '가', '을', '를', '에', '의', '로', '와', '과',
                     '으로', '에서', '하고', '그리고', '하지만', '또한']
    words = []
    for sent in sentences:
        for word in sent.split():
            cw = re.sub(r'[^\w\s]', '', word)
            if len(cw) >= 2:
                for post in postpositions:
                    if cw.endswith(post) and len(cw) > len(post):
                        cw = cw[:-len(post)]
                        break
                words.append(cw)

    word_counts = Counter(words)
    sentence_scores: dict = {}
    for i, sent in enumerate(sentences):
        score = 0
        sw = sent.split()
        if not sw:
            continue
        for word in sw:
            cw = re.sub(r'[^\w\s]', '', word)
            for post in postpositions:
                if cw.endswith(post) and len(cw) > len(post):
                    cw = cw[:-len(post)]
                    break
            score += word_counts.get(cw, 0)
        sentence_scores[i] = score / (len(sw) + 2)

    top_idx = sorted(sentence_scores, key=sentence_scores.get, reverse=True)[:num_sentences]
    top_idx.sort()
    return " ".join(sentences[i] for i in top_idx)

# ══════════════════════════════════════════════════════════════
# 4. 뉴스 크롤러 (기존 로직 100% 유지)
# ══════════════════════════════════════════════════════════════
class NewsScraper:
    def __init__(self):
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/110.0.0.0 Safari/537.36"
            )
        }

    def clean_news_body(self, text: str) -> str:
        if not text:
            return ""
        text = re.sub(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', '', text)
        text = re.sub(r'^\[[^\]]+\]|^\([^\)]+\)|^【[^】]+】', '', text)
        text = re.sub(r'저작권자\(c\).*?금지', '', text)
        text = re.sub(r'무단\s*전재\s*및\s*재배포\s*금지', '', text)
        text = re.sub(r'기사제보.*?\.', '', text)
        return ' '.join(text.split()).strip()

    def get_naver_news_content(self, url: str) -> str:
        """네이버 뉴스 본문 영역을 파싱하여 클렌징된 텍스트를 반환합니다."""
        if "news.naver.com" not in url:
            return ""
        try:
            res = requests.get(url, headers=self.headers, timeout=5, verify=False)
            if res.status_code == 200:
                soup = BeautifulSoup(res.text, 'html.parser')
                selectors = [
                    '#dic_area', '#articleBodyContents', '#articleBody',
                    'div.article_body', 'div#article_body'
                ]
                content_div = None
                for sel in selectors:
                    content_div = soup.select_one(sel)
                    if content_div:
                        break
                if content_div:
                    for tag in content_div(['script', 'style', 'iframe', 'noscript']):
                        tag.decompose()
                    text = content_div.get_text(separator=' ')
                    return self.clean_news_body(text)
        except Exception as e:
            logger.error(f"네이버 뉴스 본문 크롤링 에러 ({url}): {e}", exc_info=True)
        return ""

    def search_news(self, keyword: str, limit: int = 10, progress_cb=None) -> list:
        """
        네이버 뉴스 검색 결과에서 news.naver.com 링크만 선별,
        전체 본문 크롤링 후 반환합니다.
        """
        news_list = []
        search_url = (
            f"https://search.naver.com/search.naver"
            f"?where=news&query={requests.utils.quote(keyword)}&sort=0"
        )
        try:
            res = requests.get(search_url, headers=self.headers, timeout=10, verify=False)
            if res.status_code != 200:
                logger.error(f"네이버 검색 결과 요청 실패 (상태코드: {res.status_code})")
                return news_list

            soup = BeautifulSoup(res.text, 'html.parser')

            # FDS 레이아웃 (최신)
            container = soup.find(class_='fds-news-item-list-tab')
            news_cards = []
            if container:
                news_cards = container.find_all('div', recursive=False)

            # 자가 복구: 개별 타이틀 링크 기반 카드 탐색
            if not news_cards:
                title_links = soup.select('a[data-heatmap-target=".tit"]')
                if title_links:
                    seen_parents = set()
                    for tl in title_links:
                        p = tl.parent
                        for _ in range(3):
                            if p and p.name == 'div':
                                break
                            p = p.parent if p else None
                        if p and p not in seen_parents:
                            seen_parents.add(p)
                            news_cards.append(p)

            # 고전 레이아웃 폴백
            if not news_cards:
                classic_bx = soup.select('ul.list_news > li.bx')
                if classic_bx:
                    for bx in classic_bx:
                        try:
                            naver_link = ""
                            tit_a = bx.select_one('a.news_tit')
                            if not tit_a:
                                continue
                            tit_href = tit_a.get('href', '')
                            if "news.naver.com" in tit_href:
                                naver_link = tit_href
                            else:
                                for info in bx.select('div.info_group > a.info'):
                                    href = info.get('href', '')
                                    if "news.naver.com" in href:
                                        naver_link = href
                                        break
                            if not naver_link:
                                continue
                            title = tit_a.get('title') or tit_a.get_text().strip()
                            press_e = bx.select_one('a.info.press')
                            press = press_e.get_text().strip() if press_e else "알 수 없음"
                            body_text = self.get_naver_news_content(naver_link)
                            if len(body_text) < 100:
                                continue
                            summary = extract_summary(body_text, num_sentences=2)
                            news_list.append({
                                'keyword': keyword, 'press': press,
                                'title': title, 'link': naver_link,
                                'summary': summary, 'body_text': body_text,
                            })
                            if progress_cb:
                                progress_cb(len(news_list) / limit)
                            if len(news_list) >= limit:
                                break
                        except Exception:
                            continue
                    return news_list

            # FDS 카드 루프
            for card in news_cards:
                try:
                    naver_link = ""
                    nav_e = card.select_one('a[data-heatmap-target=".nav"]')
                    if nav_e:
                        naver_link = nav_e.get('href', '')
                    if not naver_link or "news.naver.com" not in naver_link:
                        for a in card.find_all('a'):
                            href = a.get('href', '')
                            if "news.naver.com" in href or "n.news.naver.com" in href:
                                naver_link = href
                                break
                    if not naver_link:
                        continue
                    title_e = card.select_one('a[data-heatmap-target=".tit"]')
                    if not title_e:
                        title_e = card.select_one('a.news_tit')
                    if not title_e:
                        continue
                    title = title_e.get_text(strip=True)
                    texts = [t.strip() for t in card.find_all(string=True) if t.strip()]
                    press = texts[0] if texts else "알 수 없음"
                    if len(press) > 15 or not press:
                        press = "언론사"
                    body_text = self.get_naver_news_content(naver_link)
                    if len(body_text) < 100:
                        continue
                    summary = extract_summary(body_text, num_sentences=2)
                    news_list.append({
                        'keyword': keyword, 'press': press,
                        'title': title, 'link': naver_link,
                        'summary': summary, 'body_text': body_text,
                    })
                    if progress_cb:
                        progress_cb(len(news_list) / limit)
                    if len(news_list) >= limit:
                        break
                except Exception as ex:
                    logger.error(f"개별 뉴스 파싱 에러: {ex}", exc_info=True)
                    continue

        except Exception as e:
            logger.error(f"뉴스 검색 페이지 로딩 에러: {e}", exc_info=True)

        return news_list

# ══════════════════════════════════════════════════════════════
# 5. 뉴스 수집 + 파일 저장 관리자
# ══════════════════════════════════════════════════════════════
class NewsManager:
    @staticmethod
    def collect_and_save(keywords: list, limit_per_keyword: int = 8,
                         progress_cb=None) -> tuple:
        """
        뉴스 수집 → 중복 제거 → CSV/TXT 저장
        Returns: (success, all_news, global_summary)
        """
        logger.info("=== 뉴스 수집 프로세스 시작 ===")
        scraper = NewsScraper()
        all_news = []
        seen_urls = set()
        seen_titles = set()

        total_kw = len([k for k in keywords if k.strip()])
        for kw_idx, kw in enumerate(keywords):
            kw = kw.strip()
            if not kw:
                continue
            logger.info(f"키워드 '{kw}' 뉴스 검색 중...")

            def _pcb(frac, _ki=kw_idx, _total_kw=total_kw):
                if progress_cb:
                    overall = (_ki / _total_kw) + (frac / _total_kw)
                    progress_cb(min(overall, 0.99))

            results = scraper.search_news(kw, limit=limit_per_keyword, progress_cb=_pcb)
            for item in results:
                url = item['link']
                title_norm = re.sub(r'\s+', '', item['title'])
                if url in seen_urls or title_norm in seen_titles:
                    continue
                seen_urls.add(url)
                seen_titles.add(title_norm)
                all_news.append(item)

        if not all_news:
            logger.warning("수집된 뉴스가 없습니다. 인터넷 연결 및 키워드를 확인해 주세요.")
            return False, [], ""

        today_str = datetime.now().strftime("%Y-%m-%d")
        output_dir = f"news_{today_str}"
        os.makedirs(output_dir, exist_ok=True)

        # ── CSV 저장 ─────────────────────────────────────────
        csv_filename = os.path.join(output_dir, f"news_list_{today_str}.csv")
        try:
            with open(csv_filename, 'w', encoding='utf-8-sig', newline='') as f:
                writer = csv.writer(f, quoting=csv.QUOTE_ALL)
                writer.writerow(['수집일시', '키워드', '언론사', '제목', '링크', '기사원문'])
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                for item in all_news:
                    writer.writerow([
                        now_str,
                        item['keyword'],
                        item['press'],
                        item['title'],
                        item['link'],
                        item.get('body_text', ''),
                    ])
            logger.info(f"엑셀(CSV) 저장 완료: {csv_filename}")
        except Exception as e:
            logger.error(f"CSV 저장 중 오류: {e}", exc_info=True)

        # ── TXT 종합 요약 리포트 ─────────────────────────────
        report_filename = os.path.join(output_dir, f"news_summary_report_{today_str}.txt")
        global_summary = ""
        try:
            all_summaries_text = " ".join(item['summary'] for item in all_news)
            global_summary = extract_summary(all_summaries_text, num_sentences=4)
            with open(report_filename, 'w', encoding='utf-8') as f:
                f.write("=" * 60 + "\n")
                f.write(f"   [종합 뉴스 요약 리포트] (수집일시: {datetime.now().strftime('%Y-%m-%d %H:%M')})\n")
                f.write("=" * 60 + "\n\n")
                f.write(f"● 수집 키워드: {', '.join(keywords)}\n")
                f.write(f"● 총 수집 뉴스: {len(all_news)}건 (중복 제거 완료)\n\n")
                f.write("-" * 60 + "\n")
                f.write("★ 전체 뉴스 종합 요약 (AI 요약본)\n")
                f.write("-" * 60 + "\n")
                f.write(global_summary + "\n\n")
                f.write("=" * 60 + "\n")
                f.write("★ 개별 뉴스 목록 및 기사 원문\n")
                f.write("=" * 60 + "\n")
                for idx, item in enumerate(all_news, 1):
                    f.write(f"\n[{idx}] [{item['keyword']}] {item['title']} ({item['press']})\n")
                    f.write(f"  링크: {item['link']}\n")
                    f.write("-" * 60 + "\n")
                    f.write(item.get('body_text', item['summary']) + "\n")
                    f.write("=" * 60 + "\n")
            logger.info(f"TXT 리포트 저장 완료: {report_filename}")
        except Exception as e:
            logger.error(f"TXT 리포트 저장 중 오류: {e}", exc_info=True)

        logger.info(f"=== 뉴스 수집 완료 ({len(all_news)}건) ===")
        return True, all_news, global_summary

# ══════════════════════════════════════════════════════════════
# 6. 텔레그램 발송 함수 (requests.post 직접 구현)
# ══════════════════════════════════════════════════════════════
TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

def send_telegram(token: str, chat_id: str, text: str) -> tuple:
    """텔레그램 봇으로 메시지를 발송합니다. Returns (success, error_msg)"""
    if not token or not chat_id:
        return False, "텔레그램 봇 토큰 또는 Chat ID가 설정되지 않았습니다."
    url = TELEGRAM_API.format(token=token.strip())
    # 4096자 초과 시 분할 전송
    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
    try:
        for chunk in chunks:
            resp = requests.post(url, json={
                "chat_id": chat_id.strip(),
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }, timeout=10, verify=False)
            if not resp.ok:
                err = resp.json().get("description", resp.text)
                return False, f"텔레그램 API 오류: {err}"
        return True, ""
    except Exception as e:
        return False, str(e)

def build_summary_telegram_msg(all_news: list, global_summary: str, keywords: list) -> str:
    """수집 완료 시 텔레그램으로 보낼 종합 요약 메시지를 생성합니다."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"📰 <b>[뉴스 수집 완료]</b> {now}",
        f"🔍 키워드: {', '.join(keywords)}  |  총 {len(all_news)}건\n",
        "📋 <b>종합 요약</b>",
        global_summary,
        "",
        "━━━━━━━━━━━━━━━━━━━━",
    ]
    for idx, item in enumerate(all_news[:10], 1):
        lines.append(f"{idx}. [{item['press']}] {item['title']}")
        lines.append(f"   🔗 {item['link']}")
    return "\n".join(lines)

def build_article_telegram_msg(item: dict) -> str:
    """단건 기사 텔레그램 메시지를 생성합니다."""
    summary = item.get('summary', '')
    return (
        f"📰 <b>{item['title']}</b>\n"
        f"🏢 {item['press']}  |  🔍 #{item['keyword']}\n"
        f"🔗 {item['link']}\n\n"
        f"<b>요약</b>\n{summary}"
    )

# ══════════════════════════════════════════════════════════════
# 7. 백그라운드 수집 스레드 관리
# ══════════════════════════════════════════════════════════════
_collection_thread = None
_scheduler_thread = None
_scheduler_stop = threading.Event()

def _run_collection(keywords, limit, token, chat_id, progress_cb=None):
    """백그라운드에서 수집 후 텔레그램 발송까지 수행"""
    st.session_state["collecting"] = True
    st.session_state["progress"] = 0.0
    try:
        success, all_news, global_summary = NewsManager.collect_and_save(
            keywords, limit_per_keyword=limit, progress_cb=progress_cb
        )
        if success:
            st.session_state["news_data"] = all_news
            st.session_state["global_summary"] = global_summary
            st.session_state["last_collected"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            # 텔레그램 자동 발송
            if token and chat_id:
                msg = build_summary_telegram_msg(all_news, global_summary, keywords)
                ok, err = send_telegram(token, chat_id, msg)
                if ok:
                    logger.info("텔레그램 종합 요약 발송 완료")
                else:
                    logger.error(f"텔레그램 발송 실패: {err}")
    except Exception as e:
        logger.error(f"수집 스레드 예외: {e}", exc_info=True)
    finally:
        st.session_state["collecting"] = False
        st.session_state["progress"] = 1.0

def start_collection_thread(keywords, limit, token, chat_id, progress_cb=None):
    global _collection_thread
    if _collection_thread and _collection_thread.is_alive():
        return False
    _collection_thread = threading.Thread(
        target=_run_collection,
        args=(keywords, limit, token, chat_id, progress_cb),
        daemon=True
    )
    _collection_thread.start()
    return True

def _scheduler_loop(hour, minute, keywords, limit, token, chat_id):
    schedule.clear()
    schedule.every().day.at(f"{hour:02d}:{minute:02d}").do(
        lambda: start_collection_thread(keywords, limit, token, chat_id)
    )
    logger.info(f"스케줄러 시작: 매일 {hour:02d}:{minute:02d} 자동 수집")
    while not _scheduler_stop.is_set():
        schedule.run_pending()
        time.sleep(30)
    logger.info("스케줄러 종료")

def start_scheduler(hour, minute, keywords, limit, token, chat_id):
    global _scheduler_thread
    _scheduler_stop.clear()
    _scheduler_thread = threading.Thread(
        target=_scheduler_loop,
        args=(hour, minute, keywords, limit, token, chat_id),
        daemon=True
    )
    _scheduler_thread.start()

def stop_scheduler():
    _scheduler_stop.set()

def scheduler_running() -> bool:
    return _scheduler_thread is not None and _scheduler_thread.is_alive()

# ══════════════════════════════════════════════════════════════
# 8. Streamlit 세션 상태 초기화
# ══════════════════════════════════════════════════════════════
def init_session():
    cfg = load_config()
    defaults = {
        "news_data": [],
        "global_summary": "",
        "collecting": False,
        "progress": 0.0,
        "last_collected": "",
        "config": cfg,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

# ══════════════════════════════════════════════════════════════
# 9. Streamlit 앱 메인
# ══════════════════════════════════════════════════════════════
def main():
    st.set_page_config(
        page_title="자동 뉴스 수집 요약기 v2.0",
        page_icon="📰",
        layout="wide",
        initial_sidebar_state="collapsed",
    )

    # ── 커스텀 CSS ───────────────────────────────────────────
    st.markdown("""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@400;500;700&display=swap');
    html, body, [class*="css"] { font-family: 'Noto Sans KR', sans-serif; }

    .main-header {
        background: linear-gradient(135deg, #0f172a 0%, #1e3a5f 50%, #0f172a 100%);
        padding: 2rem 2.5rem 1.5rem;
        border-radius: 16px;
        margin-bottom: 1.5rem;
        border: 1px solid rgba(59,130,246,0.3);
        box-shadow: 0 8px 32px rgba(0,0,0,0.4);
    }
    .main-header h1 { color: #e2e8f0; margin:0; font-size:1.9rem; font-weight:700; }
    .main-header p  { color: #94a3b8; margin:0.3rem 0 0; font-size:0.9rem; }

    .status-badge {
        display: inline-flex; align-items: center; gap: 0.4rem;
        padding: 0.3rem 0.8rem; border-radius: 999px; font-size: 0.82rem; font-weight:600;
    }
    .badge-active   { background: rgba(34,197,94,0.15); color: #4ade80; border: 1px solid rgba(34,197,94,0.4); }
    .badge-inactive { background: rgba(239,68,68,0.12);  color: #f87171; border: 1px solid rgba(239,68,68,0.3); }
    .badge-running  { background: rgba(234,179,8,0.15);  color: #fbbf24; border: 1px solid rgba(234,179,8,0.4); }

    div[data-testid="stTextArea"] textarea {
        background: #0a0f1e !important;
        color: #a3e635 !important;
        font-family: 'Consolas', monospace !important;
        font-size: 0.82rem !important;
        border: 1px solid rgba(59,130,246,0.2) !important;
        border-radius: 8px !important;
    }
    </style>
    """, unsafe_allow_html=True)

    init_session()
    cfg: dict = st.session_state["config"]

    # ── 헤더 ─────────────────────────────────────────────────
    st.markdown("""
    <div class="main-header">
        <h1>📰 자동 뉴스 수집 및 요약기 v2.0</h1>
        <p>네이버 뉴스 포털 기사 전문 수집 · 빈도 기반 AI 요약 · 텔레그램 알림 연동</p>
    </div>
    """, unsafe_allow_html=True)

    # ── 3탭 레이아웃 ─────────────────────────────────────────
    tab1, tab2, tab3 = st.tabs([
        "⚙️  수집 및 알림 설정",
        "📄  오늘의 뉴스",
        "🖥️  시스템 로그",
    ])

    # ════════════════════════════════════════════════
    # 탭 1 : 수집 및 알림 설정
    # ════════════════════════════════════════════════
    with tab1:
        col_stat, col_last = st.columns([1, 1])
        with col_stat:
            is_running = scheduler_running()
            is_collecting = st.session_state.get("collecting", False)
            if is_collecting:
                st.markdown('<span class="status-badge badge-running">⏳ 수집 중</span>', unsafe_allow_html=True)
            elif is_running:
                st.markdown('<span class="status-badge badge-active">🟢 스케줄러 감시 중</span>', unsafe_allow_html=True)
            else:
                st.markdown('<span class="status-badge badge-inactive">🔴 스케줄러 비활성</span>', unsafe_allow_html=True)
        with col_last:
            last = st.session_state.get("last_collected", "")
            if last:
                st.caption(f"✅ 마지막 수집: {last}")

        prog = st.session_state.get("progress", 0.0)
        if is_collecting or (prog > 0 and prog < 1.0):
            st.progress(prog, text="뉴스 수집 진행 중..." if is_collecting else "수집 완료")

        st.divider()

        # ── 키워드 설정 ──────────────────────────────────────
        st.subheader("🔍 검색 키워드")
        kw_input = st.text_input(
            "키워드 (쉼표로 구분)",
            value=", ".join(cfg.get("keywords", DEFAULT_CONFIG["keywords"])),
            placeholder="예: ai, 생성형, llm, 반도체",
            key="kw_input",
        )
        limit_per = st.number_input(
            "키워드당 최대 수집 기사 수",
            min_value=1, max_value=30,
            value=cfg.get("limit_per_keyword", 8),
            key="limit_per",
        )

        st.divider()

        # ── 스케줄러 설정 ────────────────────────────────────
        st.subheader("🕐 자동 수집 스케줄")
        col_h, col_m = st.columns(2)
        with col_h:
            s_hour = st.number_input("시(Hour)", 0, 23, cfg.get("schedule_hour", 8), key="s_hour")
        with col_m:
            s_min  = st.number_input("분(Min)",  0, 59, cfg.get("schedule_minute", 0), key="s_min")

        col_btn1, col_btn2 = st.columns(2)
        with col_btn1:
            if st.button("▶ 스케줄러 시작", use_container_width=True, type="secondary"):
                keywords = [k.strip() for k in kw_input.split(",") if k.strip()]
                if not scheduler_running():
                    start_scheduler(
                        s_hour, s_min, keywords, limit_per,
                        cfg.get("telegram_token", ""),
                        cfg.get("telegram_chat_id", ""),
                    )
                    st.success(f"스케줄러 시작: 매일 {s_hour:02d}:{s_min:02d} 자동 수집")
                else:
                    st.warning("이미 스케줄러가 실행 중입니다.")
        with col_btn2:
            if st.button("⏹ 스케줄러 중지", use_container_width=True):
                stop_scheduler()
                st.info("스케줄러를 중지했습니다.")

        st.divider()

        # ── 텔레그램 연동 설정 ───────────────────────────────
        st.subheader("🤖 텔레그램 알림 설정")
        st.caption("봇 토큰과 Chat ID를 입력하면 수집 완료 시 자동으로 요약본이 발송됩니다.")
        tg_token = st.text_input(
            "봇 토큰 (Bot Token)",
            value=cfg.get("telegram_token", ""),
            type="password",
            placeholder="123456789:ABCDEFGxxxxxxx",
            key="tg_token",
        )
        tg_chat = st.text_input(
            "Chat ID",
            value=cfg.get("telegram_chat_id", ""),
            placeholder="-100xxxxxxxxxx  또는  @채널명",
            key="tg_chat",
        )
        col_ts1, col_ts2 = st.columns(2)
        with col_ts1:
            if st.button("💾 설정 저장", use_container_width=True):
                cfg["keywords"] = [k.strip() for k in kw_input.split(",") if k.strip()]
                cfg["telegram_token"]    = tg_token
                cfg["telegram_chat_id"]  = tg_chat
                cfg["schedule_hour"]     = int(s_hour)
                cfg["schedule_minute"]   = int(s_min)
                cfg["limit_per_keyword"] = int(limit_per)
                save_config(cfg)
                st.session_state["config"] = cfg
                st.success("✅ 설정이 저장되었습니다.")
        with col_ts2:
            if st.button("📡 텔레그램 연결 테스트", use_container_width=True):
                ok, err = send_telegram(tg_token, tg_chat, "✅ 뉴스 수집기 v2.0 텔레그램 연결 테스트 성공!")
                if ok:
                    st.success("테스트 메시지 발송 성공!")
                else:
                    st.error(f"발송 실패: {err}")

        st.divider()

        # ── 즉시 수집 버튼 ───────────────────────────────────
        if st.button("⚡ 지금 즉시 뉴스 수집 시작", use_container_width=True, type="primary"):
            keywords = [k.strip() for k in kw_input.split(",") if k.strip()]
            if not keywords:
                st.warning("키워드를 하나 이상 입력해 주세요.")
            elif st.session_state.get("collecting"):
                st.warning("이미 수집이 진행 중입니다. 잠시 후 다시 시도해 주세요.")
            else:
                def _update_progress(frac: float):
                    st.session_state["progress"] = frac

                cfg["keywords"] = keywords
                cfg["limit_per_keyword"] = int(limit_per)
                started = start_collection_thread(
                    keywords, int(limit_per),
                    tg_token, tg_chat,
                    progress_cb=_update_progress,
                )
                if started:
                    st.success("🚀 수집을 시작했습니다! 잠시 후 [오늘의 뉴스] 탭에서 결과를 확인하세요.")
                    st.info("💡 페이지를 새로 고침하면 최신 상태가 반영됩니다.")
                else:
                    st.warning("이미 수집 스레드가 실행 중입니다.")

    # ════════════════════════════════════════════════
    # 탭 2 : 오늘의 뉴스
    # ════════════════════════════════════════════════
    with tab2:
        news_data = st.session_state.get("news_data", [])

        if not news_data:
            st.info("📭 아직 수집된 뉴스가 없습니다. [수집 및 알림 설정] 탭에서 수집을 시작해 주세요.")
        else:
            st.caption(f"총 **{len(news_data)}건** 수집 완료 | 마지막 수집: {st.session_state.get('last_collected', '-')}")

            global_sum = st.session_state.get("global_summary", "")
            if global_sum:
                with st.expander("📋 전체 뉴스 종합 요약 보기", expanded=True):
                    st.info(global_sum)

            st.divider()

            col_filter, _ = st.columns([2, 3])
            with col_filter:
                keywords_available = sorted(set(n['keyword'] for n in news_data))
                kw_filter = st.multiselect(
                    "키워드 필터",
                    options=keywords_available,
                    default=keywords_available,
                    key="kw_filter",
                )
            filtered = [n for n in news_data if n['keyword'] in kw_filter]

            import pandas as pd
            df = pd.DataFrame([{
                "번호": i + 1,
                "언론사": n['press'],
                "제목": n['title'],
                "키워드": f"#{n['keyword']}",
            } for i, n in enumerate(filtered)])

            st.dataframe(df, use_container_width=True, hide_index=True, height=220)

            article_titles = [f"[{n['press']}] {n['title']}" for n in filtered]
            selected_label = st.selectbox(
                "📰 본문을 볼 기사를 선택하세요",
                options=article_titles,
                key="selected_article",
            )

            if selected_label:
                sel_idx = article_titles.index(selected_label)
                sel_item = filtered[sel_idx]

                st.markdown(f"### {sel_item['title']}")
                st.caption(f"🏢 {sel_item['press']}  |  🔍 #{sel_item['keyword']}")

                st.markdown("**📋 요약**")
                st.info(sel_item.get('summary', ''))

                st.markdown("**📄 기사 전체 원문**")
                st.text_area(
                    label="원문",
                    value=sel_item.get('body_text', '본문을 불러올 수 없습니다.'),
                    height=340,
                    key="body_area",
                    label_visibility="collapsed",
                )

                col_link, col_tg, _ = st.columns([1, 1, 2])
                with col_link:
                    st.link_button("🔗 본문 링크 열기", url=sel_item['link'], use_container_width=True)
                with col_tg:
                    tg_cfg_token = st.session_state["config"].get("telegram_token", "")
                    tg_cfg_chat  = st.session_state["config"].get("telegram_chat_id", "")
                    if st.button("🚀 선택 기사 텔레그램 즉시 발송",
                                 use_container_width=True, key="tg_send_btn"):
                        if not tg_cfg_token or not tg_cfg_chat:
                            st.warning("텔레그램 봇 토큰과 Chat ID를 [수집 및 알림 설정] 탭에서 먼저 입력하고 저장해 주세요.")
                        else:
                            msg = build_article_telegram_msg(sel_item)
                            ok, err = send_telegram(tg_cfg_token, tg_cfg_chat, msg)
                            if ok:
                                st.success("✅ 텔레그램으로 기사를 발송했습니다!")
                            else:
                                st.error(f"발송 실패: {err}")

    # ════════════════════════════════════════════════
    # 탭 3 : 시스템 로그
    # ════════════════════════════════════════════════
    with tab3:
        st.subheader("🖥️ 실시간 시스템 로그")
        st.caption("수집 및 스케줄러 관련 로그가 실시간으로 기록됩니다.")

        log_content = log_stream.getvalue()

        if not log_content:
            st.info("아직 기록된 로그가 없습니다.")
        else:
            lines = log_content.strip().split("\n")
            recent = "\n".join(lines[-100:])
            st.text_area(
                "로그",
                value=recent,
                height=500,
                key="log_area",
                label_visibility="collapsed",
            )

        if st.button("🗑️ 로그 초기화", key="clear_log"):
            log_stream.truncate(0)
            log_stream.seek(0)
            st.rerun()

        st.divider()
        if os.path.exists("error.log"):
            with st.expander("📋 error.log 파일 내용 (에러 전용)"):
                try:
                    with open("error.log", "r", encoding="utf-8") as f:
                        err_log = f.read()
                    if err_log.strip():
                        st.text_area("error.log", value=err_log[-5000:], height=250,
                                     label_visibility="collapsed")
                    else:
                        st.success("에러 로그가 없습니다. 👍")
                except Exception:
                    st.warning("error.log 파일을 읽을 수 없습니다.")


if __name__ == "__main__":
    main()