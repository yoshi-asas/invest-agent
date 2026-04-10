import streamlit as st
import auth

# ページ全体の基本設定
st.set_page_config(page_title="プロ野球ダッシュボード", layout="wide")

# パスワードチェック
if not auth.check_password():
    st.stop()

# ページ定義
page_sabermetrics = st.Page("views/sabermetrics.py", title="セイバーメトリクス", icon="⚾")
page_standings = st.Page("views/standings.py", title="順位表", icon="🏆")

# ナビゲーション設定と実行
pg = st.navigation([page_sabermetrics, page_standings])
pg.run()
