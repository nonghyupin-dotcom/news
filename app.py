"""
자동 뉴스 수집 및 요약기 v2.6 (언론사명 추출 고도화)
- UI: Streamlit 웹 애플리케이션 (Centered 모던 대시보드)
- 뉴스: Bing RSS + 언론사 고유 메타태그(og:site_name) 기반 프레스명 정확도 100% 추출
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
from bs4 import BeautifulSoup
import streamlit as st
import pandas as pd

# SSL 경고 억제
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

# ══════════════════════════════════════════════════════════════
# 2. 파일 영속성 시스템
# ══════════════════════════════════════════════════════════════
CONFIG_FILE = "config.json"
DATA_FILE = "latest_news.json"

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

def save_latest_news(news_data, global_summary):
    payload = {
        "last_collected": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "global_summary": global_summary,
        "news_data": news_data
    }
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

def load_latest_news() -> dict:
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except: pass
    return {"last_collected": "", "global_summary": "", "news_data": []}

# ══════════════════════════════════════════════════════════════
# 3. 빈도 기반 문장 추출 요약기
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
    return "\n\n".join([f"✔️ {sentences[i]}" for i in top_idx])

# ══════════════════════════════════════════════════════════════
# 4. 빙(Bing) 뉴스 우회 + 유니버설 스크래퍼 엔진
# ══════════════════════════════════════════════════════════════
class UniversalNewsScraper:
    def __init__(self):
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        }

    def clean_text(self, text: str) -> str:
        text = re.sub(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', '', text)
        text = re.sub(r'^\[[^\]]+\]|^\([^\)]+\)|^【[^】]+】', '', text)
        text = re.sub(r'무단\s*전재\s*및\s*재배포\s*금지', '', text)
        text = re.sub(r'저작권자\(c\).*?금지', '', text)
        return ' '.join(text.split()).strip()

    def fetch_universal_body(self, url: str) -> tuple[str, str]:
        """본문 텍스트와 함께 메타태그 기반의 실제 언론사명(press_name)을 추출하여 튜플로 반환"""
        site_name = ""
        try:
            res = requests.get(url, headers=self.headers, timeout=6, verify=False)
            res.encoding = res.apparent_encoding if res.apparent_encoding else 'utf-8'
            
            if res.status_code != 200: return "", ""
                
            soup = BeautifulSoup(res.text, 'html.parser')
            
            # [v2.6 기능] og:site_name 메타 태그에서 정확한 한국어 언론사명 추출
            meta_site = soup.find('meta', property='og:site_name')
            if meta_site and meta_site.get('content'):
                site_name = meta_site.get('content').strip()
            
            for tag in soup(['script', 'style', 'iframe', 'noscript', 'header', 'footer', 'nav', 'form', 'aside']):
                tag.decompose()
                
            selectors = [
                '#newsct_article', '#dic_area', '#articleBodyContents', '#articleBody', 
                '#articleBodyBody', '.article_body', '.article-body', '#article_body', 
                '#news_body', 'div.story', 'article', '.story', '#article_content', '.article_cc'
            ]
            for sel in selectors:
                target = soup.select_one(sel)
                if target:
                    txt = self.clean_text(target.get_text(separator=' '))
                    if len(txt) > 200: return txt, site_name
                        
            p_tags = soup.find_all(['p', 'div'])
            valid_chunks = []
            for p in p_tags:
                if p.find(['p', 'div']): continue
                p_txt = p.get_text().strip()
                if len(p_txt) > 35 and not any(x in p_txt for x in ['Copyright', '저작권', '기자', '무단전재', '댓글']):
                    valid_chunks.append(p_txt)
            
            if valid_chunks:
                return self.clean_text(" ".join(valid_chunks)), site_name
        except:
            pass
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
            add_log(f"🔍 '{keyword}' 관련 최신 기사 {len(items)}개 포착 완료", "INFO")
            
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
                
                # 본문 추출 및 HTML 메타태그 기반 언론사명 반환
                body, html_press = self.fetch_universal_body(href)
                if len(body) < 150: continue
                
                # [v2.6 로직] 언론사명 우선순위: 1. 본문 메타태그 -> 2. RSS source -> 3. URL 도메인명 추출
                press = html_press if html_press else rss_press
                if not press or press == "언론사":
                    domain = urlparse(href).netloc
                    press = domain.replace("www.", "") if domain else "언론사"
                    
                add_log(f"📰 수집 중: {title[:15]}... ({press})", "INFO")
                
                summary = extract_summary(body, 2)
                results.append({
                    "keyword": keyword,
                    "press": press,
                    "title": title,
                    "link": href,
                    "summary": summary,
                    "body_text": body
                })
                
                if len(results) >= limit:
                    break
        except Exception as e:
            add_log(f"검색 엔진 크롤링 중 예외 발생: {e}", "ERROR")
        return results

# ══════════════════════════════════════════════════════════════
# 5. 파이프라인 관리자
# ══════════════════════════════════════════════════════════════
def start_pipeline(keywords, limit):
    add_log("⚡ 뉴스 수집 종합 파이프라인 시동...")
    scraper = UniversalNewsScraper()
    all_news = []
    
    total = len(keywords)
    
    for idx, kw in enumerate(keywords):
        kw = kw.strip()
        if not kw: continue
        add_log(f"🚀 키워드 [{kw}] 작업 세션 개시")
        items = scraper.run_search(kw, limit)
        all_news.extend(items)
        
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
        f"📊 **오늘 수집된 전체 뉴스: 총 {len(all_news)}건**\n"
        f"🏷️ **키워드별 수집량:** {kw_stat_str}\n\n"
        f"💡 **[주요 핵심 문장 추출]**\n{extracted_sentences}"
    )
    
    save_latest_news(all_news, global_summary)
    
    today = datetime.now().strftime("%Y-%m-%d")
    os.makedirs(f"news_{today}", exist_ok=True)
    csv_path = f"news_{today}/news_list_{today}.csv"
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f, quoting=csv.QUOTE_ALL)
        writer.writerow(['수집일시', '키워드', '언론사', '제목', '링크', '기사원문'])
        for n in all_news:
            writer.writerow([today, n['keyword'], n['press'], n['title'], n['link'], n['body_text']])
            
    add_log(f"🏁 파이프라인 종료! 총 {len(all_news)}건 저장 완료")
    return True

def send_telegram(token, chat_id, text) -> bool:
    if not token or not chat_id: return False
    url = f"https://api.telegram.org/bot{token.strip()}/sendMessage"
    try:
        res = requests.post(url, json={
            "chat_id": chat_id.strip(),
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }, timeout=8, verify=False)
        return res.ok
    except:
        return False

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
            db = load_latest_news()
            msg = f"📰 <b>[자동 뉴스 수집 완료]</b>\n\n{db['global_summary']}"
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
    _scheduler_thread = threading.Thread(
        target=_scheduler_loop,
        args=(hour, minute, keywords, limit, token, chat_id),
        daemon=True
    )
    _scheduler_thread.start()

def stop_scheduler():
    _scheduler_stop.set()

def scheduler_running():
    return _scheduler_thread is not None and _scheduler_thread.is_alive()

# ══════════════════════════════════════════════════════════════
# 7. Streamlit UI 렌더링 엔진
# ══════════════════════════════════════════════════════════════
def main():
    st.set_page_config(page_title="News Web v2.6", page_icon="📰", layout="centered")
    
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
    
    st.markdown('<div class="main-box"><h2>📰 AI 뉴스 크롤러 & 대시보드 v2.6</h2><p style="color:#94a3b8; margin:0;">언론사명 메타태그 정밀 파싱 지원 · 사내 자동화 대시보드 시스템</p></div>', unsafe_allow_html=True)
    
    cfg = load_config()
    db = load_latest_news()
    
    tab1, tab2, tab3 = st.tabs(["⚙️ 제어 및 알림 설정", "📄 수집 뉴스 대시보드", "🖥️ 시스템 실시간 로그"])
    
    with tab1:
        st.subheader("📊 엔진 파라미터 구성")
        kw_str = st.text_input("수집 키워드 (쉼표 구분)", value=", ".join(cfg["keywords"]))
        limit_val = st.number_input("키워드당 목표 수집 수", min_value=1, max_value=20, value=cfg.get("limit_per_keyword", 5))
        
        st.divider()
        st.subheader("🕐 자동 수집 스케줄러 (매일)")
        
        c_stat, _ = st.columns(2)
        with c_stat:
            if scheduler_running():
                st.markdown('<span class="status-badge badge-active">🟢 스케줄러 감시 중</span>', unsafe_allow_html=True)
            else:
                st.markdown('<span class="status-badge badge-inactive">🔴 스케줄러 비활성</span>', unsafe_allow_html=True)
                
        col_h, col_m = st.columns(2)
        with col_h:
            s_hour = st.number_input("시(Hour)", 0, 23, cfg.get("schedule_hour", 8))
        with col_m:
            s_min  = st.number_input("분(Min)",  0, 59, cfg.get("schedule_minute", 0))

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
        st.subheader("🤖 메신저 라우팅 연동 (Telegram)")
        tg_token = st.text_input("봇 토큰 (Bot Token)", value=cfg.get("telegram_token", ""), type="password")
        tg_id = st.text_input("대상 Chat ID", value=cfg.get("telegram_chat_id", ""))
        
        col_ctrl1, col_ctrl2 = st.columns(2)
        with col_ctrl1:
            if st.button("💾 모든 설정 저장", use_container_width=True):
                cfg["keywords"] = [k.strip() for k in kw_str.split(",") if k.strip()]
                cfg["limit_per_keyword"] = limit_val
                cfg["schedule_hour"] = s_hour
                cfg["schedule_minute"] = s_min
                cfg["telegram_token"] = tg_token
                cfg["telegram_chat_id"] = tg_id
                save_config(cfg)
                st.success("시스템 구성 파일 업데이트 완료!")
                
        with col_ctrl2:
            if st.button("📡 연동 회선 테스트", use_container_width=True):
                if send_telegram(tg_token, tg_id, "🤖 <b>알림:</b> 뉴스 수집기 원격 라우팅 채널이 활성화되었습니다."):
                    st.success("텔레그램 발송 성공!")
                else:
                    st.error("발송 실패. 토큰 또는 ID를 체크하세요.")
                    
        st.divider()
        if st.button("⚡ 지금 즉시 크롤링 엔진 가동", use_container_width=True):
            kws = [k.strip() for k in kw_str.split(",") if k.strip()]
            with st.spinner("언론사별 메타태그 파싱 및 AI 요약본 산출 중..."):
                success = start_pipeline(kws, limit_val)
                if success:
                    st.success("수집이 완료되었습니다! '📄 수집 뉴스 대시보드' 탭으로 이동하세요.")
                    time.sleep(1)
                    st.rerun()
                else:
                    st.error("수집 실패. 로그 탭에서 원인을 점검하세요.")

    with tab2:
        if not db["news_data"]:
            st.info("📭 현재 보관된 로컬 뉴스 데이터가 없습니다. 제어 설정 탭에서 엔진을 가동해 주세요.")
        else:
            st.metric(label="마지막 동기화 시각", value=db["last_collected"])
            
            if db["global_summary"]:
                st.markdown("### 📋 핵심 종합 리포트")
                st.info(db["global_summary"])
                
            st.divider()
            st.markdown("### 📰 개별 뉴스 상세 탐색")
            
            df = pd.DataFrame([{
                "번호": i + 1,
                "언론사": n['press'],
                "키워드": f"#{n['keyword']}",
                "기사 제목": n['title']
            } for i, n in enumerate(db["news_data"])])
            
            st.dataframe(df, use_container_width=True, hide_index=True)
            
            sel_titles = [f"[{n['press']}] {n['title']}" for n in db["news_data"]]
            selected = st.selectbox("본문을 확인할 기사를 선택하세요.", options=sel_titles)
            
            if selected:
                idx = sel_titles.index(selected)
                item = db["news_data"][idx]
                
                st.markdown(f"#### {item['title']}")
                st.markdown(f"**💡 기사 핵심 요약:**")
                st.warning(item['summary'])
                
                st.markdown("**📄 기사 전체 본문 원문:**")
                st.text_area("body", value=item['body_text'], height=300, label_visibility="collapsed")
                
                c1, c2 = st.columns(2)
                with c1:
                    st.link_button("🔗 뉴스 정식 링크 열기", url=item['link'], use_container_width=True)
                with c2:
                    if st.button("🚀 이 기사만 텔레그램으로 즉시 전송", use_container_width=True):
                        msg = f"📰 <b>{item['title']}</b>\n🏢 {item['press']} | 🔍 #{item['keyword']}\n\n<b>[요약]</b>\n{item['summary']}\n\n🔗 링크: {item['link']}"
                        if send_telegram(cfg["telegram_token"], cfg["telegram_chat_id"], msg):
                            st.success("텔레그램 전송 완료!")
                        else:
                            st.error("전송 실패. 텔레그램 설정을 세팅해 주세요.")

    with tab3:
        st.subheader("🖥️ 실시간 백엔드 가동 로그")
        if not st.session_state["internal_logs"]:
            st.caption("대기 중... 로그 기록이 없습니다.")
        else:
            log_box = "\n".join(st.session_state["internal_logs"][::-1])
            st.text_area("logs", value=log_box, height=450, label_visibility="collapsed")
            
        if st.button("🗑️ 로그 버퍼 클리어"):
            st.session_state["internal_logs"] = []
            st.rerun()

if __name__ == "__main__":
    main()
