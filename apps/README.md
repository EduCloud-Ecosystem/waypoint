# apps/: synthetic demo tenants (Phase 1.5 pre-build)

One directory per tenant in docs/TENANTS.md. Each app is hello-world level but
genuinely exercises one platform behavior, and each ships with its final
Dockerfile (instantiated from templates/). These are the exact artifacts that
deploy to Coolify in Phases 4 through 6; only hostnames and the dev scheme
change on the server.

| Directory           | Framework        | Proves                          |
|---------------------|------------------|---------------------------------|
| demo-public-shiny   | R Shiny          | public load, interactive, no gate |
| demo-internal-shiny | R Shiny          | internal app behind the auth gate |
| demo-dash-secret    | Dash             | env/secret read (DEMO_API_KEY)  |
| demo-survey         | Shiny for Python | persistent write to DATA_DIR    |
| demo-static-report  | static HTML      | static serving                  |

Rules carried from the onboarding contract: every app binds 0.0.0.0 on its
EXPOSEd port, uses relative paths, and reads secrets only from the environment
(never from source). The auth gate for internal apps is applied OUTSIDE the app
by oauth2-proxy (Phase 3 / docker-compose.dev.yml), not in app code.
