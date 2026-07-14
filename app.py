import streamlit as st
import pandas as pd
import sqlite3
import random
import json
import io
import os
import re
from datetime import datetime, timedelta
from google import genai

# 로컬 및 클라우드 환경 테스트 시 HTTPS 오류 우회
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

# --- 구글 로그인(OAuth 2.0) 및 드라이브 라이브러리 ---
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

# 구글 드라이브 파일 접근 권한 범위
SCOPES = ['https://www.googleapis.com/auth/drive']

# --- [페이지 설정] ---
st.set_page_config(page_title="AI 스마트 단어장", page_icon="📖", layout="wide")

# --- [API 키 설정] ---
GEMINI_API_KEY = st.secrets.get("GEMINI_API_KEY", "")

# --- [데이터베이스 설정] ---
conn = sqlite3.connect('vocab_app.db', check_same_thread=False)
c = conn.cursor()
c.execute('''CREATE TABLE IF NOT EXISTS vocab 
             (category TEXT, word TEXT, meaning TEXT, example TEXT, date TEXT, wrong_count INTEGER DEFAULT 0, last_tested TEXT)''')
c.execute('''CREATE TABLE IF NOT EXISTS oauth_store (state TEXT, verifier TEXT)''')
c.execute('''CREATE TABLE IF NOT EXISTS oauth_creds (creds TEXT)''')
conn.commit()

# =======================================================
# 1. 구글 드라이브 OAuth 인증 로직
# =======================================================
def handle_oauth_callback():
    if 'code' in st.query_params and 'state' in st.query_params:
        state = st.query_params['state']
        code = st.query_params['code']
        
        c.execute("SELECT verifier FROM oauth_store WHERE state=?", (state,))
        row = c.fetchone()
        
        if not row:
            st.query_params.clear()
            st.warning("로그인 세션이 만료되었습니다. 데이터 백업 탭에서 다시 시도하십시오.")
            return

        verifier = row[0]
        
        try:
            client_config = json.loads(st.secrets["GOOGLE_CLIENT_CONFIG"])
            flow = Flow.from_client_config(
                client_config,
                scopes=SCOPES,
                redirect_uri=st.secrets["REDIRECT_URI"]
            )
            
            flow.code_verifier = verifier
            flow.fetch_token(code=code)
            creds = flow.credentials
            
            cred_dict = {
                'token': creds.token,
                'refresh_token': creds.refresh_token,
                'token_uri': creds.token_uri,
                'client_id': creds.client_id,
                'client_secret': creds.client_secret,
                'scopes': creds.scopes
            }
            
            c.execute("DELETE FROM oauth_creds")
            c.execute("INSERT INTO oauth_creds VALUES (?)", (json.dumps(cred_dict),))
            c.execute("DELETE FROM oauth_store") 
            conn.commit()
            
            st.query_params.clear()
            st.rerun()
        except Exception as e:
            st.error(f"구글 로그인 인증 오류: {e}")

handle_oauth_callback()

def init_drive_service():
    c.execute("SELECT creds FROM oauth_creds")
    row = c.fetchone()
    if row:
        try:
            cred_dict = json.loads(row[0])
            creds = Credentials.from_authorized_user_info(cred_dict, SCOPES)
            return build('drive', 'v3', credentials=creds)
        except: pass
    return None

def upload_to_google_drive(csv_string):
    service = init_drive_service()
    if not service: raise Exception("구글 드라이브 로그인이 필요합니다.")
    file_name = f"vocab_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    file_metadata = {'name': file_name, 'parents': [st.secrets["GOOGLE_FOLDER_ID"]]}
    csv_bytes = csv_string.encode('utf-8-sig')
    media = MediaIoBaseUpload(io.BytesIO(csv_bytes), mimetype='text/csv', resumable=True)
    file = service.files().create(body=file_metadata, media_body=media, fields='id').execute()
    return file.get('id')

def download_latest_from_google_drive():
    service = init_drive_service()
    if not service: raise Exception("구글 드라이브 로그인이 필요합니다.")
    folder_id = st.secrets["GOOGLE_FOLDER_ID"]
    query = f"'{folder_id}' in parents and mimeType = 'text/csv' and trashed = false"
    results = service.files().list(q=query, orderBy="modifiedTime desc", pageSize=1, fields="files(id, name)").execute()
    files = results.get('files', [])
    if not files: raise Exception("백업된 파일이 없습니다.")
    file_id = files[0]['id']
    content = service.files().get_media(fileId=file_id).execute()
    return content, files[0]['name']

# =======================================================
# 2. 상태 관리 및 메인 UI
# =======================================================
if 'quiz_started' not in st.session_state: st.session_state.quiz_started = False
if 'extracted_df' not in st.session_state: st.session_state.extracted_df = None

st.title("📖 AI 스마트 단어장")
tab1, tab2, tab3, tab4, tab5 = st.tabs(["➕ 단어 추가", "📚 누적 단어 확인", "🎯 실전 퀴즈", "📊 학습 통계", "⚙️ 데이터 백업/복구"])

# --- [탭 1: 단어 추가] ---
with tab1:
    st.subheader("새로운 단어 등록")
    add_mode = st.radio("등록 방식 선택", ["수동 입력", "AI 자동 추출 (영어 지문 입력)"], horizontal=True)
    
    c.execute("SELECT DISTINCT category FROM vocab")
    existing_categories = [row[0] for row in c.fetchall() if row[0]]
    
    col_c1, col_c2 = st.columns(2)
    with col_c1:
        sel_cat = st.selectbox("기존 카테고리 선택", ["(새로 입력)"] + existing_categories)
    with col_c2:
        new_cat = st.text_input("새 카테고리 입력 (선택 시 이 값이 우선 적용됨)")
    
    category = new_cat.strip() if new_cat.strip() else (sel_cat if sel_cat != "(새로 입력)" else "미분류")

    if add_mode == "수동 입력":
        with st.form("manual_input_form"):
            col1, col2 = st.columns(2)
            with col1: word = st.text_input("영단어")
            with col2: meaning = st.text_input("뜻")
            example = st.text_area("예문 (선택 사항)")
            if st.form_submit_button("단어 저장"):
                if word.strip() and meaning.strip():
                    c.execute("INSERT INTO vocab (category, word, meaning, example, date, wrong_count, last_tested) VALUES (?, ?, ?, ?, ?, 0, NULL)", 
                              (category, word.strip(), meaning.strip(), example.strip(), datetime.now().strftime("%Y-%m-%d")))
                    conn.commit()
                    st.success(f"'{word}' 단어가 저장되었습니다.")
                else: st.error("영단어와 뜻을 모두 입력하십시오.")
    else:
        with st.form("ai_extract_form"):
            text_input = st.text_area("영어 지문을 입력하십시오. AI가 핵심 단어 10개를 자동으로 추출합니다.", height=200)
            if st.form_submit_button("🤖 AI 단어 추출"):
                if not text_input.strip(): st.error("지문을 입력하십시오.")
                elif not GEMINI_API_KEY: st.error("Gemini API 키가 설정되지 않았습니다.")
                else:
                    with st.spinner("AI가 단어를 분석 및 추출 중입니다..."):
                        prompt = f"다음 영어 지문에서 학습하기 좋은 핵심 영단어 최대 10개를 추출하십시오. 반드시 아래 JSON 배열 형식으로만 응답하십시오.\n\n지문: {text_input}\n\n출력 형식:\n[{{\"word\": \"apple\", \"meaning\": \"사과\", \"example\": \"I ate an apple.\"}}]"
                        try:
                            client = genai.Client(api_key=GEMINI_API_KEY)
                            response = client.models.generate_content(model='gemini-2.5-flash', contents=prompt)
                            result_text = response.text.strip()
                            if result_text.startswith("
