import streamlit as st
import sqlite3
import json
import random
import pandas as pd
import re
from datetime import datetime
from google import genai
from PIL import Image

# --- 구글 드라이브 연동 라이브러리 ---
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaInMemoryUpload

# --- [보안: 로그인 시스템] ---
def check_password():
    if "pwd" in st.query_params:
        if st.query_params["pwd"] == st.secrets["APP_PASSWORD"]:
            st.session_state["password_correct"] = True

    if st.session_state.get("password_correct", False):
        return True

    st.title("🔒 나만의 단어장 로그인")
    st.warning("⚠️ **경고: 처음에 설정한 비밀번호를 잃어버리면 절대 찾을 수 없습니다. 꼭 기억해 주세요!**")
    
    password = st.text_input("비밀번호를 입력하세요", type="password")
    
    if st.button("접속하기"):
        if password == st.secrets["APP_PASSWORD"]:
            st.session_state["password_correct"] = True
            st.rerun()
        else:
            st.error("❌ 비밀번호가 일치하지 않습니다.")
    return False

if not check_password():
    st.stop()

# --- [구글 드라이브 업로드 함수] ---
def upload_to_google_drive(csv_string):
    creds_info = json.loads(st.secrets["GOOGLE_SERVICE_ACCOUNT"])
    creds = service_account.Credentials.from_service_account_info(creds_info)
    service = build('drive', 'v3', credentials=creds)
    
    file_name = f"vocab_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    file_metadata = {
        'name': file_name,
        'parents': [st.secrets["GOOGLE_FOLDER_ID"]]
    }
    
    media = MediaInMemoryUpload(csv_string.encode('utf-8-sig'), mimetype='text/csv')
    file = service.files().create(body=file_metadata, media_body=media, fields='id').execute()
    return file.get('id')

# 1. 데이터베이스 설정
conn = sqlite3.connect('my_vocab.db', check_same_thread=False)
c = conn.cursor()
c.execute('''CREATE TABLE IF NOT EXISTS vocab 
             (category TEXT, word TEXT, meaning TEXT, example TEXT, date TEXT, wrong_count INTEGER DEFAULT 0)''')
conn.commit()

try:
    c.execute("ALTER TABLE vocab ADD COLUMN wrong_count INTEGER DEFAULT 0")
    conn.commit()
except sqlite3.OperationalError:
    pass

# 세션 상태 초기화
if 'extracted_df' not in st.session_state:
    st.session_state.extracted_df = None
if 'quiz_started' not in st.session_state:
    st.session_state.quiz_started = False
if 'quiz_pool' not in st.session_state:
    st.session_state.quiz_pool = []
if 'current_idx' not in st.session_state:
    st.session_state.current_idx = 0
if 'graded' not in st.session_state:
    st.session_state.graded = False
if 'options' not in st.session_state:
    st.session_state.options = None
if 'options_pairs' not in st.session_state:
    st.session_state.options_pairs = None
if 'last_result' not in st.session_state:
    st.session_state.last_result = None
if 'score' not in st.session_state:
    st.session_state.score = 0
if 'wrong_answers' not in st.session_state:
    st.session_state.wrong_answers = []

# 2. 웹사이트 화면 구성 및 UI 커스텀
st.set_page_config(page_title="단어장 앱", page_icon="📝")

# --- [UI 디자인 커스텀: 폰트 및 버튼 크기 확대 CSS] ---
st.markdown("""
<style>
/* 주관식 텍스트 입력창 글씨 및 높이 확대 */
.stTextInput input {
    font-size: 20px !important;
    padding: 15px !important;
}
/* 클릭 버튼(보기 블록, 정답 확인, 다음 문제 등) 크기 확대 */
.stButton > button, .stDownloadButton > button {
    font-size: 20px !important;
    padding: 15px 30px !important;
    font-weight: bold !important;
}
/* 문제 알림창 텍스트 크기 확대 */
.stAlert p {
    font-size: 20px !important;
}
/* 아코디언(예문 보기) 텍스트 크기 확대 */
.stExpander p {
    font-size: 18px !important;
}
</style>
""", unsafe_allow_html=True)

st.title("단어장 앱")

c.execute("SELECT COUNT(*) FROM vocab")
total_vocab_count = c.fetchone()[0]
st.info(f"현재 데이터베이스에 등록된 총 영단어 수: **{total_vocab_count}개**")

tab1, tab2, tab3, tab4 = st.tabs(["단어장 생성", "누적 단어 확인", "실전 퀴즈", "데이터 백업/복구"])

API_KEY = st.secrets["GEMINI_API_KEY"]

# --- [탭 1: 단어장 생성] ---
with tab1:
    st.subheader("사진 또는 PDF로 단어 추가")
    
    c.execute("SELECT DISTINCT category FROM vocab")
    db_categories = [row[0] for row in c.fetchall() if row[0]]
    
    base_categories = ["토플 영단어", "경제학 용어", "ETC"]
    for cat in db_categories:
        if cat not in base_categories:
            base_categories.append(cat)
            
    base_categories.append("직접 입력")
    
    category = st.selectbox("카테고리 선택", base_categories)
    if category == "직접 입력":
        category = st.text_input("새 카테고리 이름 입력")

    uploaded_files = st.file_uploader("단어가 있는 사진 또는 PDF 파일 업로드 (복수 선택 가능)", 
                                      type=["jpg", "png", "jpeg", "pdf"], 
                                      accept_multiple_files=True)
    
    if uploaded_files:
        st.write(f"📎 총 {len(uploaded_files)}개의 파일이 선택되었습니다.")
        
        if st.button("AI 분석 실행"):
            if not API_KEY:
                st.error("API 키 오류")
            else:
                with st.spinner('모든 파일을 통합 분석 중입니다...'):
                    try:
                        from google.genai import types
                        client = genai.Client(api_key=API_KEY)
                        
                        contents = []
                        for f in uploaded_files:
                            file_bytes = f.read()
                            contents.append(types.Part.from_bytes(data=file_bytes, mime_type=f.type))
                        
                        prompt = """
                        업로드된 이미지 또는 PDF 문서들에서 중요한 영어 단어들을 모두 추출해주세요. 
                        여러 개의 파일이나 페이지가 제공된 경우, 누락 없이 모든 파일의 단어를 하나로 합쳐서 추출해야 합니다.
                        반드시 아래 형태의 JSON 배열로만 답변해야 합니다:
                        [
                          {"word": "단어", "meaning": "한국어 뜻", "example": "영어 예문"}
                        ]
                        """
                        contents.append(prompt)
                        
                        response = client.models.generate_content(
                            model='gemini-2.5-flash',
                            contents=contents
                        )
                        
                        result_text = response.text.strip()
                        if result_text.startswith("```json"):
                            result_text = result_text[7:-3]
                        elif result_text.startswith("```"):
                            result_text = result_text[3:-3]
                            
                        vocab_list = json.loads(result_text)
                        st.session_state.extracted_df = pd.DataFrame(vocab_list)
                        
                    except Exception as e:
                        st.error(f"오류 발생: {e}")

        if st.session_state.extracted_df is not None:
            st.warning("데이터 검수: 잘못 추출된 데이터가 있다면 표를 클릭하여 직접 수정하십시오.")
            
            edited_df = st.data_editor(st.session_state.extracted_df, use_container_width=True, num_rows="dynamic")
            
            if st.button("검수 완료 및 데이터베이스 저장"):
                today = datetime.now().strftime("%Y-%m-%d")
                
                for index, row in edited_df.iterrows():
                    c.execute("INSERT INTO vocab (category, word, meaning, example, date, wrong_count) VALUES (?, ?, ?, ?, ?, 0)", 
                              (category, row['word'], row['meaning'], row['example'], today))
                conn.commit()
                
                st.success(f"{len(edited_df)}개의 단어 저장 완료")
                st.session_state.extracted_df = None
                st.rerun()

# --- [탭 2: 누적 단어 확인] ---
with tab2:
    st.subheader("데이터베이스 목록 및 관리")
    
    view_mode = st.radio("필터 옵션", ["전체 단어 보기", "틀린 단어(오답)만 카테고리별로 모아보기"], horizontal=True, key="view_mode")
    view_style = st.radio("표시 방식", ["표(엑셀) 형식으로 수정/관리", "플래시카드 형식으로 예문과 함께 학습"], horizontal=True, key="view_style")
    st.divider()
    
    c.execute("SELECT DISTINCT category FROM vocab")
    categories = [row[0] for row in c.fetchall() if row[0]]
    
    if categories:
        selected_category = st.selectbox("카테고리 조회", ["전체"] + categories, key="view_cat")
        
        if selected_category != "전체":
            st.markdown(f"### ⚠️ 카테고리 관리")
            if st.button(f"🔥 '{selected_category}' 카테고리 및 내부 단어 일괄 삭제", key="del_cat_btn"):
                c.execute("DELETE FROM vocab WHERE category=?", (selected_category,))
                conn.commit()
                st.success(f"'{selected_category}' 카테고리와 그 안의 모든 단어가 삭제되었습니다.")
                st.rerun
