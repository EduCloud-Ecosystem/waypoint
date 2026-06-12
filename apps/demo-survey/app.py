# demo-survey: internal Shiny for Python demo tenant.
# Proves the persistent write path: submissions are appended to a CSV under
# DATA_DIR, which on the server is a Coolify persistent volume (and in
# docker-compose.dev.yml a named volume). Data must survive container restarts
# and be included in backups. No secrets.

import csv
import os
from datetime import datetime, timezone
from pathlib import Path

from shiny import App, reactive, render, ui

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
CSV_PATH = DATA_DIR / "submissions.csv"
HEADER = ["timestamp_utc", "name", "comment"]


def ensure_store() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not CSV_PATH.exists():
        with CSV_PATH.open("w", newline="") as f:
            csv.writer(f).writerow(HEADER)


def read_rows() -> list[list[str]]:
    if not CSV_PATH.exists():
        return []
    with CSV_PATH.open(newline="") as f:
        return list(csv.reader(f))


app_ui = ui.page_fluid(
    ui.h1("demo-survey (team only)"),
    ui.p("Internal demo. Submissions are saved to a persistent volume and "
         "survive restarts, proving the write path the platform provides."),
    ui.input_text("name", "Your name"),
    ui.input_text("comment", "Comment"),
    ui.input_action_button("submit", "Submit"),
    ui.output_text_verbatim("status"),
    ui.h3("Saved submissions"),
    ui.output_table("table"),
)


def server(input, output, session):
    refresh = reactive.value(0)

    @reactive.effect
    @reactive.event(input.submit)
    def _save():
        if not input.name() and not input.comment():
            return
        ensure_store()
        with CSV_PATH.open("a", newline="") as f:
            csv.writer(f).writerow([
                datetime.now(timezone.utc).isoformat(),
                input.name(),
                input.comment(),
            ])
        refresh.set(refresh() + 1)

    @output
    @render.text
    def status():
        refresh()
        rows = read_rows()
        n = max(len(rows) - 1, 0)
        return f"{n} submission(s) on disk at {CSV_PATH}."

    @output
    @render.table
    def table():
        refresh()
        import pandas as pd
        rows = read_rows()
        if len(rows) <= 1:
            return pd.DataFrame(columns=HEADER)
        return pd.DataFrame(rows[1:], columns=rows[0])


app = App(app_ui, server)
