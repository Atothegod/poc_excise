# 113-pea-oms

# OMS Connect: AI-Enhanced Complaint and Data Management System

**Project Title:** โครงการวิเคราะห์และศึกษาการพัฒนาแพลตฟอร์มบริหารจัดการข้อมูลข้อร้องเรียนด้านไฟฟ้าขัดข้องด้วย AI กรณีนำร่องที่จังหวัดปทุมธานี (OMS AI-Enhanced Complaint and Data Management System) [cite: 9, 21, 22]  
**Pilot Location:** Pathum Thani, Thailand [cite: 10]  
**Owner:** Provincial Electricity Authority (PEA) [cite: 14]  
**Developer:** Thammasat University Research and Consultancy Institute [cite: 13]

---

## 📖 Table of Contents
- [Project Overview](#-project-overview)
- [Key Features](#-key-features)
- [System Architecture](#-system-architecture)
- [Tech Stack](#-tech-stack)
- [Research Methodology](#-research-methodology)
- [Installation & Setup](#-installation--setup)
- [Project Roadmap](#-project-roadmap)
- [License & Intellectual Property](#-license--intellectual-property)

---

## 💡 Project Overview

**OMS Connect** is a pilot initiative designed to transform how the Provincial Electricity Authority (PEA) handles power outage complaints. Currently, PEA faces challenges with scattered data across multiple channels (Line, Facebook, Web) and reactive responses[cite: 30, 31].

This project aims to develop a centralized **Omnichannel Platform** powered by **AI Agents (LLMs)** and **Data Analytics** to:
1.  Centralize complaint management[cite: 38].
2.  Provide accurate **Estimated Time to Restoration (ETR)**[cite: 37].
3.  Proactively identify outage risks using Graph Databases and Weather/Social data[cite: 46, 50].
4.  Enhance customer experience with empathetic AI communication[cite: 37].

---

## ✨ Key Features

### 1. Omnichannel Integration
- **Centralized Hub:** Aggregates complaints from LINE, Facebook, and Web APIs into a single platform[cite: 45].
- **Graph Database:** Maps relationships between users, assets, and service areas to perform Root Cause Analysis[cite: 46].
- **External Data Injection:** Integrates weather data (15-min intervals), local news, and social media sentiment for context[cite: 50, 51, 52].

### 2. Intelligent LLM Agents
- **Automated Response:** Handles FAQs and routine inquiries[cite: 64].
- **ETR Calculation:** Estimates restoration time based on asset status, field resource allocation, and weather conditions[cite: 231].
- **Empathetic AI:** Trained to provide comforting and polite responses to reduce customer anxiety during outages[cite: 37].
- **Contact Account (CA) Verification:** Verifies user identity against the customer database[cite: 65].

### 3. Operational Dashboard & HIL
- **Kanban Board:** Visualizes ticket status (e.g., "Pending", "In Progress", "Resolved")[cite: 68, 69].
- **Human-in-the-Loop (HIL):** Allows human operators to review, approve, or reject AI-generated proactive alerts before they are sent[cite: 235].
- **Geospatial Visualization:** Real-time heatmap of outages and complaints in Pathum Thani[cite: 47, 52].

---

## 🏗 System Architecture

The system connects public-facing channels with PEA's internal Outage Management System (OMS).


**High-Level Flow:**
1.  **User Input:** Complaints come via Line/Facebook[cite: 129].
2.  **Omnichannel Layer:** Consolidates data[cite: 330].
3.  **LLM Agent:**
    - Verifies Identity (CA)[cite: 323].
    - Queries Knowledge Hub & Graph DB[cite: 338].
    - Generates response & ETR[cite: 231].
4.  **Kanban/Dashboard:** Branch managers track status[cite: 325].
5.  **PEA OMS Interface:** Field technicians receive work orders and report status back to the system[cite: 327].

---

## 🛠 Tech Stack

Based on the technical specifications[cite: 454, 456, 457]:

| Component | Technology |
| :--- | :--- |
| **Frontend** | React.js, Tailwind CSS (implied by "Modern Framework") [cite: 73, 457] |
| **Backend API** | Python, Django [cite: 457] |
| **AI / LLM** | OpenAI GPT, Google Gemini, Hugging Face Transformers [cite: 454, 457] |
| **Conversation Mgmt** | Chatwoot [cite: 457] |
| **Data Pipelines** | Kedro, Apache Airflow [cite: 457] |
| **Databases** | PostgreSQL (Relational), Redis (Cache), ClickHouse (Analytics), Minio (Storage) [cite: 456, 457] |
| **Visualization** | Tableau or Superset [cite: 454] |
| **Infrastructure** | Cloud-based with High Security [cite: 57] |

---

## 🔬 Research Methodology

This project follows a **6-month Research & Development timeline** focusing on three key research questions[cite: 224, 229, 235]:

1.  **Data Integration:** How to structure multi-modal data (Social, Weather, Assets) into a Graph DB for proactive detection?
2.  **LLM Architecture:** Testing architectures for accurate ETR estimation and empathetic "Comforting Agents."
3.  **Human-in-the-Loop (HIL):** Designing dashboards that balance AI autonomy with human oversight for critical alerts.

---

## ⚙️ Installation & Setup

*(Note: This section is a placeholder for the actual implementation details)*

### Prerequisites
- Python 3.9+
- Node.js 16+
- Docker & Docker Compose
- PostgreSQL

### Local Development

1.  **Clone the repository**
    ```bash
    git clone https://gitlab.com/storemesh/project/pea/113-pea-oms.git
    cd 113-pea-oms.git
    ```

2.  **Environment Variables**
    Create a `.env` file based on `.env.example`:
    ```bash
    cp .env.example .env
    # Configure LLM API Keys (OpenAI/Gemini), DB credentials, and PEA OMS Endpoints
    ```

3.  **Start Services (Docker)**
    ```bash
    docker-compose up -d --build
    ```

4.  **Run Migrations**
    ```bash
    docker-compose exec backend python manage.py migrate
    ```

---

## 📅 Project Roadmap

The project is executed over 6 months[cite: 173, 462]:

* **Month 1-2:** Requirement Gathering, System Design, Graph DB Setup[cite: 546].
* **Month 3:** Prototype Development (Omnichannel & LLM Agent)[cite: 558].
* **Month 4:** Pilot Launch in Pathum Thani & HIL Dashboard Implementation[cite: 571].
* **Month 5:** Testing, Feedback Collection, and System Tuning[cite: 608].
* **Month 6:** Final Evaluation, Knowledge Transfer (OJT), and Scaling Plan[cite: 433, 577].

---

## 📜 License & Intellectual Property

* **Ownership:** All Intellectual Property (IP), including the platform, software, and data models, belongs **100% to the Provincial Electricity Authority (PEA)**[cite: 484].
* **Usage:** For research and internal PEA operations only.

---

*Generated based on the OMS Research Grant Form (2025-09-30).*