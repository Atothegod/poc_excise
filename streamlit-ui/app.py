# app.py (Streamlit)

import streamlit as st
import os
import json
import pandas as pd

from config import UPLOAD_DIR
from file_manager import (
    ensure_upload_dir,
    upload_file,
    list_files,
    preview_csv,
)

from mapping_manager import (
    init_mapping_state,
    render_mapping_editor,
    render_file_mapping_status,
)

from pipeline_runner import run_script


# =====================================================
# CONFIG
# =====================================================

SHARED_DIR = "shared"
os.makedirs(SHARED_DIR, exist_ok=True)

st.set_page_config(
    page_title="Pipeline Control Panel",
    layout="centered"
)

st.title("Data Pipeline Control Panel")


# =====================================================
# SESSION STATE
# =====================================================

if "running" not in st.session_state:
    st.session_state.running = False


# =====================================================
# INIT
# =====================================================

ensure_upload_dir()
init_mapping_state()


# =====================================================
# UPLOAD CSV
# =====================================================

st.header("Upload CSV")
upload_file(disabled=st.session_state.running)


# =====================================================
# FILE LIST
# =====================================================

st.subheader("Available Files")

selected = list_files(disabled=st.session_state.running)

files = sorted([
    f for f in os.listdir(UPLOAD_DIR)
    if f.lower().endswith(".csv")
    and os.path.isfile(os.path.join(UPLOAD_DIR, f))
])


# =====================================================
# MAPPING
# =====================================================

if files:
    st.divider()
    render_file_mapping_status(files)

    st.divider()
    render_mapping_editor(files)


# =====================================================
# PREVIEW
# =====================================================

if selected:
    st.divider()
    st.subheader(f"{selected} Preview")
    preview_csv(selected)


# =====================================================
# 🔥 COLUMN DESCRIPTION SECTION (ONLY DESCRIPTION)
# =====================================================

if selected:
    st.divider()
    st.header("Column Description (AI Metadata)")

    file_path = os.path.join(UPLOAD_DIR, selected)

    # Robust encoding fallback
    def read_csv_safely(path):
        encodings_to_try = ["utf-8", "utf-8-sig", "cp874", "tis-620", "latin1"]
        for enc in encodings_to_try:
            try:
                return pd.read_csv(path, nrows=5, encoding=enc)
            except Exception:
                continue
        raise Exception("ไม่สามารถอ่านไฟล์ได้ (unsupported encoding)")

    try:
        df_sample = read_csv_safely(file_path)
    except Exception as e:
        st.error(f"❌ ไม่สามารถอ่านไฟล์ได้: {e}")
        df_sample = None

    if df_sample is not None:

        column_meta_path = os.path.join(
            SHARED_DIR,
            f"column_metadata_{selected}.json"
        )

        if os.path.exists(column_meta_path):
            with open(column_meta_path, "r", encoding="utf-8") as f:
                existing_meta = json.load(f)
        else:
            existing_meta = {
                "file_name": selected,
                "columns": []
            }

        updated_columns = []

        for col in df_sample.columns:

            existing_col = next(
                (c for c in existing_meta["columns"]
                 if c["column_name"] == col),
                {}
            )

            st.subheader(f"Column: {col}")

            description = st.text_input(
                "Description",
                value=existing_col.get("description", ""),
                key=f"{col}_desc"
            )

            updated_columns.append({
                "column_name": col,
                "description": description
            })

            st.divider()

        if st.button("Save Column Metadata"):
            final_metadata = {
                "file_name": selected,
                "columns": updated_columns
            }

            with open(column_meta_path, "w", encoding="utf-8") as f:
                json.dump(final_metadata, f, ensure_ascii=False, indent=2)

            st.success("✅ Column metadata saved successfully.")


# =====================================================
# PIPELINE ACTIONS
# =====================================================

st.divider()
st.header("Actions")

col1, col2 = st.columns(2)

with col1:
    st.markdown("### Load")

    if st.button(
        "Transform & Validate (Staging)",
        disabled=st.session_state.running,
        use_container_width=True
    ):
        st.session_state.running = True
        with st.spinner("Running Transform & Validate..."):
            log = run_script("01_ingest_staging.py")
        st.session_state.running = False
        st.code(log)

    if st.button(
        "Push to Production",
        disabled=st.session_state.running,
        use_container_width=True
    ):
        st.session_state.running = True
        with st.spinner("Pushing to Production..."):
            log = run_script("02_staging_prod.py")
        st.session_state.running = False
        st.code(log)


with col2:
    st.markdown("### Maintenance")

    if st.button(
        "Delete Staging Data",
        disabled=st.session_state.running,
        use_container_width=True
    ):
        st.session_state.running = True
        with st.spinner("Deleting Staging..."):
            log = run_script("delete_staging.py")
        st.session_state.running = False
        st.code(log)

    st.markdown("#### ⚠ Delete Production")
    confirm = st.checkbox("Confirm deletion")

    if st.button(
        "Delete Production Data",
        disabled=(st.session_state.running or not confirm),
        use_container_width=True
    ):
        st.session_state.running = True
        with st.spinner("Deleting Production..."):
            log = run_script("delete_prod.py")
        st.session_state.running = False
        st.code(log)