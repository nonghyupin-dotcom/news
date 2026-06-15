"""
깃허브 액션(GitHub Actions) 전용 무인 자동 뉴스 수집기 (batch_scraper.py)
- 웹 화면(UI) 없이 오직 수집, 요약, 구글 시트 저장, 텔레그램 발송만 수행합니다.
"""

import os
import re
import json
import xml.etree.ElementTree as ET
from urllib.parse import urlparse
from datetime import datetime
from collections import Counter

import requests
import urllib3
import gspread
from google.oauth2.service_account import Credentials
from bs4 import BeautifulSoup

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ==========================================
# ⚙️ 봇 설정 파라미터 (원하는 대로 수정하세요)
# ==========================================
TARGET_KEYWORDS = ["서부발전", "ai", "생성형", "llm"]  # 수집할 키워드 목록
LIMIT_PER_KEYWORD = 5                             # 키워드당 수집할 기사 수
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

def print_log(msg):
    now = datetime.now().strftime("%H:%M:%S")
    print(f"[{now}] {msg}")

# ==========================================
# 1. 구글 시트 연결
# ==========================================
def init_gsheets():
    try:
        # 깃허브 Secrets에서 구글 키를 가져옵니다.
        gcp_key_str = os.environ.get("GCP_KEY_JSON")
        if not gcp_key_str:
            print_log("❌ [오류] 환경변수에 GCP_KEY_JSON이 없습니다.")
            return None

        creds_dict = json.loads(gcp_key_str, strict=False)
        scopes = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        client = gspread.authorize(creds)
        
        sheet = client.open("News_DB")
        news_ws = sheet.worksheet("뉴스DB")
        return news_ws
    except Exception as e:
        print_log(f"❌ 구글 시트 연결 에러: {e}")
        return None

# ==========================================
# 2. 요약 및 크롤링 엔진
# ==========================================
def extract_summary(text: str, num_sentences: int = 2) -> str:
    if not text or len(text.strip()) < 10: return "본문 내용이 너무 짧아 요약할 수 없습니다."
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    sentences = [s.strip() for s in sentences if len(s.strip()) > 5]
    if len(sentences) <= num_sentences: return " ".join(sentences)
        
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

def clean_text(text: str) -> str:
    text = re.sub(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', '', text)
    text = re.sub(r'^\[[^\]]+\]|^\([^\)]+\)|^【[^】]+】', '', text)
    text = re.sub(r'무단\s*전재\s*및\s*재배포\s*금지|저작권자\(c\).*?금지', '', text)
    return ' '.join(text.split()).strip()

def fetch_universal_body(url: str) -> tuple[str, str]:
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    site_name = ""
    try:
        res = requests.get(url, headers=headers, timeout=6, verify=False)
        res.encoding = res.apparent_encoding if res.apparent_encoding else 'utf-8'
        if res.status_code != 200: return "", ""
            
        soup = BeautifulSoup(res.text, 'html.parser')
        meta_site = soup.find('meta', property='og:site_name')
        if meta_site and meta_site.get('content'): site_name = meta_site.get('content').strip()
        
        for tag in soup(['script', 'style', 'iframe', 'noscript', 'header', 'footer', 'nav', 'form', 'aside']):
            tag.decompose()
            
        selectors = ['#newsct_article', '#dic_area', '#articleBodyContents', '#articleBody', '.article_body', '#news_body', 'div.story', 'article', '#article_content']
        for sel in selectors:
            target = soup.select_one(sel)
            if target:
                txt = clean_text(target.get_text(separator=' '))
                if len(txt) > 200: return txt, site_name
                    
        p_tags = soup.find_all(['p', 'div'])
        valid_chunks = [p.get_text().strip() for p in p_tags if not p.find(['p', 'div']) and len(p.get_text().strip()) > 35 and not any(x in p.get_text().strip() for x in ['Copyright', '저작권', '무단전재'])]
        if valid_chunks: return clean_text(" ".join(valid_chunks)), site_name
    except: pass
    return "", site_name

def run_search(keyword: str, limit: int) -> list:
    results = []
    url = f"https://www.bing.com/news/search?q={requests.utils.quote(keyword)}&format=rss&mkt=ko-KR"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        res = requests.get(url, headers=headers, timeout=8, verify=False)
        if res.status_code != 200: return results
        
        root = ET.fromstring(res.text)
        items = root.findall('.//item')
        seen_urls = set()
        for item in items:
            href = item.find('link').text.strip() if item.find('link') is not None else ""
            if not href or href in seen_urls: continue
            seen_urls.add(href)
            
            title = item.find('title').text.strip() if item.find('title') is not None else "제목 없음"
            rss_press = item.find('source').text.strip() if item.find('source') is not None else ""
            
            body, html_press = fetch_universal_body(href)
            if len(body) < 150: continue
            
            press = html_press if html_press else rss_press
            if not press or press == "언론사":
                domain = urlparse(href).netloc
                press = domain.replace("www.", "") if domain else "언론사"
                
            print_log(f"📰 수집: {title[:15]}... ({press})")
            summary = extract_summary(body, 2)
            results.append({"keyword": keyword, "press": press, "title": title, "link": href, "summary": summary, "body_text": body})
            
            if len(results) >= limit: break
    except Exception as e:
        print_log(f"검색 예외: {e}")
    return results

def send_telegram(token, chat_id, text):
    if not token or not chat_id: return
    url = f"https://api.telegram.org/bot{token.strip()}/sendMessage"
    try:
        requests.post(url, json={"chat_id": chat_id.strip(), "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}, timeout=8, verify=False)
    except: pass

# ==========================================
# 3. 메인 파이프라인 (실행부)
# ==========================================
def main():
    print_log("🚀 깃허브 무인 자동 수집 봇 가동 시작!")
    all_news = []
    
    for kw in TARGET_KEYWORDS:
        if not kw.strip(): continue
        print_log(f"🔍 키워드 [{kw}] 수집 중...")
        all_news.extend(run_search(kw, LIMIT_PER_KEYWORD))
        
    if not all_news:
        print_log("❌ 수집된 뉴스가 없습니다. 종료합니다.")
        return
        
    # 중복 제거
    unique_news = {re.sub(r'\s+', '', n['title']): n for n in all_news}.values()
    all_news = list(unique_news)
    
    # 통계 및 요약 텍스트 생성
    kw_counts = Counter([n['keyword'] for n in all_news])
    kw_stat_str = ", ".join([f"'{k}' {v}건" for k, v in kw_counts.items()])
    all_sum_text = " ".join([n['summary'] for n in all_news])
    extracted_sentences = extract_summary(all_sum_text, 4)
    
    summary_msg = (
        f"📰 <b>[새벽 자동 뉴스 수집 완료]</b>\n\n"
        f"📊 <b>총 {len(all_news)}건 수집</b> ({kw_stat_str})\n\n"
        f"💡 <b>[주요 핵심 요약]</b>\n{extracted_sentences}\n\n"
        f"👉 <b>대시보드에서 전체 기사를 확인하세요!</b>"
    )

    # 구글 시트 저장
    news_ws = init_gsheets()
    if news_ws:
        rows_to_insert = []
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for n in all_news:
            rows_to_insert.append([now_str, n['keyword'], n['press'], n['title'], n['link'], n['summary'], n['body_text']])
        if rows_to_insert:
            news_ws.append_rows(rows_to_insert)
            print_log(f"✅ 구글 시트 DB에 {len(rows_to_insert)}건 저장 완료!")
            
            # 텔레그램 발송 (저장 성공 시에만)
            send_telegram(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, summary_msg)
            print_log("✅ 텔레그램 요약본 발송 완료!")
    else:
        print_log("❌ DB 연결 실패로 저장을 취소합니다.")

    print_log("🏁 무인 로봇 작업 정상 종료!")

if __name__ == "__main__":
    main()
