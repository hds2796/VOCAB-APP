import streamlit as st
import pandas as pd
import sqlite3
import random
import json
import io
import os
import re
import PyPDF2
from PIL import Image
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
st.set_page_config(page_title="Project1_VOCAB", page_icon="📖", layout="wide")

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

st.title("📖 Project1_VOCAB")
tab1, tab2, tab3, tab4, tab5 = st.tabs(["➕ 단어 추가", "📚 누적 단어 확인", "🎯 실전 퀴즈", "📊 학습 통계", "⚙️ 데이터 백업/복구"])

# --- [탭 1: 단어 추가] ---
with tab1:
    st.subheader("새로운 단어 등록")
    add_mode = st.radio("등록 방식 선택", ["수동 입력", "AI 자동 추출 (영어 지문 입력)", "AI 자동 추출 (이미지/PDF 파일 업로드)"], horizontal=True)
    
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
                
    elif add_mode == "AI 자동 추출 (영어 지문 입력)":
        with st.form("ai_text_extract_form"):
            text_input = st.text_area("영어 지문 또는 단어 목록을 입력하십시오.", height=200)
            if st.form_submit_button("🤖 텍스트에서 단어 추출"):
                if not text_input.strip(): st.error("데이터를 입력하십시오.")
                elif not GEMINI_API_KEY: st.error("Gemini API 키가 설정되지 않았습니다.")
                else:
                    with st.spinner("AI가 단어를 분석 및 추출 중입니다..."):
                        prompt = f"""다음 데이터를 분석하여 단어를 추출하십시오. 
조건 1: 만약 제공된 텍스트가 '단어장(단어와 뜻이 나열된 목록)' 형태라면, 목록에 있는 **모든 단어**를 개수 제한 없이 누락 없이 100% 추출하십시오.
조건 2: 만약 일반적인 영어 지문(기사, 문단 등)이라면, 전체 내용에서 학습할 가치가 있는 **핵심 영단어 모두**를 개수 제한 없이 추출하십시오.
반드시 아래 JSON 배열 형식으로만 응답하십시오.

데이터: {text_input}

출력 형식:
[{{\"word\": \"apple\", \"meaning\": \"사과\", \"example\": \"I ate an apple.\"}}]"""
                        try:
                            client = genai.Client(api_key=GEMINI_API_KEY)
                            response = client.models.generate_content(model='gemini-3.1-flash-lite', contents=prompt)
                            result_text = response.text.strip().replace("```json", "").replace("```", "").strip()
                            st.session_state.extracted_df = pd.DataFrame(json.loads(result_text))
                        except Exception as e: st.error(f"오류 발생: {e}")
                        
    elif add_mode == "AI 자동 추출 (이미지/PDF 파일 업로드)":
        with st.form("ai_file_extract_form"):
            # 여러 파일 동시 업로드 허용
            uploaded_files = st.file_uploader("단어장 사진(JPG, PNG)이나 영어 지문 PDF를 업로드하십시오. (여러 개 선택 가능)", type=["png", "jpg", "jpeg", "pdf"], accept_multiple_files=True)
            if st.form_submit_button("🤖 파일에서 단어 추출"):
                if not uploaded_files:
                    st.error("파일을 업로드하십시오.")
                elif not GEMINI_API_KEY:
                    st.error("Gemini API 키가 설정되지 않았습니다.")
                else:
                    with st.spinner("AI가 파일을 분석하여 단어를 추출 중입니다. (파일이 많을수록 시간이 소요됩니다)..."):
                        try:
                            client = genai.Client(api_key=GEMINI_API_KEY)
                            
                            prompt = """제공된 파일 데이터들을 분석하여 단어를 추출하십시오. 
조건 1: 만약 제공된 파일들이 '단어장(단어와 뜻이 나열된 목록)' 형태라면, 목록에 있는 **모든 단어**를 개수 제한 없이 누락 없이 100% 추출하십시오.
조건 2: 만약 일반적인 글(기사, 문단 등)이라면, 전체 내용에서 학습할 가치가 있는 **핵심 영단어 모두**를 개수 제한 없이 선별하여 추출하십시오.
반드시 아래 JSON 배열 형식으로만 응답하십시오.

출력 형식:
[{"word": "apple", "meaning": "사과", "example": "I ate an apple."}]"""
                            
                            contents = [prompt]
                            
                            # 여러 파일의 데이터를 모두 contents 리스트에 추가
                            for uploaded_file in uploaded_files:
                                if uploaded_file.name.lower().endswith('.pdf'):
                                    pdf_reader = PyPDF2.PdfReader(uploaded_file)
                                    pdf_text = ""
                                    for page in pdf_reader.pages:
                                        pdf_text += page.extract_text() + "\n"
                                    contents.append(f"PDF 텍스트 내용 ({uploaded_file.name}):\n{pdf_text}")
                                else:
                                    img = Image.open(uploaded_file)
                                    contents.append(img)
                                
                            response = client.models.generate_content(model='gemini-3.1-flash-lite', contents=contents)
                            result_text = response.text.strip().replace("```json", "").replace("```", "").strip()
                            st.session_state.extracted_df = pd.DataFrame(json.loads(result_text))
                        except Exception as e:
                            st.error(f"오류 발생: {e}")

    # 데이터 추출 완료 후 공통으로 실행되는 검수 테이블 UI
    if st.session_state.extracted_df is not None:
        st.warning("데이터 검수: 잘못 추출된 데이터가 있다면 표를 클릭하여 직접 수정하십시오.")
        edited_df = st.data_editor(st.session_state.extracted_df, use_container_width=True, num_rows="dynamic")
        if st.button("검수 완료 및 데이터베이스 저장"):
            today = datetime.now().strftime("%Y-%m-%d")
            for index, row in edited_df.iterrows():
                c.execute("INSERT INTO vocab (category, word, meaning, example, date, wrong_count, last_tested) VALUES (?, ?, ?, ?, ?, 0, NULL)", 
                          (category, row['word'], row['meaning'], row['example'], today))
            conn.commit()
            st.success(f"{len(edited_df)}개의 단어 저장 완료")
            st.session_state.extracted_df = None
            st.rerun()

# --- [탭 2: 누적 단어 확인] ---
with tab2:
    st.subheader("데이터베이스 목록 및 관리")
    view_mode = st.radio("필터 옵션", ["전체 단어 보기", "틀린 단어(오답)만 카테고리별로 모아보기"], horizontal=True)
    view_style = st.radio("표시 방식", ["표(엑셀) 형식으로 수정/관리", "플래시카드 형식으로 예문과 함께 학습"], horizontal=True)
    st.divider()
    
    c.execute("SELECT DISTINCT category FROM vocab")
    categories = [row[0] for row in c.fetchall() if row[0]]
    
    if categories:
        selected_category = st.selectbox("카테고리 조회", ["전체"] + categories)
        if selected_category != "전체":
            st.markdown(f"### ⚠️ 카테고리 관리")
            if st.button(f"🔥 '{selected_category}' 카테고리 및 내부 단어 일괄 삭제"):
                c.execute("DELETE FROM vocab WHERE category=?", (selected_category,))
                conn.commit()
                st.rerun()
            st.divider()
        
        query = "SELECT rowid, category, word, meaning, example, wrong_count, last_tested FROM vocab"
        conditions, params = [], []
        if selected_category != "전체":
            conditions.append("category = ?"); params.append(selected_category)
        if view_mode == "틀린 단어(오답)만 카테고리별로 모아보기": conditions.append("wrong_count > 0")
        if conditions: query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY category ASC, word ASC"
        
        c.execute(query, tuple(params))
        words = c.fetchall()
        
        if words:
            if view_style == "표(엑셀) 형식으로 수정/관리":
                df_existing = pd.DataFrame(words, columns=["ID", "카테고리", "단어", "뜻", "예문", "틀린 횟수", "최근 학습일"])
                st.write("✏️ **데이터 수정 (셀 더블클릭 후 수정 가능)**")
                edited_existing_df = st.data_editor(df_existing, disabled=["ID", "틀린 횟수", "최근 학습일"], use_container_width=True)
                if st.button("변경사항 갱신"):
                    for index, row in edited_existing_df.iterrows():
                        c.execute("UPDATE vocab SET category=?, word=?, meaning=?, example=? WHERE rowid=?", (row["카테고리"], row["단어"], row["뜻"], row["예문"], row["ID"]))
                    conn.commit()
                    st.success("데이터베이스 갱신 완료"); st.rerun()
                    
                st.divider()
                delete_options = {f"[{row[0]}] {row[2]} : {row[3]}": row[0] for row in words}
                selected_words_to_delete = st.multiselect("삭제할 단어를 선택하십시오.", options=list(delete_options.keys()))
                if st.button("❌ 선택한 단어 삭제"):
                    if selected_words_to_delete:
                        for item in selected_words_to_delete: c.execute("DELETE FROM vocab WHERE rowid=?", (delete_options[item],))
                        conn.commit(); st.rerun()
            else:
                st.write("💡 **단어 상자를 클릭하면 뜻과 예문이 펼쳐집니다.**")
                for w in words:
                    with st.expander(f"📖 **{w[2]}** (틀린 횟수: {w[5]})"):
                        st.markdown(f"**뜻:** {w[3]}\n\n**예문:** {w[4]}")
                        st.caption(f"카테고리: {w[1]} | 최근 학습일: {w[6] if w[6] else '없음'}")
        else: st.info("조건에 맞는 데이터가 없습니다.")
    else: st.info("데이터가 없습니다.")

# --- [탭 3: 실전 퀴즈] ---
with tab3:
    st.subheader("퀴즈 설정 및 실행")
    if not st.session_state.quiz_started:
        c.execute("SELECT DISTINCT category FROM vocab")
        quiz_categories = [row[0] for row in c.fetchall() if row[0]]
        
        if len(quiz_categories) > 0:
            quiz_cat = st.selectbox("시험 범위 선택", ["전체"] + quiz_categories)
            quiz_type = st.radio("문제 유형", ["객관식", "주관식"], horizontal=True)
            quiz_direction = st.radio("출제 방식", ["영단어를 보고 뜻 맞추기", "뜻을 보고 영단어 맞추기"], horizontal=True)
            quiz_mode = st.radio("출제 대상", ["모든 단어 대상", "틀린 단어(오답)만 모아서 시험보기", "망각 곡선 복습 (최근 3일 이상 안 본 단어 우선)"], horizontal=True)
            
            query = "SELECT rowid, word, meaning, example, wrong_count FROM vocab"
            conditions, params = [], []
            if quiz_cat != "전체": conditions.append("category = ?"); params.append(quiz_cat)
            if quiz_mode == "틀린 단어(오답)만 모아서 시험보기": conditions.append("wrong_count > 0")
            elif quiz_mode == "망각 곡선 복습 (최근 3일 이상 안 본 단어 우선)":
                three_days_ago = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
                conditions.append("(last_tested IS NULL OR last_tested <= ?)"); params.append(three_days_ago)
                
            if conditions: query += " WHERE " + " AND ".join(conditions)
            c.execute(query, tuple(params))
            raw_all_words = c.fetchall()
            
            unique_all_words = []
            seen_words = set()
            for row in raw_all_words:
                if row[1] not in seen_words: seen_words.add(row[1]); unique_all_words.append(row)
            
            st.write(f"현재 조건에 맞는 단어는 중복 제외 **{len(unique_all_words)}개** 입니다.")
            if len(unique_all_words) > 0:
                num_questions = st.number_input("출제할 문제 수", min_value=1, max_value=len(unique_all_words), value=min(10, len(unique_all_words)), step=1)
                c.execute("SELECT word, meaning FROM vocab")
                global_pool = c.fetchall()
                
                if st.button("퀴즈 시작"):
                    if quiz_type == "객관식" and len(global_pool) < 4: st.warning("보기를 생성하려면 최소 4개 이상의 단어가 필요합니다.")
                    else:
                        st.session_state.quiz_started = True
                        st.session_state.quiz_pool = random.sample(unique_all_words, num_questions)
                        st.session_state.current_idx, st.session_state.score = 0, 0
                        st.session_state.graded = False
                        st.session_state.options, st.session_state.options_pairs = None, None
                        st.session_state.q_type, st.session_state.q_dir = quiz_type, quiz_direction
                        st.session_state.wrong_answers = []
                        st.rerun()
            else: st.warning("조건에 맞는 단어가 없습니다.")
        else: st.info("데이터베이스에 등록된 단어가 없습니다.")
    else:
        if st.session_state.current_idx < len(st.session_state.quiz_pool):
            st.progress(st.session_state.current_idx / len(st.session_state.quiz_pool))
            st.write(f"### 문제 {st.session_state.current_idx + 1} / {len(st.session_state.quiz_pool)}")
            
            current_q = st.session_state.quiz_pool[st.session_state.current_idx]
            q_id, raw_word, raw_meaning, raw_example, q_wrong_count = current_q
            c.execute("SELECT word, meaning FROM vocab")
            global_pool = c.fetchall()
            
            q_text, q_ans = (raw_word, raw_meaning) if st.session_state.q_dir == "영단어를 보고 뜻 맞추기" else (raw_meaning, raw_word)
            st.info(f"문제: **{q_text}** (현재까지 틀린 횟수: {q_wrong_count}회)")
            
            with st.expander("👉 여기를 클릭해서 예문(힌트) 보기"):
                if raw_example:
                    if st.session_state.q_dir == "뜻을 보고 영단어 맞추기":
                        st.write(re.sub(re.escape(raw_word), "_____", raw_example, flags=re.IGNORECASE))
                    else: st.write(raw_example)
                else: st.write("등록된 예문이 없습니다.")

            if not st.session_state.graded:
                if st.session_state.q_type == "객관식":
                    if st.session_state.options_pairs is None:
                        wrong_pool_pairs = [w for w in global_pool if w[0] != raw_word and w[1] != raw_meaning]
                        opts_pairs = random.sample(wrong_pool_pairs, min(3, len(wrong_pool_pairs))) + [(raw_word, raw_meaning)]
                        random.shuffle(opts_pairs)
                        st.session_state.options_pairs = opts_pairs
                        st.session_state.options = [p[1] if st.session_state.q_dir == "영단어를 보고 뜻 맞추기" else p[0] for p in opts_pairs]
                    
                    st.write("**정답을 선택하십시오:**")
                    for opt in st.session_state.options:
                        if st.button(opt, key=f"btn_{st.session_state.current_idx}_{opt}", use_container_width=True):
                            st.session_state.graded = True
                            if opt == q_ans:
                                st.session_state.last_result = "correct"; st.session_state.score += 1
                            else:
                                st.session_state.last_result = "incorrect"
                                c.execute("UPDATE vocab SET wrong_count = wrong_count + 1 WHERE rowid = ?", (q_id,))
                                st.session_state.wrong_answers.append({"문제": q_text, "정답": q_ans, "내가 고른 답": opt})
                            c.execute("UPDATE vocab SET last_tested = ? WHERE rowid = ?", (datetime.now().strftime("%Y-%m-%d"), q_id))
                            conn.commit(); st.rerun()
                else:
                    user_answer = st.text_input("정답 입력", key=f"sa_{st.session_state.current_idx}")
                    if st.button("정답 확인"):
                        st.session_state.graded = True
                        if user_answer.strip().lower() == q_ans.strip().lower():
                            st.session_state.last_result = "correct"; st.session_state.score += 1
                        else:
                            st.session_state.last_result = "incorrect"
                            c.execute("UPDATE vocab SET wrong_count = wrong_count + 1 WHERE rowid = ?", (q_id,))
                            st.session_state.wrong_answers.append({"문제": q_text, "정답": q_ans, "내가 고른 답": user_answer})
                        c.execute("UPDATE vocab SET last_tested = ? WHERE rowid = ?", (datetime.now().strftime("%Y-%m-%d"), q_id))
                        conn.commit(); st.rerun()
            else:
                if st.session_state.last_result == "correct": st.success("정답입니다.")
                else: st.error(f"오답입니다. 정답: {q_ans}")
                
                with st.expander("📖 전체 예문 다시 확인하기"): st.write(raw_example if raw_example else "등록된 예문이 없습니다.")
                if st.session_state.q_type == "객관식":
                    st.divider(); st.markdown("💡 **보기 단어 뜻 확인**")
                    for w, m in st.session_state.options_pairs:
                        if w == raw_word: st.markdown(f"- **{w} : {m} (정답)**")
                        else: st.markdown(f"- {w} : {m}")
                    
                if st.button("다음 문제", use_container_width=True):
                    st.session_state.current_idx += 1
                    st.session_state.graded = False
                    st.session_state.options, st.session_state.options_pairs = None, None
                    st.rerun()
        else:
            st.progress(1.0); st.markdown("---"); st.subheader("🏁 퀴즈 결과")
            total_questions = len(st.session_state.quiz_pool)
            st.write(f"### 🏆 최종 점수: {st.session_state.score} / {total_questions} 점")
            if st.session_state.score == total_questions: st.balloons(); st.success("💯 완벽합니다!")
            else:
                st.warning("📝 **오답 노트**")
                st.table(pd.DataFrame(st.session_state.wrong_answers))
            if st.button("새 퀴즈 설정으로 돌아가기"): st.session_state.quiz_started = False; st.rerun()

# --- [탭 4: 학습 통계 대시보드] ---
with tab4:
    st.subheader("📊 나의 학습 통계")
    c.execute("SELECT COUNT(*), SUM(wrong_count) FROM vocab")
    total_words, total_wrongs = c.fetchone()
    
    col1, col2 = st.columns(2)
    col1.metric("총 등록 단어 수", f"{total_words}개")
    col2.metric("누적 오답 횟수", f"{total_wrongs or 0}회")
    
    st.divider()
    col3, col4 = st.columns(2)
    with col3:
        st.markdown("### 🚨 가장 많이 틀린 단어 TOP 10")
        c.execute("SELECT word, wrong_count FROM vocab WHERE wrong_count > 0 ORDER BY wrong_count DESC LIMIT 10")
        top_wrong = c.fetchall()
        if top_wrong: st.bar_chart(pd.DataFrame(top_wrong, columns=["단어", "틀린 횟수"]).set_index("단어"))
        else: st.info("아직 오답 기록이 없습니다.")
            
    with col4:
        st.markdown("### 📁 카테고리별 단어 분포")
        c.execute("SELECT category, COUNT(*) FROM vocab GROUP BY category")
        cat_dist = c.fetchall()
        if cat_dist: st.bar_chart(pd.DataFrame(cat_dist, columns=["카테고리", "단어 수"]).set_index("카테고리"))
        else: st.info("데이터가 없습니다.")

# --- [탭 5: 데이터 백업/복구 (구글 드라이브 양방향 연동)] ---
with tab5:
    st.subheader("데이터 백업 및 복구 관리")
    st.write("무료 클라우드 서버 특성상 서버가 재부팅되면 저장된 단어가 초기화될 수 있습니다. 공부를 마친 후 수시로 데이터를 백업해 두십시오.")
    
    c.execute("SELECT COUNT(*) FROM oauth_creds")
    is_authenticated = c.fetchone()[0] > 0
    
    if not is_authenticated:
        st.warning("클라우드 자동 저장 및 복구 기능을 사용하려면 권한 인증이 필요합니다.")
        try:
            client_config = json.loads(st.secrets["GOOGLE_CLIENT_CONFIG"])
            flow = Flow.from_client_config(client_config, scopes=SCOPES, redirect_uri=st.secrets["REDIRECT_URI"])
            auth_url, state = flow.authorization_url(prompt='consent', access_type='offline')
            c.execute("DELETE FROM oauth_store")
            c.execute("INSERT INTO oauth_store (state, verifier) VALUES (?, ?)", (state, flow.code_verifier))
            conn.commit()
            st.markdown(f"### [👉 구글 계정으로 로그인하여 드라이브 연동하기]({auth_url})")
        except Exception as e: st.error(f"Secrets 설정 확인 요망: {e}")
    else:
        col_auth1, col_auth2 = st.columns([3, 1])
        with col_auth1: st.success("✅ 구글 드라이브 인증이 완료되었습니다.")
        with col_auth2:
            if st.button("🔌 연동 해제 및 재연동", use_container_width=True):
                c.execute("DELETE FROM oauth_creds"); conn.commit(); st.rerun()
                
    st.divider()
    
    c.execute("SELECT category, word, meaning, example, date, wrong_count, last_tested FROM vocab")
    all_rows = c.fetchall()
    
    df_backup = pd.DataFrame(all_rows, columns=["category", "word", "meaning", "example", "date", "wrong_count", "last_tested"])
    csv_data = df_backup.to_csv(index=False)
    csv_bytes = csv_data.encode('utf-8-sig')
    
    st.markdown("### 📥 단어장 백업 (서버 -> 보관소)")
    col_b1, col_b2 = st.columns(2)
    with col_b1:
        st.download_button(label="기기(폰/PC)에 파일 다운로드", data=csv_bytes, file_name=f"vocab_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv", mime="text/csv", use_container_width=True)
    with col_b2:
        if st.button("🚀 구글 드라이브 자동 저장", use_container_width=True):
            if not is_authenticated:
                st.error("먼저 상단의 구글 드라이브 연동을 진행해 주십시오.")
            else:
                with st.spinner('구글 드라이브 업로드 중...'):
                    try:
                        upload_to_google_drive(csv_data)
                        st.success("구글 드라이브 백업 완료!")
                    except Exception as e: 
                        st.error(f"업로드 실패: {e}")
                        st.info("💡 권한이 만료되었을 수 있습니다. '연동 해제 및 재연동' 버튼을 눌러 다시 로그인해 주십시오.")
        
    st.divider()
    
    st.markdown("### 📤 단어장 복구 (보관소 -> 서버)")
    if st.button("🔄 구글 드라이브에서 최신 백업 즉시 불러오기", use_container_width=True):
        if not is_authenticated:
            st.error("먼저 상단의 구글 드라이브 연동을 진행해 주십시오.")
        else:
            with st.spinner('구글 드라이브에서 최신 백업 탐색 중...'):
                try:
                    content_bytes, file_name = download_latest_from_google_drive()
                    df_restore = pd.read_csv(io.BytesIO(content_bytes))
                    
                    c.execute("DELETE FROM vocab")
                    for _, row in df_restore.iterrows():
                        last_t = row.get("last_tested", None)
                        last_t = last_t if pd.notna(last_t) else None
                        c.execute("INSERT INTO vocab (category, word, meaning, example, date, wrong_count, last_tested) VALUES (?, ?, ?, ?, ?, ?, ?)",
                                  (str(row['category']), str(row['word']), str(row['meaning']), str(row['example']), str(row['date']), int(row['wrong_count']), last_t))
                    conn.commit()
                    st.success(f"성공! 최신 백업 파일 [{file_name}] 데이터를 정상적으로 불러왔습니다.")
                    st.rerun()
                except Exception as e:
                    st.error(f"불러오기 실패: {e}")
                    st.info("💡 권한이 만료되었을 수 있습니다. '연동 해제 및 재연동' 버튼을 눌러 다시 로그인해 주십시오.")
                
    st.caption("또는 아래에 직접 백업 파일을 업로드하여 복구할 수도 있습니다.")
    uploaded_backup = st.file_uploader("이전에 백업한 CSV 파일을 선택하십시오.", type=["csv"])
    
    if uploaded_backup is not None:
        try:
            df_restore = pd.read_csv(uploaded_backup)
            required_cols = ["category", "word", "meaning", "example", "date", "wrong_count"]
            
            if all(col in df_restore.columns for col in required_cols):
                st.success("올바른 양식의 백업 파일이 확인되었습니다.")
                st.dataframe(df_restore.head(5), use_container_width=True)
                restore_mode = st.radio("복구 방식을 선택하십시오.", ["기존 단어장에 이어서 추가하기", "기존 데이터 전부 지우고 새로 덮어쓰기"])
                
                if st.button("🚀 업로드 파일로 복구 실행", use_container_width=True):
                    if restore_mode == "기존 데이터 전부 지우고 새로 덮어쓰기": c.execute("DELETE FROM vocab")
                    for _, row in df_restore.iterrows():
                        last_t = row.get("last_tested", None)
                        last_t = last_t if pd.notna(last_t) else None
                        c.execute("INSERT INTO vocab (category, word, meaning, example, date, wrong_count, last_tested) VALUES (?, ?, ?, ?, ?, ?, ?)",
                                  (str(row['category']), str(row['word']), str(row['meaning']), str(row['example']), str(row['date']), int(row['wrong_count']), last_t))
                    conn.commit()
                    st.success(f"성공적으로 데이터를 복구했습니다!")
                    st.rerun()
            else: st.error("필수 열 정보가 누락되었습니다.")
        except Exception as e: st.error(f"오류 발생: {e}")
