//! Static assets embedded into the binary at compile time via rust-embed.
//! This makes the exe self-contained — no separate `static/` folder required.

use rust_embed::Embed;
use axum::{
    http::{StatusCode, header, Response},
    response::IntoResponse,
    body::Body,
};

#[derive(Embed)]
#[folder = "static/"]
pub struct Asset;

/// Axum handler: serve an embedded file by path.
pub async fn serve_embedded(path: &str) -> impl IntoResponse {
    let path = path.trim_start_matches('/');
    let path = if path.is_empty() { "index.html" } else { path };

    match Asset::get(path) {
        Some(file) => {
            let mime = mime_for(path);
            Response::builder()
                .status(StatusCode::OK)
                .header(header::CONTENT_TYPE, mime)
                .body(Body::from(file.data.into_owned()))
                .unwrap()
        }
        None => {
            // Fallback to index.html for SPA-style navigation
            match Asset::get("index.html") {
                Some(file) => Response::builder()
                    .status(StatusCode::OK)
                    .header(header::CONTENT_TYPE, "text/html; charset=utf-8")
                    .body(Body::from(file.data.into_owned()))
                    .unwrap(),
                None => Response::builder()
                    .status(StatusCode::NOT_FOUND)
                    .body(Body::from("Not found"))
                    .unwrap(),
            }
        }
    }
}

fn mime_for(path: &str) -> &'static str {
    match path.rsplit('.').next().unwrap_or("") {
        "html" => "text/html; charset=utf-8",
        "js"   => "application/javascript",
        "css"  => "text/css",
        "png"  => "image/png",
        "ico"  => "image/x-icon",
        "svg"  => "image/svg+xml",
        "json" => "application/json",
        _      => "application/octet-stream",
    }
}
