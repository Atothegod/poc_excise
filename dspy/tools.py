from agent_tools import Check_Outage_Tool
from django_client import (
    CA_NUMBER_PATTERN,
    DJANGO_API_URL,
    _normalize_dialog_history,
    fetch_session_context,
    is_valid_ca_number,
    record_closed_loop_response,
    save_report_to_db,
    sync_chat_history_to_db,
)
from response_formatters import (
    format_branch_label,
    format_eta_detail,
    format_etr_label,
    format_mass_outage_label,
    join_branch_eta_etr,
)
from session_state import (
    current_login_ca_number,
    current_pdpa_consent,
    current_session_id,
    current_time_stamp,
    get_latest_outage,
    latest_outage_by_session,
    remember_latest_outage,
    restore_latest_outage,
)
from time_utils import (
    current_authoritative_time,
    format_eta_label,
    format_time_only,
    parse_iso_datetime,
)


_format_etr_label = format_etr_label
_format_mass_outage_label = format_mass_outage_label
_format_branch_label = format_branch_label
_format_eta_detail = format_eta_detail
_join_branch_eta_etr = join_branch_eta_etr


__all__ = [
    "CA_NUMBER_PATTERN",
    "DJANGO_API_URL",
    "Check_Outage_Tool",
    "_format_branch_label",
    "_format_eta_detail",
    "_format_etr_label",
    "_format_mass_outage_label",
    "_join_branch_eta_etr",
    "_normalize_dialog_history",
    "current_authoritative_time",
    "current_login_ca_number",
    "current_pdpa_consent",
    "current_session_id",
    "current_time_stamp",
    "fetch_session_context",
    "format_branch_label",
    "format_eta_detail",
    "format_eta_label",
    "format_etr_label",
    "format_mass_outage_label",
    "format_time_only",
    "get_latest_outage",
    "is_valid_ca_number",
    "join_branch_eta_etr",
    "latest_outage_by_session",
    "parse_iso_datetime",
    "record_closed_loop_response",
    "remember_latest_outage",
    "restore_latest_outage",
    "save_report_to_db",
    "sync_chat_history_to_db",
]
