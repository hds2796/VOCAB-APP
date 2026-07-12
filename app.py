import streamlit as st
import sqlite3
import json
import random
import pandas as pd
import re
from datetime import datetime
from google import genai
from PIL import Image

# --- [보안: 로그인 시스템] ---
def check_password():
    """Returns `True` if the user had a correct password."""
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

# 2. 웹사이트 화면 구성
st.set_page_config(page_title="단어장 앱", page_icon="📝")
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
                st.rerun()
            st.divider()
        
        query = "SELECT rowid, category, word, meaning, example, wrong_count FROM vocab"
        conditions = []
        params = []
        
        if selected_category != "전체":
            conditions.append("category = ?")
            params.append(selected_category)
            
        if view_mode == "틀린 단어(오답)만 카테고리별로 모아보기":
            conditions.append("wrong_count > 0")
            
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
            
        query += " ORDER BY category ASC, word ASC"
        
        c.execute(query, tuple(params))
        words = c.fetchall()
        
        if words:
            if view_style == "표(엑셀) 형식으로 수정/관리":
                df_existing = pd.DataFrame(words, columns=["ID", "카테고리", "단어", "뜻", "예문", "틀린 횟수"])
                st.write("✏️ **데이터 수정 (셀 더블클릭 후 수정 가능)**")
                edited_existing_df = st.data_editor(df_existing, disabled=["ID", "틀린 횟수"], use_container_width=True)
                
                if st.button("변경사항 갱신"):
                    for index, row in edited_existing_df.iterrows():
                        c.execute("UPDATE vocab SET category=?, word=?, meaning=?, example=? WHERE rowid=?",
                                  (row["카테고리"], row["단어"], row["뜻"], row["예문"], row["ID"]))
                    conn.commit()
                    st.success("데이터베이스 갱신 완료")
                    st.rerun()
                    
                st.divider()
                st.markdown("### 🗑️ 개별 단어 선택 삭제")
                delete_options = {f"[{row[0]}] {row[2]} : {row[3]}": row[0] for row in words}
                selected_words_to_delete = st.multiselect("삭제할 단어를 선택하십시오. (복수 선택 가능)", options=list(delete_options.keys()))
                
                if st.button("❌ 선택한 단어 삭제", key="del_words_btn"):
                    if selected_words_to_delete:
                        for item in selected_words_to_delete:
                            word_id = delete_options[item]
                            c.execute("DELETE FROM vocab WHERE rowid=?", (word_id,))
                        conn.commit()
                        st.success(f"선택한 {len(selected_words_to_delete)}개의 단어가 삭제되었습니다.")
                        st.rerun()
                    else:
                        st.warning("삭제할 단어를 먼저 선택해 주십시오.")
                        
            else:
                st.write("💡 **단어 상자를 클릭하면 뜻과 예문이 펼쳐집니다.**")
                for w in words:
                    with st.expander(f"📖 **{w[2]}** (틀린 횟수: {w[5]})"):
                        st.markdown(f"**뜻:** {w[3]}")
                        st.markdown(f"**예문:** {w[4]}")
                        st.caption(f"카테고리: {w[1]}")
        else:
            st.info("조건에 맞는 데이터가 없습니다.")
    else:
        st.info("데이터가 없습니다.")

# --- [탭 3: 실전 퀴즈] ---
with tab3:
    st.subheader("퀴즈 설정 및 실행")
    
    if not st.session_state.quiz_started:
        c.execute("SELECT DISTINCT category FROM vocab")
        quiz_categories = [row[0] for row in c.fetchall() if row[0]]
        
        if len(quiz_categories) > 0:
            quiz_cat = st.selectbox("시험 범위 선택", ["전체"] + quiz_categories, key="quiz_cat")
            quiz_type = st.radio("문제 유형", ["객관식", "주관식"], horizontal=True)
            quiz_direction = st.radio("출제 방식", ["영단어를 보고 뜻 맞추기", "뜻을 보고 영단어 맞추기"], horizontal=True)
            quiz_mode = st.radio("출제 대상", ["모든 단어 대상", "틀린 단어(오답)만 모아서 시험보기"], horizontal=True)
            
            query = "SELECT rowid, word, meaning, example, wrong_count FROM vocab"
            conditions = []
            params = []
            
            if quiz_cat != "전체":
                conditions.append("category = ?")
                params.append(quiz_cat)
                
            if quiz_mode == "틀린 단어(오답)만 모아서 시험보기":
                conditions.append("wrong_count > 0")
                
            if conditions:
                query += " WHERE " + " AND ".join(conditions)
                
            c.execute(query, tuple(params))
            raw_all_words = c.fetchall()
            
            unique_all_words = []
            seen_words = set()
            for row in raw_all_words:
                if row[1] not in seen_words:
                    seen_words.add(row[1])
                    unique_all_words.append(row)
            
            st.write(f"현재 조건에 맞는 단어는 중복 제외 **{len(unique_all_words)}개** 입니다.")
            
            if len(unique_all_words) > 0:
                num_questions = st.number_input("출제할 문제 수를 설정하십시오.", min_value=1, max_value=len(unique_all_words), value=min(10, len(unique_all_words)), step=1)
                
                c.execute("SELECT word, meaning FROM vocab")
                global_pool = c.fetchall()
                
                if st.button("퀴즈 시작"):
                    if quiz_type == "객관식" and len(global_pool) < 4:
                        st.warning("객관식 보기를 만들기 위해 전체 데이터베이스에 최소 4개 이상의 단어가 필요합니다.")
                    else:
                        st.session_state.quiz_started = True
                        st.session_state.quiz_pool = random.sample(unique_all_words, num_questions)
                        st.session_state.current_idx = 0
                        st.session_state.graded = False
                        st.session_state.options = None
                        st.session_state.options_pairs = None
                        st.session_state.q_type = quiz_type
                        st.session_state.q_dir = quiz_direction
                        st.session_state.score = 0
                        st.session_state.wrong_answers = []
                        st.rerun()
            else:
                st.warning("조건에 맞는 단어가 없습니다.")
        else:
            st.info("데이터베이스에 등록된 단어가 없습니다.")
            
    else:
        if st.session_state.current_idx < len(st.session_state.quiz_pool):
            st.progress(st.session_state.current_idx / len(st.session_state.quiz_pool))
            st.write(f"### 문제 {st.session_state.current_idx + 1} / {len(st.session_state.quiz_pool)}")
            
            current_q = st.session_state.quiz_pool[st.session_state.current_idx]
            q_id, raw_word, raw_meaning, raw_example, q_wrong_count = current_q
            
            c.execute("SELECT word, meaning FROM vocab")
            global_pool = c.fetchall()
            
            if st.session_state.q_dir == "영단어를 보고 뜻 맞추기":
                q_text = raw_word
                q_ans = raw_meaning
            else:
                q_text = raw_meaning
                q_ans = raw_word
                
            st.info(f"문제: **{q_text}** (현재까지 틀린 횟수: {q_wrong_count}회)")
            
            # --- [추가/수정된 부분: 예문 힌트 제공 아코디언] ---
            with st.expander("👉 여기를 클릭해서 예문(힌트) 보기"):
                if raw_example:
                    # 뜻을 보고 영단어를 맞추는 방향일 때 정답 노출 방지 처리
                    if st.session_state.q_dir == "뜻을 보고 영단어 맞추기":
                        hidden_example = re.sub(re.escape(raw_word), "_____", raw_example, flags=re.IGNORECASE)
                        st.write(hidden_example)
                    else:
                        st.write(raw_example)
                else:
                    st.write("등록된 예문이 없습니다.")
            # ---------------------------------------------------

            if st.session_state.q_type == "객관식":
                if st.session_state.options_pairs is None:
                    wrong_pool_pairs = [w for w in global_pool if w[0] != raw_word and w[1] != raw_meaning]
                    wrong_samples = random.sample(wrong_pool_pairs, min(3, len(wrong_pool_pairs)))
                    
                    opts_pairs = wrong_samples + [(raw_word, raw_meaning)]
                    random.shuffle(opts_pairs)
                    st.session_state.options_pairs = opts_pairs
                    
                    if st.session_state.q_dir == "영단어를 보고 뜻 맞추기":
                        st.session_state.options = [p[1] for p in opts_pairs]
                    else:
                        st.session_state.options = [p[0] for p in opts_pairs]
                    
                user_answer = st.radio("정답 선택", st.session_state.options, key=f"mc_{st.session_state.current_idx}")
            else:
                user_answer = st.text_input("정답 입력", key=f"sa_{st.session_state.current_idx}")
                
            if not st.session_state.graded:
                if st.button("정답 확인"):
                    st.session_state.graded = True
                    
                    if st.session_state.q_type == "객관식":
                        is_correct = (user_answer == q_ans)
                    else:
                        is_correct = (user_answer.strip().lower() == q_ans.strip().lower())
                        
                    if is_correct:
                        st.session_state.last_result = "correct"
                        st.session_state.score += 1
                    else:
                        st.session_state.last_result = "incorrect"
                        c.execute("UPDATE vocab SET wrong_count = wrong_count + 1 WHERE rowid = ?", (q_id,))
                        conn.commit()
                        st.session_state.wrong_answers.append({
                            "문제": q_text,
                            "정답": q_ans,
                            "내가 고른 답": user_answer
                        })
                        
                    st.rerun()
            else:
                if st.session_state.last_result == "correct":
                    st.success("정답입니다.")
                else:
                    st.error(f"오답입니다. 정답: {q_ans}")
                
                # 채점이 끝난 후에는 온전한 예문 패널 노출 (기존 코드 유지)
                with st.expander("📖 전체 예문 다시 확인하기"):
                    st.write(raw_example if raw_example else "등록된 예문이 없습니다.")
                
                if st.session_state.q_type == "객관식":
                    st.divider()
                    st.markdown("💡 **보기 단어 뜻 확인**")
                    for w, m in st.session_state.options_pairs:
                        if w == raw_word and m == raw_meaning:
                            st.markdown(f"- **{w} : {m} (정답)**")
                        else:
                            st.markdown(f"- {w} : {m}")
                    
                if st.button("다음 문제"):
                    st.session_state.current_idx += 1
                    st.session_state.graded = False
                    st.session_state.options = None
                    st.session_state.options_pairs = None
                    st.rerun()
        else:
            st.progress(1.0)
            st.markdown("---")
            st.subheader("🏁 퀴즈 결과")
            
            total_questions = len(st.session_state.quiz_pool)
            score = st.session_state.score
            
            st.write(f"### 🏆 최종 점수: {score} / {total_questions} 점")
            
            if score == total_questions:
                st.balloons()
                st.success("💯 완벽합니다! 모든 문제를 다 맞추셨습니다!")
            else:
                st.warning("📝 **오답 노트**")
                df_wrong = pd.DataFrame(st.session_state.wrong_answers)
                st.table(df_wrong)
                
            st.markdown("---")
            if st.button("새 퀴즈 설정으로 돌아가기"):
                st.session_state.quiz_started = False
                st.rerun()

# --- [탭 4: 데이터 백업/복구] ---
with tab4:
    st.subheader("데이터 백업 및 복구 관리")
    st.write("무료 클라우드 서버 특성상 서버가 재부팅되면 저장된 단어가 초기화될 수 있습니다. 단어를 추가하거나 공부를 마친 후 아래 버튼을 통해 수시로 데이터를 백업해 두십시오.")
    
    c.execute("SELECT category, word, meaning, example, date, wrong_count FROM vocab")
    all_rows = c.fetchall()
    
    if all_rows:
        df_backup = pd.DataFrame(all_rows, columns=["category", "word", "meaning", "example", "date", "wrong_count"])
        csv_data = df_backup.to_csv(index=False).encode('utf-8-sig')
        
        st.download_button(
            label="📥 현재 단어장 CSV 파일로 다운로드 (백업)",
            data=csv_data,
            file_name=f"vocab_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
            mime="text/csv"
        )
    else:
        st.info("데이터베이스에 백업할 단어가 없습니다.")
        
    st.divider()
    
    st.markdown("### 📤 백업 파일로 복구하기")
    uploaded_backup = st.file_uploader("이전에 백업한 CSV 파일을 선택하십시오.", type=["csv"])
    
    if uploaded_backup is not None:
        try:
            df_restore = pd.read_csv(uploaded_backup)
            required_cols = ["category", "word", "meaning", "example", "date", "wrong_count"]
            
            if all(col in df_restore.columns for col in required_cols):
                st.success("올바른 양식의 백업 파일이 확인되었습니다.")
                st.dataframe(df_restore.head(5), use_container_width=True)
                
                restore_mode = st.radio("복구 방식을 선택하십시오.", ["기존 단어장에 이어서 추가하기", "기존 데이터 전부 지우고 새로 덮어쓰기"])
                
                if st.button("🚀 데이터 복구 실행"):
                    if restore_mode == "기존 데이터 전부 지우고 새로 덮어쓰기":
                        c.execute("DELETE FROM vocab")
                        conn.commit()
                        
                    for _, row in df_restore.iterrows():
                        c.execute("INSERT INTO vocab (category, word, meaning, example, date, wrong_count) VALUES (?, ?, ?, ?, ?, ?)",
                                  (str(row['category']), str(row['word']), str(row['meaning']), str(row['example']), str(row['date']), int(row['wrong_count'])))
                    conn.commit()
                    
                    st.success(f"성공적으로 {len(df_restore)}개의 단어 데이터를 복구했습니다!")
                    st.rerun()
            else:
                st.error("업로드된 파일의 형식이 올바르지 않습니다. 필수 열 정보가 누락되었습니다.")
        except Exception as e:
            st.error(f"파일을 파싱하는 중 오류가 발생했습니다: {e}")
