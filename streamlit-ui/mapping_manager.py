# mapping_manager.py

import streamlit as st
import pandas as pd
import json
import os
from config import MAPPING_FILE


def init_mapping_state():
    if "file_table_mapping" not in st.session_state:
        st.session_state.file_table_mapping = {}

    if os.path.exists(MAPPING_FILE):
        with open(MAPPING_FILE, "r") as f:
            st.session_state.file_table_mapping = json.load(f)


def persist_mapping():
    with open(MAPPING_FILE, "w") as f:
        json.dump(st.session_state.file_table_mapping, f, indent=2)


def render_file_mapping_status(files):
    st.markdown("### File Status")

    data = []
    for f in files:
        mapped = st.session_state.file_table_mapping.get(f)
        data.append({
            "CSV File": f,
            "Mapped Table": mapped if mapped else "—",
            "Status": "✅ Mapped" if mapped else "⚠ Not Mapped"
        })

    df = pd.DataFrame(data)
    st.dataframe(df, use_container_width=True)


def render_mapping_editor(files):
    st.markdown("### Assign Table Name")

    selected_file = st.selectbox(
        "Select CSV to assign table name",
        files
    )

    current_value = st.session_state.file_table_mapping.get(
        selected_file,
        selected_file.replace(".csv", "")
    )

    custom_table = st.text_input(
        "Table name",
        value=current_value
    )

    col1, col2 = st.columns(2)

    with col1:
        if st.button("💾 Save Mapping"):
            st.session_state.file_table_mapping[selected_file] = custom_table
            persist_mapping()
            st.success(f"{selected_file} → {custom_table}")
            st.rerun()

    with col2:
        if st.button("❌ Remove Mapping"):
            if selected_file in st.session_state.file_table_mapping:
                del st.session_state.file_table_mapping[selected_file]
                persist_mapping()
                st.warning("Mapping removed")
                st.rerun()