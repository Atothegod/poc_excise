# file_manager.py

import os
import pandas as pd
import streamlit as st
from pathlib import Path
from config import UPLOAD_DIR, CSV_ENCODING


def ensure_upload_dir():
    os.makedirs(UPLOAD_DIR, exist_ok=True)


def upload_file(disabled=False):
    uploaded_file = st.file_uploader(
        "Upload CSV",
        type=["csv"],
        disabled=disabled
    )

    if uploaded_file:

        file_name = uploaded_file.name
        file_ext = Path(file_name).suffix.lower()

        if file_ext != ".csv":
            st.error("Only .csv files allowed")
            st.stop()

        save_path = os.path.join(UPLOAD_DIR, file_name)

        with open(save_path, "wb") as f:
            f.write(uploaded_file.getbuffer())

        st.success(f"Saved: {file_name}")


def list_files(disabled=False):

    files = sorted([
        f for f in os.listdir(UPLOAD_DIR)
        if f.lower().endswith(".csv")
        and os.path.isfile(os.path.join(UPLOAD_DIR, f))
    ])

    if not files:
        st.info("No CSV files available.")
        return None

    selected = st.selectbox(
        "Select file to preview",
        files,
        disabled=disabled
    )

    col1, col2 = st.columns([3, 1])

    with col1:
        st.write(f"Selected: **{selected}**")

    with col2:
        if st.button("Delete File", disabled=disabled):
            os.remove(os.path.join(UPLOAD_DIR, selected))
            st.success("File deleted")
            st.rerun()

    return selected


def preview_csv(file_name):
    file_path = os.path.join(UPLOAD_DIR, file_name)

    try:
        df = pd.read_csv(file_path, encoding=CSV_ENCODING)
        st.dataframe(df, use_container_width=True)
        st.caption(f"Rows: {len(df)} | Columns: {len(df.columns)}")
    except Exception as e:
        st.error(f"Failed to read CSV: {e}")