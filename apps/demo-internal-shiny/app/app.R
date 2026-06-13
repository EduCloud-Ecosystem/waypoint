# demo-internal-shiny: internal (team-only) R Shiny demo tenant.
# Identical app shape to the public one; the difference is the auth GATE applied
# outside the app by oauth2-proxy (Phase 3). App code carries no auth logic.
# It optionally reads the forwarded user header oauth2-proxy sets, to show who
# the proxy authenticated, proving the gate is in front of it.

library(shiny)

ui <- fluidPage(
  titlePanel("demo-internal-shiny (team only)"),
  sidebarLayout(
    sidebarPanel(
      sliderInput("bins", "Bins", min = 5, max = 50, value = 20)
    ),
    mainPanel(
      p("Internal demo app. Reaching this page at all means the Keycloak gate ",
        "in front of it let you through."),
      textOutput("whoami"),
      plotOutput("plot")
    )
  )
)

server <- function(input, output, session) {
  output$whoami <- renderText({
    # The oauth2-proxy forward-auth gate sets X-Auth-Request-User on the request
    # it lets through (Traefik forwards it via authResponseHeaders). Fall back to
    # X-Forwarded-User in case a deployment passes the legacy header instead.
    user <- session$request$HTTP_X_AUTH_REQUEST_USER
    if (is.null(user) || user == "") user <- session$request$HTTP_X_FORWARDED_USER
    if (is.null(user) || user == "") {
      "No forwarded user header seen (running without the proxy in front)."
    } else {
      paste("Authenticated as:", user)
    }
  })

  output$plot <- renderPlot({
    hist(faithful$eruptions, breaks = input$bins,
         main = "Old Faithful eruptions", col = "seagreen", border = "white")
  })
}

shinyApp(ui, server)
