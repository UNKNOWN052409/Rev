// Rev Firecrawl — Self-Hosted Web Intelligence Service (Rust)
// ============================================================
// Firecrawl replacement: web search + scraping as a standalone binary.
// No Docker, no external dependencies — just run the binary.
//
// Endpoints:
//   GET  /health          — service health check
//   POST /v1/search       — web search (SERP scraping)
//   POST /v1/scrape       — URL → markdown/html
//   POST /v1/map          — discover URLs on a site
//   POST /v1/extract      — LLM-ready content extraction
//
// Usage:
//   ./rev-firecrawl --port 3002

mod scrape;
mod search;

use axum::{
    extract::State,
    http::StatusCode,
    response::Json,
    routing::{get, post},
    Router,
};
use serde::{Deserialize, Serialize};
use std::net::SocketAddr;
use tower_http::cors::CorsLayer;
use tower_http::trace::TraceLayer;

#[derive(Clone)]
struct AppState {
    client: reqwest::Client,
}

// ---------- Request/Response types ----------

#[derive(Deserialize)]
struct SearchRequest {
    query: String,
    #[serde(default = "default_limit")]
    limit: usize,
}

fn default_limit() -> usize {
    5
}

#[derive(Serialize)]
struct SearchResponse {
    success: bool,
    data: Vec<SearchResult>,
}

#[derive(Serialize, Clone)]
struct SearchResult {
    title: String,
    url: String,
    description: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    markdown: Option<String>,
}

#[derive(Deserialize)]
struct ScrapeRequest {
    url: String,
    #[serde(default)]
    formats: Vec<String>,
}

#[derive(Serialize)]
struct ScrapeResponse {
    success: bool,
    data: ScrapeData,
}

#[derive(Serialize)]
struct ScrapeData {
    #[serde(skip_serializing_if = "Option::is_none")]
    markdown: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    html: Option<String>,
    metadata: ScrapeMetadata,
}

#[derive(Serialize)]
struct ScrapeMetadata {
    title: String,
    url: String,
    status: u16,
}

#[derive(Deserialize)]
struct MapRequest {
    url: String,
}

#[derive(Serialize)]
struct MapResponse {
    success: bool,
    links: Vec<String>,
}

#[derive(Serialize)]
struct HealthResponse {
    status: String,
    version: String,
    service: String,
}

// ---------- Handlers ----------

async fn health() -> Json<HealthResponse> {
    Json(HealthResponse {
        status: "ok".to_string(),
        version: env!("CARGO_PKG_VERSION").to_string(),
        service: "rev-firecrawl".to_string(),
    })
}

async fn handle_search(
    State(state): State<AppState>,
    Json(req): Json<SearchRequest>,
) -> Result<Json<SearchResponse>, (StatusCode, String)> {
    tracing::info!("search: {}", req.query);
    match search::web_search(&state.client, &req.query, req.limit).await {
        Ok(results) => Ok(Json(SearchResponse {
            success: true,
            data: results,
        })),
        Err(e) => Err((StatusCode::INTERNAL_SERVER_ERROR, e.to_string())),
    }
}

async fn handle_scrape(
    State(state): State<AppState>,
    Json(req): Json<ScrapeRequest>,
) -> Result<Json<ScrapeResponse>, (StatusCode, String)> {
    tracing::info!("scrape: {}", req.url);
    match scrape::scrape_url(&state.client, &req.url, &req.formats).await {
        Ok(data) => Ok(Json(ScrapeResponse {
            success: true,
            data,
        })),
        Err(e) => Err((StatusCode::INTERNAL_SERVER_ERROR, e.to_string())),
    }
}

async fn handle_map(
    State(state): State<AppState>,
    Json(req): Json<MapRequest>,
) -> Result<Json<MapResponse>, (StatusCode, String)> {
    tracing::info!("map: {}", req.url);
    match scrape::map_urls(&state.client, &req.url).await {
        Ok(links) => Ok(Json(MapResponse {
            success: true,
            links,
        })),
        Err(e) => Err((StatusCode::INTERNAL_SERVER_ERROR, e.to_string())),
    }
}

// ---------- Main ----------

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    // Init logging
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "rev_firecrawl=info,tower_http=info".into()),
        )
        .init();

    // Parse args
    let args: Vec<String> = std::env::args().collect();
    let port: u16 = args
        .iter()
        .position(|a| a == "--port")
        .and_then(|i| args.get(i + 1))
        .and_then(|p| p.parse().ok())
        .unwrap_or(3002);

    // Build HTTP client
    let client = reqwest::Client::builder()
        .user_agent(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) \
             AppleWebKit/537.36 (KHTML, like Gecko) \
             Chrome/131.0.0.0 Safari/537.36",
        )
        .timeout(std::time::Duration::from_secs(30))
        .build()?;

    let state = AppState { client };

    // Build router
    let app = Router::new()
        .route("/health", get(health))
        .route("/v1/search", post(handle_search))
        .route("/v1/scrape", post(handle_scrape))
        .route("/v1/map", post(handle_map))
        .layer(CorsLayer::permissive())
        .layer(TraceLayer::new_for_http())
        .with_state(state);

    let addr = SocketAddr::from(([0, 0, 0, 0], port));
    tracing::info!("rev-firecrawl listening on {}", addr);

    let listener = tokio::net::TcpListener::bind(addr).await?;
    axum::serve(listener, app).await?;

    Ok(())
}
