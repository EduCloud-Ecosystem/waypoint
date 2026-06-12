# demo-dash-secret: internal Dash demo tenant.
# Proves env/secret handling: the app reads DEMO_API_KEY from the environment
# (injected by Coolify's env UI on the server, or by docker-compose.dev.yml
# locally). The value never lives in this repo. The app reports only a MASKED
# form and the length, never the raw secret.

import os

import dash
from dash import Input, Output, dcc, html

app = dash.Dash(__name__)
server = app.server  # WSGI entrypoint for gunicorn (app:server)


def key_status() -> str:
    key = os.environ.get("DEMO_API_KEY", "")
    if not key:
        return "DEMO_API_KEY is NOT set. Inject it via the env UI (never the repo)."
    masked = f"{key[:2]}***{key[-2:]}" if len(key) > 4 else "***"
    return f"DEMO_API_KEY is set. Masked: {masked}. Length: {len(key)}."


app.layout = html.Div(
    style={"fontFamily": "system-ui, sans-serif", "maxWidth": "640px",
           "margin": "2rem auto", "lineHeight": "1.5"},
    children=[
        html.H1("demo-dash-secret"),
        html.P("Internal demo on the EduCloud hosting layer. Reaching this page "
               "means the Keycloak gate let you through. The button below reads "
               "a secret from the environment to prove env/secret handling."),
        html.Button("Check secret", id="check", n_clicks=0),
        html.Pre(id="out",
                 style={"background": "#f4f4f4", "padding": "1rem",
                        "borderRadius": "6px"}),
    ],
)


@app.callback(Output("out", "children"), Input("check", "n_clicks"))
def show(_n_clicks):
    return key_status()


if __name__ == "__main__":
    # Local dev convenience only; in containers gunicorn serves app:server.
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), debug=False)
