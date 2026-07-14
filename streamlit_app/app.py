"""PDF Bridge operator workspace — Streamlit entry point.

Run from the repository root so the bundled theme applies:

    streamlit run streamlit_app/app.py

The app is a pure HTTP client of a running PDF Bridge service; it holds an
anonymous browser session with a CSRF token exactly like the built-in UI and
never opens the catalog database directly.
"""

from __future__ import annotations

import streamlit as st

import bridge_ui as ui

st.set_page_config(
    page_title="PDF Bridge",
    page_icon=":material/picture_as_pdf:",
    layout="wide",
    initial_sidebar_state="expanded",
)

if "bridge_base_url" not in st.session_state:
    st.session_state["bridge_base_url"] = ui.DEFAULT_BASE_URL

pages = [
    st.Page("views/overview.py", title="Overview", icon=":material/dashboard:", default=True),
    st.Page("views/upload.py", title="Upload", icon=":material/upload_file:"),
    st.Page("views/workspace.py", title="Review queue", icon=":material/fact_check:"),
    st.Page("views/library.py", title="Library", icon=":material/library_books:"),
    st.Page("views/search.py", title="Search", icon=":material/search:"),
]

navigation = st.navigation(pages)
ui.render_connection_settings()
navigation.run()
