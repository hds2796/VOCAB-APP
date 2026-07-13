import streamlit as st
import sqlite3
import json
import random
import pandas as pd
import re
import io
import os
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

# =======================================================
# 1. 데이터베이스 설정 (세션 초기화 방지를 위해 최상단 배치)
# =======================================================
conn = sqlite3.connect('my_vocab.db', check_same_thread=False)
c = conn.cursor()

# 단어장 테이블
c.execute('''CREATE TABLE IF NOT EXISTS vocab 
            (category TEXT, word TEXT, meaning TEXT, example TEXT, date TEXT, wrong_count INTEGER DEFAULT 0, last_tested TEXT)''')

# 구글 로그인 인증용 임시 저장 테이블 및 권한 토큰 영구 저장 테이블 신설
c.execute('''CREATE TABLE IF NOT EXISTS oauth_store (state TEXT, verifier TEXT)''')
c.execute('''CREATE TABLE IF NOT EXISTS oauth_creds (creds TEXT)''')
conn.commit()

try:
    c.execute("ALTER TABLE vocab ADD COLUMN wrong_count INTEGER DEFAULT 0")
    conn.commit()
except sqlite3.OperationalError:
    pass

try:
    c.execute("ALTER TABLE vocab ADD COLUMN last_tested TEXT")
    conn.commit()
except sqlite3.OperationalError:
    pass


# --- [보안: 로그인 시스템] ---
def check_password():
    if "pwd" in st.query_params:
        if st.query_params["pwd"] == st.secrets["APP_PASSWORD"]:
            st.session_state["password_correct"] = True

    if st.session_state.get("password_correct", False):
        return True

    st.title("🔒 나만의 단어장 로그인")
    st.warning("⚠️ **경고: 처음에 설정한 비밀번호를 잃어버리면 절대 찾을 수 없습니다.**")

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


# --- [구글 드라이브 OAuth 인증 콜백 처리] ---
def handle_oauth_callback():
    # URL에 state와 code가 모두 반환된 경우 처리
    if 'code' in st.query_params and 'state' in st.query_params:
        state = st.query_params['state']
        code = st.query_params['code']

        # DB에서 일치하는 state의 verifier를 탐색 (세션 초기화 우회)
        c.execute("SELECT verifier FROM oauth_store WHERE state=?", (state,))
        row = c.fetchone()

        if not row:
            st.query_params.clear()
            st.warning("로그인 세션이 만료되었습니다. 데이터 백업 탭에서 버튼을 다시 클릭하여 주십시오.")
            return

        verifier = row[0]

        try:
            client_config = json.loads(st.secrets["GOOGLE_CLIENT_CONFIG"])
            flow = Flow.from_client_config(
                client_config,
                scopes=SCOPES,
                redirect_uri=st.secrets["REDIRECT_URI"]
            )

            # DB에서 꺼내온 검증키 주입
            flow.code_verifier = verifier
            flow.fetch_token(code=code)
            creds = flow.credentials

            # 인증 토큰을 DB에 영구 저장 (앱 재부팅 시에도 로그인 유지)
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
            c.execute("DELETE FROM oauth_store") # 사용 끝난 임시 검증키 삭제
            conn.commit()

            st.query_params.clear()
            st.rerun()
        except Exception as e:
            st.error(f"구글 로그인 인증 오류가 발생했습니다: {e}")

handle_oauth_callback()


def init_drive_service():
    # DB에 저장된 권한 토큰을 불러와 드라이브 서비스 활성화
    c.execute("SELECT creds FROM oauth_creds")
    row = c.fetchone()
    if row:
        try:
            cred_dict = json.loads(row[0])
            creds = Credentials.from_authorized_user_info(cred_dict, SCOPES)
            return build('drive', 'v3', credentials=creds)
        except Exception as e:
            pass
    return None


def upload_to_google_drive(csv_string):
    service = init_drive_service()
    if not service:
        raise Exception("먼저 구글 드라이브로 로그인해야 합니다.")

    file_name = f"vocab_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    file_metadata = {
        'name': file_name,
        'parents': [st.secrets["GOOGLE_FOLDER_ID"]]
    }

    csv_bytes = csv_string.encode('utf-8-sig')
    media = MediaIoBaseUpload(io.BytesIO(csv_bytes), mimetype='text/csv', resumable=True)
    file = service.files().create(body=file_metadata, media_body=media, fields='id').execute()
    return file.get('id')


def download_latest_from_google_drive():
    service = init_drive_service()
    if not service:
        raise Exception("먼저 구글 드라이브로 로그인해야 합니다.")

    folder_id = st.secrets["GOOGLE_FOLDER_ID"]
    query = f"'{folder_id}' in parents and mimeType = 'text/csv' and trashed = false"
    results = service.files().list(
        q=query,
        orderBy="modifiedTime desc",
        pageSize=1,
        fields="files(id, name)"
    ).execute()

    files = results.get('files', [])
    if not files:
        raise Exception("구글 드라이브 폴더에 백업된 CSV 파일이 없습니다.")

    latest_file = files[0]
    file_id = latest_file['id']
    file_name = latest_file['name']

    content = service.files().get_media(fileId=file_id).execute()
    return content, file_name


# 세션 상태 초기화
if 'extracted_df' not in st.session_state: st.session_state.extracted_df = None
if 'quiz_started' not in st.session_state: st.session_state.quiz_started = False
if 'quiz_pool' not in st.session_state: st.session_state.quiz_pool = []
if 'current_idx' not in st.session_state: st.session_state.current_idx = 0
if 'graded' not in st.session_state: st.session_state.graded = False
if 'options' not in st.session_state: st.session_state.options = None
if 'options_pairs' not in st.session_state: st.session_state.options_pairs = None
if 'last_result' not in st.session_state: st.session_state.last_result = None
if 'score' not in st.session_state: st.session_state.score = 0
if 'wrong_answers' not in st.session_state: st.session_state.wrong_answers = []


# 2. 웹사이트 화면 구성
st.set_page_config(page_title="VOCAB", page_icon="📝")

st.markdown("""
<style>
.stTextInput input { font-size: 20px !important; padding: 15px !important; }
.stButton > button, .stDownloadButton > button { font-size: 20px !important; padding: 15px 30px !important; font-weight: bold !important; }
.stAlert p { font-size: 20px !important; }
.stExpander p { font-size: 18px !important; }
</style>
""", unsafe_allow_html=True)

st.title("VOCAB")

c.execute("SELECT COUNT(*) FROM vocab")
total_vocab_count = c.fetchone()[0]
st.info(f"현재 데이터베이스에 등록된 총 영단어 수: **{total_vocab_count}개**")

tab1, tab2, tab3, tab4, tab5 = st.tabs(["단어장 생성", "누적 단어 확인", "실전 퀴즈", "📊 학습 통계", "데이터 백업/복구"])

API_KEY = st.secrets.get("GEMINI_API_KEY", "")

# --- [탭 1: 단어장 생성] ---
with tab1:
    st.subheader("사진 또는 PDF로 단어 추가")
    c.execute("SELECT DISTINCT category FROM vocab")
    db_categories = [row[0] for row in c.fetchall() if row[0]]
    base_categories = ["토플 영단어", "ETC"]
    for cat in db_categories:
        if cat not in base_categories: base_categories.append(cat)
    base_categories.append("직접 입력")

    category = st.selectbox("카테고리 선택", base_categories)
    if category == "직접 입력": category = st.text_input("새 카테고리 이름 입력")

    uploaded_files = st.file_uploader("단어가 있는 사진 또는 PDF 파일 업로드 (복수 선택 가능)", type=["jpg", "png", "jpeg", "pdf"], accept_multiple_files=True)

    if uploaded_files:
        st.write(f"📎 총 {len(uploaded_files)}개의 파일이 선택되었습니다.")
        if st.button("AI 추출 실행"):
            if not API_KEY: st.error("API 키 오류")
            else:
                with st.spinner('모든 파일을 통합 분석 중입니다...'):
                    try:
                        from google.genai import types
                        client = genai.Client(api_key=API_KEY)
                        contents = []
                        for f in uploaded_files:
                            contents.append(types.Part.from_bytes(data=f.read(), mime_type=f.type))

                        prompt = """
                        업로드된 이미지 또는 PDF 문서들에서 중요한 영어 단어들을 모두 추출해주세요.
                        단어장 파일의 경우 단어장에서 외우도록 한 모든 단어를 추출합니다.
                        그 외에는 외울만하다고 생각되는 단어를 추출합니다.
                        즉, I'm, No와 유사한 역할들을 하는 단어는 추출하지 않습니다.
                        반드시 아래 형태의 JSON 배열로만 답변해야 합니다:
                        [ {"word": "단어", "meaning": "한국어 뜻", "example": "영어 예문"} ]
                        """
                        contents.append(prompt)
                        response = client.models.generate_content(model='gemini-2.5-flash', contents=contents)
                        result_text = response.text.strip()

                        prefix_json = "`" * 3 + "json"
                        prefix_empty = "`" * 3

                        if result_text.startswith(prefix_json): 
                            result_text = result_text[7:-3]
                        elif result_text.startswith(prefix_empty): 
                            result_text = result_text[3:-3]
                            
                        st.session_state.extracted_df = pd.DataFrame(json.loads(result_text))
                    except Exception as e: st.error(f"오류 발생: {e}")

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
            with st.spinner('구글 드라이브 업로드 중...'):
                try:
                    upload_to_google_drive(csv_data)
                    st.success("구글 드라이브 백업 완료!")
                except Exception as e: st.error(f"업로드 실패: {e}")
        
    st.divider()
    
    st.markdown("### 📤 단어장 복구 (보관소 -> 서버)")
    if st.button("🔄 구글 드라이브에서 최신 백업 즉시 불러오기", use_container_width=True):
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
