# StoreMesh Data Pipeline POC

An end-to-end proof of concept for loading CSV files into PostgreSQL, validating and promoting them through `staging` and `production`, and querying the results with an LLM-powered chat assistant.

## What is included

- `streamlit-ui/`: a control panel for uploading CSV files, mapping file names to table names, editing column descriptions, and running pipeline actions.
- `pipeline/`: ingestion, transformation, validation, promotion, and cleanup scripts for PostgreSQL.
- `chainlit-app/`: a chat-based SQL assistant that generates and runs queries against the database.
- `ad_hoc/`: CSV input files and the file-to-table mapping JSON.
- `shared/`: per-file column metadata used by the Chainlit assistant.
- `data/`: local SQLite artifacts that appear to be used for experimentation and reference.

## Architecture

1. Upload CSV files through the Streamlit control panel.
2. Map each file name to a target table name.
3. Optionally add human-readable column descriptions.
4. Run `Transform & Validate (Staging)` to load files into the `staging` schema.
5. Run `Push to Production` to replace the corresponding tables in the `production` schema.
6. Use the Chainlit assistant to ask questions in natural language and generate SQL against the database.

## Service Overview

| Service | Port | Purpose |
| --- | --- | --- |
| PostgreSQL | `5432` | Stores the `staging` and `production` schemas |
| Streamlit | `8501` | CSV upload, table mapping, metadata editing, and pipeline controls |
| Chainlit | `8500` | LLM assistant for SQL generation and querying |

## Requirements

- Docker
- Docker Compose
- A `.env` file with database and LLM credentials

## Environment Variables

The project reads the following variables from `.env`:

| Variable | Used by | Purpose |
| --- | --- | --- |
| `PROJECT_NAME` | `docker-compose.yml` | Prefix for container names |
| `POSTGRES_USER` | Pipeline and Chainlit apps | PostgreSQL username |
| `POSTGRES_PASSWORD` | Pipeline and Chainlit apps | PostgreSQL password |
| `LLM_DB` | Pipeline and Chainlit apps | Database name used for the app |
| `LLM_URL` | Chainlit app variants that use LiteLLM | Base URL for the local or remote LLM endpoint |
| `API_KEY` | Chainlit app variants that use LiteLLM | API key for the model endpoint |
| `API_KEY_4` | `app_04_gemini2.5.py` | API key for the Gemini-based Chainlit app started by Compose |

If you switch Chainlit entrypoints, check the relevant file in `chainlit-app/` for the exact LLM variables it expects.

## Quick Start

1. Create a `.env` file in the project root.
2. Fill in the required database and model variables.
3. Start the full stack:

```bash
docker compose up --build
```

4. Open the apps:
`Streamlit`: http://localhost:8501
`Chainlit`: http://localhost:8500
`PostgreSQL`: localhost:5432

## Typical Workflow

### 1. Upload and prepare data

Use the Streamlit app to:

- upload CSV files into `ad_hoc/`
- assign each file a target table name
- preview the CSV content
- add column descriptions that are saved to `shared/column_metadata_<file>.json`

### 2. Stage the files

Click **Transform & Validate (Staging)** in Streamlit.

This runs `pipeline/01_ingest_staging.py`, which:

- loads `ad_hoc/file_table_mapping.json`
- reads all CSV files in `ad_hoc/`
- normalizes Thai month columns
- casts obvious date, numeric, and boolean columns
- writes each file into the `staging` schema
- validates the result against the `production` schema

### 3. Promote to production

Click **Push to Production** in Streamlit.

This runs `pipeline/02_staging_prod.py`, which replaces the matching table in `production` with the staged version.

### 4. Query the data

Open the Chainlit app and ask questions in natural language.

The default Compose command starts `chainlit-app/app_04_gemini2.5.py`, which:

- connects to PostgreSQL
- reads schema metadata and column descriptions from `shared/`
- builds SQL with an LLM
- executes safe read-only queries
- returns the result to chat

## Manual Pipeline Commands

If you want to run scripts directly inside the pipeline container:

```bash
docker compose exec pipeline python 01_ingest_staging.py
docker compose exec pipeline python 02_staging_prod.py
docker compose exec pipeline python delete_staging.py
docker compose exec pipeline python delete_prod.py
```

## Data Notes

- CSV parsing expects `cp874` by default in the pipeline.
- The Streamlit uploader tries a few encodings when previewing files.
- Thai month names are normalized into additional `*_NUM` columns when a column looks like a month field.
- Column descriptions saved in `shared/` are injected into the Chainlit prompt to improve SQL generation.

## Repository Layout

```text
.
|- ad_hoc/                # CSV uploads and file-table mapping
|- chainlit-app/          # LLM chat app and prompt configs
|- data/                  # SQLite sample databases / local artifacts
|- pipeline/              # Ingest, validate, promote, cleanup scripts
|- shared/                # Column metadata JSON files
|- streamlit-ui/          # Upload and pipeline control panel
|- docker-compose.yml     # Full stack definition
`- README.md
```

## Notes

- The production delete action is exposed in the Streamlit UI and should be used carefully.
- The Chainlit app reads the active schema from its chat settings, so you can switch between `public` and `production` depending on the deployed entrypoint.
- If you change the default Chainlit file in Compose, update the README and the `command` in `docker-compose.yml` together.
