"""
자동 뉴스 수집 및 요약기 v3.0 (최종 완성판)
- 게시판 헤더 독립형 강제 파싱 적용 (무적 게시판)
- 요약 리포트 기호 버그 수정
- 구글 시트 DB 완벽 연동
"""

import os
import re
import csv
import sys
import json
import time
import threading
import xml.etree.ElementTree as ET
from urllib.parse import urlparse
from datetime import datetime
from collections import Counter

import requests
import schedule
import urllib3
import gspread
from google.oauth2.service_account import Credentials
from bs4 import BeautifulSoup
import streamlit as st
import pandas as pd

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ══════════════════════════════════════════════════════════════
# 1. 영속성 로그 시스템
# ══════════════════════════════════════════════════════════════
if "internal_logs" not in st.session_state:
    st.session_state["internal_logs"] = []

def add_log(message: str, level: str = "INFO"):
    now = datetime.now().strftime("%H:%M:%S")
    log_line = f"[{now}] [{level}] {message}"
    st.session_state["internal_logs"].append(log_line)
    if len(st.session_state["internal_logs"]) > 150:
        st.session_state["internal_logs"].pop(0)

CONFIG_FILE = "config.json"
DEFAULT_CONFIG = {
    "keywords": ["ai", "생성형", "llm"],
    "telegram_token": "",
    "telegram_chat_id": "",
    "limit_per_keyword": 5,
    "schedule_hour": 8,
    "schedule_minute": 0
}

def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                for k, v in DEFAULT_CONFIG.items():
                    cfg.setdefault(k, v)
                return cfg
        except: pass
    return DEFAULT_CONFIG.copy()

def save_config(cfg: dict):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

# ══════════════════════════════════════════════════════════════
# 2. 구글 시트(DB) 연결 매니저
# ══════════════════════════════════════════════════════════════
@st.cache_resource(ttl=600)
def init_gsheets():
    try:
        if "gcp_service_account" not in st.secrets:
            add_log("시크릿 금고에 [gcp_service_account] 키가 없습니다.", "WARNING")
            return None, None
            
        scopes = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
        creds_dict = dict(st.secrets["gcp_service_account"])
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        client = gspread.authorize(creds)
        
        sheet = client.open("News_DB")
        
        news_ws = sheet.worksheet("뉴스DB")
        if not news_ws.get_all_values():
            news_ws.append_row(['수집일시', '키워드', '언론사', '제목', '링크', '요약', '기사원문'])
            
        board_ws = sheet.worksheet("게시판DB")
        if not board_ws.get_all_values():
            board_ws.append_row(['작성일시', '작성자', '내용'])
            
        return news_ws, board_ws
    except Exception as e:
        add_log(f"구글 시트 연동 에러: {e}", "ERROR")
        return None, None

# ══════════════════════════════════════════════════════════════
# 3. 빈도 기반 문장 추출 요약기 (기호 버그 수정)
# ══════════════════════════════════════════════════════════════
def extract_summary(text: str, num_sentences: int = 2) -> str:
    if not text or len(text.strip()) < 10:
        return "본문 내용이 너무 짧아 요약할 수 없습니다."
    
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    sentences = [s.strip() for s in sentences if len(s.strip()) > 5]
    
    if len(sentences) <= num_sentences:
        return " ".join(sentences)
        
    postpositions = ['은', '는', '이', '가', '을', '를', '에', '의', '로', '와', '과', '으로', '에서']
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
    sentence_scores = {}
    for i, sent in enumerate(sentences):
        score = 0
        sw = sent.split()
        if not sw: continue
        for word in sw:
            cw = re.sub(r'[^\w\s]', '', word)
            score += word_counts.get(cw, 0)
        sentence_scores[i] = score / (len(sw) + 2)
        
    top_idx = sorted(sentence_scores, key=sentence_scores.get, reverse=True)[:num_sentences]
    top_idx.sort()
    
    # [v3.0] 이중 기호 방지를 위해 심플한 불릿 포인트(•) 하나만 적용
    return "\n\n".join([f"• {sentences[i]}" for i in top_idx])

# ══════════════════════════════════════════════════════════════
# 4. 빙(Bing) 뉴스 우회 + 유니버설 스크래퍼 엔진
# ══════════════════════════════════════════════════════════════
class UniversalNewsScraper:
    def __init__(self):
        self.headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

    def clean_text(self, text: str) -> str:
        text = re.sub(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', '', text)
        text = re.sub(r'^\[[^\]]+\]|^\([^\)]+\)|^【[^】]+】', '', text)
        text = re.sub(r'무단\s*전재\s*및\s*재배포\s*금지|저작권자\(c\).*?금지', '', text)
        return ' '.join(text.split()).strip()

    def fetch_universal_body(self, url: str) -> tuple[str, str]:
        site_name = ""
        try:
            res = requests.get(url, headers=self.headers, timeout=6, verify=False)
            res.encoding = res.apparent_encoding if res.apparent_encoding else 'utf-8'
            if res.status_code != 200: return "", ""
                
            soup = BeautifulSoup(res.text, 'html.parser')
            meta_site = soup.find('meta', property='og:site_name')
            if meta_site and meta_site.get('content'):
                site_name = meta_site.get('content').strip()
            
            for tag in soup(['script', 'style', 'iframe', 'noscript', 'header', 'footer', 'nav', 'form', 'aside']):
                tag.decompose()
                
            selectors = ['#newsct_article', '#dic_area', '#articleBodyContents', '#articleBody', '.article_body', '#news_body', 'div.story', 'article', '#article_content']
            for sel in selectors:
                target = soup.select_one(sel)
                if target:
                    txt = self.clean_text(target.get_text(separator=' '))
                    if len(txt) > 200: return txt, site_name
                        
            p_tags = soup.find_all(['p', 'div'])
            valid_chunks = [p.get_text().strip() for p in p_tags if not p.find(['p', 'div']) and len(p.get_text().strip()) > 35 and not any(x in p.get_text().strip() for x in ['Copyright', '저작권', '무단전재'])]
            if valid_chunks: return self.clean_text(" ".join(valid_chunks)), site_name
        except: pass
        return "", site_name

    def run_search(self, keyword: str, limit: int) -> list:
        results = []
        url = f"https://www.bing.com/news/search?q={requests.utils.quote(keyword)}&format=rss&mkt=ko-KR"
        try:
            res = requests.get(url, headers=self.headers, timeout=8, verify=False)
            if res.status_code != 200:
                add_log(f"❌ 검색망 접근 실패 (HTTP {res.status_code})", "ERROR")
                return results
            
            root = ET.fromstring(res.text)
            items = root.findall('.//item')
            seen_urls = set()
            for item in items:
                link_node = item.find('link')
                href = link_node.text.strip() if link_node is not None and link_node.text else ""
                if not href or href in seen_urls: continue
                seen_urls.add(href)
                
                title_node = item.find('title')
                title = title_node.text.strip() if title_node is not None and title_node.text else "제목 없음"
                
                source_node = item.find('source')
                rss_press = source_node.text.strip() if source_node is not None and source_node.text else ""
                
                body, html_press = self.fetch_universal_body(href)
                if len(body) < 150: continue
                
                press = html_press if html_press else rss_press
                if not press or press == "언론사":
                    domain = urlparse(href).netloc
                    press = domain.replace("www.", "") if domain else "언론사"
                    
                add_log(f"📰 수집 중: {title[:15]}... ({press})", "INFO")
                
                summary = extract_summary(body, 2)
                results.append({
                    "keyword": keyword, "press": press, "title": title,
                    "link": href, "summary": summary, "body_text": body
                })
                
                if len(results) >= limit: break
        except Exception as e:
            add_log(f"검색 엔진 크롤링 중 예외: {e}", "ERROR")
        return results

# ══════════════════════════════════════════════════════════════
# 5. 파이프라인 관리자
# ══════════════════════════════════════════════════════════════
def start_pipeline(keywords, limit):
    add_log("⚡ 뉴스 수집 종합 파이프라인 시동...")
    scraper = UniversalNewsScraper()
    all_news = []
    
    for kw in keywords:
        kw = kw.strip()
        if not kw: continue
        add_log(f"🚀 키워드 [{kw}] 작업 세션 개시")
        all_news.extend(scraper.run_search(kw, limit))
        
    if not all_news:
        add_log("❌ [에러] 수집된 뉴스가 없습니다.", "ERROR")
        return False
        
    unique_news = {re.sub(r'\s+', '', n['title']): n for n in all_news}.values()
    all_news = list(unique_news)
    
    kw_counts = Counter([n['keyword'] for n in all_news])
    kw_stat_str = ", ".join([f"'{k}' {v}건" for k, v in kw_counts.items()])
    
    all_sum_text = " ".join([n['summary'] for n in all_news])
    extracted_sentences = extract_summary(all_sum_text, 4)
    
    global_summary = (
        f"📊 **수집된 전체 뉴스: 총 {len(all_news)}건**\n"
        f"🏷️ **키워드별 수집량:** {kw_stat_str}\n\n"
        f"💡 **[주요 핵심 문장 추출]**\n{extracted_sentences}"
    )
    st.session_state["global_summary_cache"] = global_summary
    
    try:
        news_ws, _ = init_gsheets()
        if news_ws:
            rows_to_insert = []
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for n in all_news:
                rows_to_insert.append([now_str, n['keyword'], n['press'], n['title'], n['link'], n['summary'], n['body_text']])
            if rows_to_insert:
                news_ws.append_rows(rows_to_insert)
                add_log(f"✅ 구글 시트 DB에 {len(rows_to_insert)}건 영구 저장 완료", "INFO")
        else:
            add_log("❌ DB 연결 실패로 저장을 건너뛰었습니다.", "ERROR")
    except Exception as e:
        add_log(f"구글 시트 저장 중 예기치 않은 오류: {e}", "ERROR")

    add_log(f"🏁 파이프라인 종료!", "INFO")
    return True

def send_telegram(token, chat_id, text) -> bool:
    if not token or not chat_id: return False
    url = f"https://api.telegram.org/bot{token.strip()}/sendMessage"
    try:
        res = requests.post(url, json={"chat_id": chat_id.strip(), "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}, timeout=8, verify=False)
        return res.ok
    except: return False

# ══════════════════════════════════════════════════════════════
# 6. 백그라운드 스케줄러 스레드
# ══════════════════════════════════════════════════════════════
_scheduler_thread = None
_scheduler_stop = threading.Event()

def _scheduler_loop(hour, minute, keywords, limit, token, chat_id):
    schedule.clear()
    def job():
        add_log(f"⏰ 스케줄러 자동 실행 (목표시간 {hour:02d}:{minute:02d})")
        success = start_pipeline(keywords, limit)
        if success and token and chat_id:
            summary_text = st.session_state.get("global_summary_cache", "요약 생성 실패")
            msg = f"📰 <b>[자동 뉴스 수집 완료]</b>\n\n{summary_text}"
            send_telegram(token, chat_id, msg)
            
    schedule.every().day.at(f"{hour:02d}:{minute:02d}").do(job)
    add_log(f"🟢 스케줄러 가동: 매일 {hour:02d}:{minute:02d} 예약됨")
    
    while not _scheduler_stop.is_set():
        schedule.run_pending()
        time.sleep(30)
    add_log("🔴 스케줄러가 중지되었습니다.")

def start_scheduler(hour, minute, keywords, limit, token, chat_id):
    global _scheduler_thread
    _scheduler_stop.clear()
    _scheduler_thread = threading.Thread(target=_scheduler_loop, args=(hour, minute, keywords, limit, token, chat_id), daemon=True)
    _scheduler_thread.start()

def stop_scheduler():
    _scheduler_stop.set()

def scheduler_running():
    return _scheduler_thread is not None and _scheduler_thread.is_alive()

# ══════════════════════════════════════════════════════════════
# 7. Streamlit UI 렌더링 엔진
# ══════════════════════════════════════════════════════════════
def main():
    st.set_page_config(page_title="News Web v3.0", page_icon="📰", layout="centered")
    
    st.markdown("""
    <style>
    .main-box { background-color: #1e293b; padding: 1.5rem; border-radius: 12px; margin-bottom: 1.5rem; color: #f8fafc; }
    .stTabs [data-baseweb="tab"] { font-size: 16px; font-weight: 600; padding: 10px 20px; }
    div.stButton > button { background-color: #2563eb !important; color: white !important; font-weight: 600; border-radius: 6px; }
    .status-badge { display: inline-flex; align-items: center; gap: 0.4rem; padding: 0.3rem 0.8rem; border-radius: 999px; font-size: 0.82rem; font-weight:600; }
    .badge-active   { background: rgba(34,197,94,0.15); color: #4ade80; border: 1px solid rgba(34,197,94,0.4); }
    .badge-inactive { background: rgba(239,68,68,0.12);  color: #f87171; border: 1px solid rgba(239,68,68,0.3); }
    </style>
    """, unsafe_allow_html=True)
    
    st.markdown('<div class="main-box"><h2>📰 AI 뉴스 크롤러 & DB 대시보드 v3.0</h2><p style="color:#94a3b8; margin:0;">구글 시트(DB) 영구 연동 및 실시간 사내 소통 게시판 탑재</p></div>', unsafe_allow_html=True)
    
    cfg = load_config()
    tab1, tab2, tab3, tab4 = st.tabs(["⚙️ 제어 설정", "📄 누적 뉴스 DB", "💬 의견 게시판", "🖥️ 로그"])
    
    with tab1:
        st.subheader("📊 엔진 파라미터 구성")
        kw_str = st.text_input("수집 키워드 (쉼표 구분)", value=", ".join(cfg["keywords"]))
        limit_val = st.number_input("키워드당 목표 수집 수", min_value=1, max_value=20, value=cfg.get("limit_per_keyword", 5))
        
        st.divider()
        st.subheader("🕐 자동 수집 스케줄러")
        c_stat, _ = st.columns(2)
        with c_stat:
            if scheduler_running(): st.markdown('<span class="status-badge badge-active">🟢 스케줄러 가동 중</span>', unsafe_allow_html=True)
            else: st.markdown('<span class="status-badge badge-inactive">🔴 스케줄러 비활성</span>', unsafe_allow_html=True)
                
        col_h, col_m = st.columns(2)
        with col_h: s_hour = st.number_input("시(Hour)", 0, 23, cfg.get("schedule_hour", 8))
        with col_m: s_min  = st.number_input("분(Min)",  0, 59, cfg.get("schedule_minute", 0))

        col_btn1, col_btn2 = st.columns(2)
        with col_btn1:
            if st.button("▶ 스케줄러 시작", use_container_width=True):
                kws = [k.strip() for k in kw_str.split(",") if k.strip()]
                start_scheduler(s_hour, s_min, kws, limit_val, cfg.get("telegram_token", ""), cfg.get("telegram_chat_id", ""))
                st.rerun()
        with col_btn2:
            if st.button("⏹ 스케줄러 중지", use_container_width=True):
                stop_scheduler()
                st.rerun()

        st.divider()
        st.subheader("🤖 텔레그램 연동")
        tg_token = st.text_input("봇 토큰", value=cfg.get("telegram_token", ""), type="password")
        tg_id = st.text_input("Chat ID", value=cfg.get("telegram_chat_id", ""))
        
        if st.button("💾 모든 설정 저장", use_container_width=True):
            cfg.update({"keywords": [k.strip() for k in kw_str.split(",") if k.strip()], "limit_per_keyword": limit_val, "schedule_hour": s_hour, "schedule_minute": s_min, "telegram_token": tg_token, "telegram_chat_id": tg_id})
            save_config(cfg)
            st.success("저장 완료!")
                    
        st.divider()
        if st.button("⚡ 지금 즉시 크롤링 엔진 가동 (DB에 누적)", use_container_width=True):
            st.cache_resource.clear()
            kws = [k.strip() for k in kw_str.split(",") if k.strip()]
            with st.spinner("뉴스 수집 및 구글 시트(DB) 저장 중..."):
                if start_pipeline(kws, limit_val):
                    st.success("수집 및 DB 저장이 완료되었습니다! '누적 뉴스 DB' 탭을 확인하세요.")
                    time.sleep(1)
                    st.rerun()

    with tab2:
        news_ws, _ = init_gsheets()
        if not news_ws:
            st.error("구글 시트 연동 키(Secrets)가 설정되지 않았거나 시트를 찾을 수 없습니다. (로그 탭을 확인하세요)")
        else:
            records = news_ws.get_all_records()
            df = pd.DataFrame(records)
            
            if df.empty:
                st.info("📭 구글 시트(DB)에 누적된 뉴스가 없습니다. 1번 탭에서 엔진을 가동해 주세요.")
            else:
                expected_cols = ['수집일시', '키워드', '언론사', '제목', '링크', '요약', '기사원문']
                for col in expected_cols:
                    if col not in df.columns:
                        df[col] = "데이터없음"
                        
                show_df = df[['수집일시', '키워드', '언론사', '제목']][::-1]
                
                st.markdown(f"### 🗄️ 누적 뉴스 DB (총 {len(records)}건)")
                st.caption("구글 시트에서 실시간으로 데이터를 불러옵니다.")
                st.dataframe(show_df, use_container_width=True, hide_index=True)
                
                st.divider()
                st.markdown("### 📰 개별 뉴스 원문 조회")
                
                sel_titles = [f"[{r['수집일시'][:10]}] [{r['언론사']}] {r['제목']}" for _, r in df[::-1].iterrows()]
                selected = st.selectbox("DB에서 원문을 조회할 기사를 선택하세요.", options=sel_titles)
                
                if selected:
                    idx = sel_titles.index(selected)
                    item = df[::-1].iloc[idx]
                    
                    st.markdown(f"#### {item['제목']}")
                    st.caption(f"📅 수집일시: {item['수집일시']} | 🏢 {item['언론사']} | 🔍 #{item['키워드']}")
                    st.markdown(f"**💡 기사 핵심 요약:**")
                    st.warning(item['요약'])
                    st.markdown("**📄 기사 전체 본문 원문:**")
                    st.text_area("body", value=item['기사원문'], height=300, label_visibility="collapsed")
                    
                    c1, c2 = st.columns(2)
                    with c1:
                        st.link_button("🔗 뉴스 정식 링크 열기", url=item['링크'], use_container_width=True)
                    with c2:
                        if st.button("🚀 이 기사 텔레그램 전송", use_container_width=True):
                            msg = f"📰 <b>{item['제목']}</b>\n🏢 {item['언론사']} | 🔍 #{item['키워드']}\n\n<b>[요약]</b>\n{item['요약']}\n\n🔗 링크: {item['링크']}"
                            if send_telegram(cfg["telegram_token"], cfg["telegram_chat_id"], msg):
                                st.success("전송 완료!")

    with tab3:
        st.markdown("### 💬 사내 뉴스 코멘트 보드")
        st.caption("뉴스를 읽고 팀원들과 의견을 공유하세요. (데이터는 구글 시트에 실시간 저장됩니다)")
        
        _, board_ws = init_gsheets()
        if board_ws:
            with st.form("board_form"):
                col_name, col_desc = st.columns([1, 4])
                with col_name:
                    author = st.text_input("작성자 (이름/직급)")
                with col_desc:
                    content = st.text_input("의견이나 인사이트를 남겨주세요")
                
                if st.form_submit_button("📢 의견 등록하기"):
                    if author and content:
                        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        try:
                            board_ws.append_row([now_str, author, content])
                            st.success("의견이 성공적으로 등록되었습니다!")
                            time.sleep(1)
                            st.rerun()
                        except Exception as e:
                            st.error(f"저장 실패: {e}")
                    else:
                        st.warning("작성자와 내용을 모두 입력해주세요.")
            
            st.divider()
            try:
                # [v3.0] 헤더 이름에 의존하지 않고 값만 무식하게 빼오는 무적 코드 적용!
                board_values = board_ws.get_all_values()
                if len(board_values) > 1: # 헤더 제외하고 작성된 데이터가 있으면
                    for r in reversed(board_values[1:]): # 첫 줄(헤더) 제외하고 역순 출력
                        b_time = r[0] if len(r) > 0 else ""
                        b_author = r[1] if len(r) > 1 and r[1].strip() else "익명"
                        b_content = r[2] if len(r) > 2 else ""
                        
                        st.markdown(f"**👤 {b_author}** <span style='color:gray; font-size:0.85em; margin-left:10px;'>{b_time}</span>", unsafe_allow_html=True)
                        if b_content:
                            st.info(b_content)
                else:
                    st.info("아직 등록된 의견이 없습니다. 첫 번째 의견을 남겨보세요!")
            except Exception as e:
                st.error(f"데이터를 불러오지 못했습니다: {e}")

    with tab4:
        st.subheader("🖥️ 실시간 백엔드 가동 로그")
        if not st.session_state["internal_logs"]:
            st.caption("대기 중... 로그 기록이 없습니다.")
        else:
            log_box = "\n".join(st.session_state["internal_logs"][::-1])
            st.text_area("logs", value=log_box, height=450, label_visibility="collapsed")
            if st.button("🗑️ 로그 초기화"):
                st.session_state["internal_logs"] = []
                st.rerun()

if __name__ == "__main__":
    main()
