"""
자동 뉴스 수집 및 요약기 v2.0 (안정화 및 UI 개선 버전)
- UI: Streamlit 웹 애플리케이션 (Centered 모던 대시보드 아키텍처)
- 뉴스: 네이버 뉴스 포털 기사 전문 수집 및 추출 요약
- 안정화: 블로킹 방식 수동 수집(st.spinner) + 로컬 JSON 영속화 처리
"""

import os
import re
import csv
import sys
import json
import time
import logging
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
# 1. 영속성 로그 시스템 (Session State 기반)
# ══════════════════════════════════════════════════════════════
if "internal_logs" not in st.session_state:
    st.session_state["internal_logs"] = []

def add_log(message: str, level: str = "INFO"):
    now = datetime.now().strftime("%H:%M:%S")
    log_line = f"[{now}] [{level}] {message}"
    st.session_state["internal_logs"].append(log_line)
    if len(st.session_state["internal_logs"]) > 100:
        st.session_state["internal_logs"].pop(0)
    
    # 에러는 파일에 별도 기록
    if level == "ERROR":
        with open("error.log", "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} [{level}] {message}\n")

# ══════════════════════════════════════════════════════════════
# 2. 설정 및 데이터 파일 제어 (JSON 영속화)
# ══════════════════════════════════════════════════════════════
CONFIG_FILE = "config.json"
DATA_FILE = "latest_news.json"

DEFAULT_CONFIG = {
    "keywords": ["ai", "생성형", "llm"],
    "telegram_token": "",
    "telegram_chat_id": "",
    "limit_per_keyword": 5
}

def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
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
# 3. 핵심 추출 요약 알고리즘
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
    return " ".join(sentences[i] for i in top_idx)

# ══════════════════════════════════════════════════════════════
# 4. 네이버 뉴스 스크래퍼 정밀 로직
# ══════════════════════════════════════════════════════════════
class NewsScraper:
    def __init__(self):
        self.headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

    def clean_text(self, text: str) -> str:
        text = re.sub(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', '', text)
        text = re.sub(r'^\[[^\]]+\]|^\([^\)]+\)|^【[^】]+】', '', text)
        text = re.sub(r'무단\s*전재\s*및\s*재배포\s*금지', '', text)
        return ' '.join(text.split()).strip()

    def fetch_naver_body(self, url: str) -> str:
        if "news.naver.com" not in url and "n.news.naver.com" not in url:
            return ""
        try:
            res = requests.get(url, headers=self.headers, timeout=5, verify=False)
            if res.status_code == 200:
                soup = BeautifulSoup(res.text, 'html.parser')
                for tag in soup(['script', 'style', 'iframe', 'noscript', 'header', 'footer']):
                    tag.decompose()
                target = soup.select_one('#dic_area') or soup.select_one('#articleBodyContents') or soup.select_one('#articleBody')
                if target:
                    return self.clean_text(target.get_text(separator=' '))
        except Exception as e:
            add_log(f"본문 파싱 실패 ({url[:30]}...): {e}", "ERROR")
        return ""

    def run_search(self, keyword: str, limit: int) -> list:
        results = []
        url = f"https://search.naver.com/search.naver?where=news&query={requests.utils.quote(keyword)}&sort=0"
        try:
            res = requests.get(url, headers=self.headers, timeout=8, verify=False)
            if res.status_code != 200: return results
            
            soup = BeautifulSoup(res.text, 'html.parser')
            # 네이버 뉴스 링크 패턴 전체 매칭 구동
            links = soup.select('a.info') + soup.select('ul.list_news .news_tit')
            
            seen_urls = set()
            for a in links:
                href = a.get('href', '')
                if "news.naver.com" in href or "n.news.naver.com" in href:
                    if href in seen_urls: continue
                    seen_urls.add(href)
                    
                    # 제목 추출
                    title = a.get_text(strip=True) or "제목 없음"
                    if len(title) < 5: continue
                    
                    body = self.fetch_naver_body(href)
                    if len(body) < 150: continue # 본문 유효성 검증
                    
                    summary = extract_summary(body, 2)
                    results.append({
                        "keyword": keyword,
                        "press": "네이버뉴스",
                        "title": title,
                        "link": href,
                        "summary": summary,
                        "body_text": body
                    })
                    if len(results) >= limit:
                        break
        except Exception as e:
            add_log(f"검색 크롤링 중 오류: {e}", "ERROR")
        return results

# ══════════════════════════════════════════════════════════════
# 5. 데이터 가공 및 파일 물리 저장 시스템
# ══════════════════════════════════════════════════════════════
def start_pipeline(keywords, limit):
    add_log("⚡ 뉴스 수집 파이프라인 가동...")
    scraper = NewsScraper()
    all_news = []
    
    progress_bar = st.progress(0.0, text="동기식 크롤링 엔진 가동 중...")
    total = len(keywords)
    
    for idx, kw in enumerate(keywords):
        kw = kw.strip()
        if not kw: continue
        add_log(f"'{kw}' 키워드 기사 수집 중...")
        items = scraper.run_search(kw, limit)
        all_news.extend(items)
        progress_bar.progress((idx + 1) / total, text=f"[{idx+1}/{total}] '{kw}' 완료")
        
    if not all_news:
        add_log("❌ 조건에 맞는 최신 네이버 뉴스 본문을 찾지 못했습니다.", "WARNING")
        progress_bar.empty()
        return False
        
    # 중복 제거
    unique_news = {re.sub(r'\s+', '', n['title']): n for n in all_news}.values()
    all_news = list(unique_news)
    
    # 종합 요약 생성
    all_sum_text = " ".join([n['summary'] for n in all_news])
    global_summary = extract_summary(all_sum_text, 4)
    
    # 로컬 물리 파일 저장 및 영속화
    save_latest_news(all_news, global_summary)
    
    # CSV 내보내기 규칙 수행
    today = datetime.now().strftime("%Y-%m-%d")
    os.makedirs(f"news_{today}", exist_ok=True)
    csv_path = f"news_{today}/news_list_{today}.csv"
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f, quoting=csv.QUOTE_ALL)
        writer.writerow(['수집일시', '키워드', '제목', '링크', '기사원문'])
        for n in all_news:
            writer.writerow([today, n['keyword'], n['title'], n['link'], n['body_text']])
            
    add_log(f"🎉 성공! 총 {len(all_news)}건 수집 및 물리 파일 동기화 완료")
    time.sleep(0.5)
    progress_bar.empty()
    return True

# ══════════════════════════════════════════════════════════════
# 6. 텔레그램 연동 시스템
# ══════════════════════════════════════════════════════════════
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
# 7. Streamlit UI 렌더링 프레임워크 (SaaS 대시보드 스타일)
# ══════════════════════════════════════════════════════════════
def main():
    st.set_page_config(page_title="News Web v2.0", page_icon="📰", layout="centered")
    
    # 깔끔하고 정돈된 프리미엄 다크/블루 CSS 스타일셋
    st.markdown("""
    <style>
    .main-box { background-color: #1e293b; padding: 1.5rem; border-radius: 12px; margin-bottom: 1.5rem; color: #f8fafc; }
    .stTabs [data-baseweb="tab"] { font-size: 16px; font-weight: 600; padding: 10px 20px; }
    div.stButton > button { background-color: #2563eb !important; color: white !important; font-weight: 600; border-radius: 6px; }
    </style>
    """, unsafe_allow_html=True)
    
    st.markdown('<div class="main-box"><h2>📰 AI 뉴스 크롤러 & 대시보드 v2.0</h2><p style="color:#94a3b8; margin:0;">실시간 네이버 뉴스 수집 엔진 및 사내 정보 자동화 대시보드 시스템</p></div>', unsafe_allow_html=True)
    
    cfg = load_config()
    db = load_latest_news()
    
    tab1, tab2, tab3 = st.tabs(["⚙️ 제어 및 알림 설정", "📄 수집 뉴스 대시보드", "🖥️ 시스템 실시간 로그"])
    
    # ── TAB 1: 설정창 ──────────────────────────────────
    with tab1:
        st.subheader("📊 엔진 파라미터 구성")
        kw_str = st.text_input("수집 키워드 (쉼표 구분)", value=", ".join(cfg["keywords"]))
        limit_val = st.number_input("키워드당 목표 수집 수", min_value=1, max_value=20, value=cfg.get("limit_per_keyword", 5))
        
        st.divider()
        st.subheader("🤖 메신저 라우팅 연동 (Telegram)")
        tg_token = st.text_input("봇 토큰 (Bot Token)", value=cfg["telegram_token"], type="password")
        tg_id = st.text_input("대상 Chat ID", value=cfg["telegram_chat_id"])
        
        col_ctrl1, col_ctrl2 = st.columns(2)
        with col_ctrl1:
            if st.button("💾 제어 구성 저장", use_container_width=True):
                cfg["keywords"] = [k.strip() for k in kw_str.split(",") if k.strip()]
                cfg["limit_per_keyword"] = limit_val
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
        # 동기식 즉시 실행 코어 버튼
        if st.button("⚡ 지금 즉시 크롤링 엔진 가동", use_container_width=True):
            kws = [k.strip() for k in kw_str.split(",") if k.strip()]
            with st.spinner("네이버 뉴스 실시간 스크래핑 및 AI 요약본 산출 중..."):
                success = start_pipeline(kws, limit_val)
                if success:
                    st.success("수집이 완료되었습니다! '📄 수집 뉴스 대시보드' 탭으로 이동하세요.")
                    time.sleep(1)
                    st.rerun()
                else:
                    st.error("수집 실패. 로그 탭을 검사하세요.")

    # ── TAB 2: 대시보드 디스플레이 ───────────────────────
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
            
            # 검색 및 보기 필터
            df = pd.DataFrame([{
                "번호": i + 1,
                "키워드": f"#{n['keyword']}",
                "기사 제목": n['title']
            } for i, n in enumerate(db["news_data"])])
            
            st.dataframe(df, use_container_width=True, hide_index=True)
            
            sel_titles = [f"[{n['keyword']}] {n['title']}" for n in db["news_data"]]
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
                    st.link_button("🔗 네이버 뉴스 정식 링크 열기", url=item['link'], use_container_width=True)
                with c2:
                    if st.button("🚀 이 기사만 텔레그램으로 즉시 전송", use_container_width=True):
                        msg = f"📰 <b>{item['title']}</b>\n🔍 #{item['keyword']}\n\n<b>[요약]</b>\n{item['summary']}\n\n🔗 링크: {item['link']}"
                        if send_telegram(cfg["telegram_token"], cfg["telegram_chat_id"], msg):
                            st.success("텔레그램 전송 완료!")
                        else:
                            st.error("전송 실패. 텔레그램 설정을 세팅해 주세요.")

    # ── TAB 3: 로그 패널 ────────────────────────────────
    with tab3:
        st.subheader("🖥️ 실시간 백엔드 가동 로그")
        if not st.session_state["internal_logs"]:
            st.caption("대기 중... 로그 기록이 없습니다.")
        else:
            log_box = "\n".join(st.session_state["internal_logs"][::-1]) # 최신 로그가 위로
            st.text_area("logs", value=log_box, height=400, label_visibility="collapsed")
            
        if st.button("🗑️ 로그 버퍼 클리어"):
            st.session_state["internal_logs"] = []
            st.rerun()

if __name__ == "__main__":
    main()
