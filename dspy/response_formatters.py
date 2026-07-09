from time_utils import format_time_only


def format_etr_label(etr, etr_source=None):
    if not etr:
        return None
    formatted_etr = format_time_only(etr)
    return f"คาดว่าจะจ่ายไฟคืนประมาณ {formatted_etr or etr}"


def format_mass_outage_label(etr_label=None):
    if etr_label:
        return f"ขณะนี้เกิดเหตุไฟดับวงกว้างในพื้นที่ค่ะ {etr_label} ค่ะ"
    return "ขณะนี้เกิดเหตุไฟดับวงกว้างในพื้นที่ค่ะ ระบบกำลังประเมินเวลาไฟกลับล่าสุดค่ะ"


def format_branch_label(fastest_branch):
    if not fastest_branch:
        return None
    return f"{fastest_branch} รับเรื่องแล้วค่ะ"


def format_eta_detail(eta_label):
    if not eta_label:
        return None
    return f"ช่างจะถึงหน้างานประมาณ {eta_label}"


def join_branch_eta_etr(branch_label=None, eta_label=None, etr_label=None):
    eta_detail = format_eta_detail(eta_label)
    details = []

    if branch_label and eta_detail:
        details.append(f"{branch_label} {eta_detail}")
    elif branch_label:
        details.append(branch_label)
    elif eta_detail:
        details.append(eta_detail)

    if etr_label:
        details.append(etr_label)

    return " และ".join(details)
