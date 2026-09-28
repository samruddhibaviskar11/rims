"""Streamlit frontend for the RIMS automation pipeline."""
import sqlite3
from pathlib import Path

import pandas as pd
import requests
import streamlit as st

import build_database
import chat_with_data as chat
import main as pipeline

BASE_DIR = Path(__file__).resolve().parent
INPUT_DIR = BASE_DIR / "input"
OUTPUT_DIR = BASE_DIR / "output"
INPUT_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)
OUTPUT_FILES = [
    ("RRP_Changes.csv", "RRP Changes"),
    ("End_Date_Changes.csv", "End Date Changes"),
    ("New_Items.csv", "New Items"),
    ("Other_Changes.csv", "Other Changes"),
]

st.set_page_config(page_title="RIMS Automation", layout="wide")
st.title("RIMS Automation")
tab_run, tab_outputs, tab_chat = st.tabs(
    ["Run Pipeline", "View Outputs", "Chat with Data"]
)

with tab_run:
    st.header("1. Provide input files")
    st.caption(f"Files are saved into `{INPUT_DIR}` and reused across runs.")
    supported_exts = pipeline.SUPPORTED_EXTENSIONS
    uploader_types = [ext.lstrip(".") for ext in supported_exts]
    existing = sorted(p.name for p in INPUT_DIR.iterdir()
                      if p.is_file() and p.suffix.lower() in supported_exts)
    if existing:
        st.write("Currently in `input/`:", ", ".join(existing))
    st.caption("The Charge Sheet and RIMS files are detected by their columns, not filenames.")
    charge_file = st.file_uploader("Charge Sheet (any filename)", type=uploader_types)
    rims_files = st.file_uploader(
        "RIMS daily files (any filenames)", type=uploader_types,
        accept_multiple_files=True,
    )
    if st.button("Save uploaded files to input/"):
        saved = []
        for uploaded in ([charge_file] if charge_file is not None else []) + list(rims_files or []):
            safe_name = Path(uploaded.name).name
            if safe_name != uploaded.name:
                st.error(f"Invalid filename: {uploaded.name}")
                continue
            (INPUT_DIR / safe_name).write_bytes(uploaded.getvalue())
            saved.append(safe_name)
        if saved:
            st.success(f"Saved: {', '.join(saved)}")
            st.rerun()
        else:
            st.warning("No files selected.")

    st.divider()
    st.header("2. Run")
    enable_llm = st.checkbox(
        "Enable local-LLM fuzzy match for 'Not Present' SKUs (requires Ollama)",
        value=False,
    )
    if st.button("Run Pipeline", type="primary"):
        try:
            with st.spinner("Reading files and building outputs..."):
                charge_raw, charge_path, rims_paths = pipeline.classify_input_files(INPUT_DIR)
                charge_lookup = pipeline.load_charge_sheet(charge_raw)
                rims_all = pipeline.load_rims_files(rims_paths)
                rrp_df, end_df, new_df, other_df = pipeline.filter_rims(rims_all)
                rrp_out = pipeline.build_rrp_output(rrp_df, charge_lookup)
                end_out = pipeline.build_end_date_output(end_df, charge_lookup)
                new_out = pipeline.build_new_items_output(new_df)
                other_out = pipeline.build_other_changes_output(other_df, charge_lookup)
                if enable_llm:
                    from llm_match import add_llm_suggestions
                    charge_full = charge_raw.copy()
                    if "Model" not in charge_full.columns:
                        raise ValueError("LLM matching requires a Model column in the Charge Sheet")
                    charge_full["SKU"] = charge_full["SKU"].astype("string").str.strip()
                    charge_full["Model"] = charge_full["Model"].astype("string").str.strip()
                    if not rrp_out.empty:
                        rrp_out = add_llm_suggestions(rrp_out, charge_full)
                    if not end_out.empty:
                        end_out = add_llm_suggestions(end_out, charge_full)
                results = [rrp_out, end_out, new_out, other_out]
                for (filename, _), frame in zip(OUTPUT_FILES, results):
                    frame.to_csv(OUTPUT_DIR / filename, index=False)
            st.success(f"Pipeline complete. Charge Sheet: {charge_path.name}; RIMS files: {len(rims_paths)}")
            for column, (_, label), frame in zip(st.columns(4), OUTPUT_FILES, results):
                column.metric(label, len(frame))
        except (FileNotFoundError, ValueError) as exc:
            st.error(str(exc))
        except requests.exceptions.ConnectionError:
            st.error("Cannot reach Ollama. Check that it is running.")
        except Exception as exc:
            st.error(f"Something went wrong: {exc}")

with tab_outputs:
    st.header("Output files")
    any_found = False
    for filename, label in OUTPUT_FILES:
        path = OUTPUT_DIR / filename
        if not path.exists():
            continue
        any_found = True
        try:
            df = pd.read_csv(path)
            st.subheader(f"{label} ({len(df)} rows)")
            st.dataframe(df, use_container_width=True)
            st.download_button(
                f"Download {filename}", data=path.read_bytes(),
                file_name=filename, mime="text/csv", key=f"dl_{filename}",
            )
        except Exception as exc:
            st.error(f"Could not open {filename}: {exc}")
        st.divider()
    if not any_found:
        st.info("No outputs yet. Run the pipeline first.")

with tab_chat:
    st.header("Chat with your data")
    st.caption("Local Ollama required. Run `ollama pull llama3.2` and start Ollama.")
    if st.button("Build / rebuild database from input/ and output/"):
        try:
            with st.spinner("Building database..."):
                build_database.main()
            st.success(f"Database ready at {build_database.DB_PATH}")
        except Exception as exc:
            st.error(f"Could not build database: {exc}")
    if not chat.DB_PATH.exists():
        st.warning("No database found. Build it using the button above.")
    else:
        if "chat_history" not in st.session_state:
            st.session_state.chat_history = []
        for role, content in st.session_state.chat_history:
            with st.chat_message(role):
                st.write(content)
        question = st.chat_input("Ask a question about your data...")
        if question:
            st.session_state.chat_history.append(("user", question))
            with st.chat_message("user"):
                st.write(question)
            with st.chat_message("assistant"):
                try:
                    with st.spinner("Thinking..."):
                        with sqlite3.connect(chat.DB_PATH) as conn:
                            schema = chat.get_schema(conn)
                            sql = chat.question_to_sql(question, schema)
                            st.code(sql, language="sql")
                            columns, rows = chat.run_query(conn, sql)
                            answer = chat.results_to_answer(
                                question, sql,
                                [dict(zip(columns, row)) for row in rows], columns,
                            )
                    st.write(answer)
                    if rows:
                        st.dataframe(pd.DataFrame(rows, columns=columns), use_container_width=True)
                    st.session_state.chat_history.append(("assistant", answer))
                except requests.exceptions.ConnectionError:
                    msg = "Cannot reach Ollama at localhost:11434. Is it running?"
                    st.error(msg)
                    st.session_state.chat_history.append(("assistant", msg))
                except Exception as exc:
                    msg = f"Something went wrong: {exc}"
                    st.error(msg)
                    st.session_state.chat_history.append(("assistant", msg))