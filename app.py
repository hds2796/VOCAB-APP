# ... existing code ...
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
# ... existing code ...
