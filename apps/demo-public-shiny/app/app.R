# demo-public-shiny: public R Shiny demo tenant.
# Proves a public app loads and is interactive (WebSockets through the proxy)
# with NO auth challenge. No secrets, no data writes.

library(shiny)

ui <- fluidPage(
  titlePanel("demo-public-shiny"),
  sidebarLayout(
    sidebarPanel(
      sliderInput("n", "Number of points", min = 10, max = 500, value = 100),
      selectInput("col", "Colour",
                  choices = c("steelblue", "tomato", "seagreen"),
                  selected = "steelblue")
    ),
    mainPanel(
      p("Public demo app on the EduCloud hosting layer. No login required."),
      plotOutput("plot"),
      verbatimTextOutput("summary")
    )
  )
)

server <- function(input, output, session) {
  samples <- reactive(rnorm(input$n))

  output$plot <- renderPlot({
    hist(samples(),
         main = paste(input$n, "random normals"),
         col = input$col, border = "white")
  })

  output$summary <- renderPrint({
    summary(samples())
  })
}

shinyApp(ui, server)
