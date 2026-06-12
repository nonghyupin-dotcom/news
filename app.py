"""
자동 뉴스 수집 및 요약기 v3.2 (통계 대시보드 시각화 기능 추가)
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
                target = soup.select_one(
