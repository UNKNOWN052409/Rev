// scrape.rs — URL → markdown/html conversion + URL mapping

use crate::{ScrapeData, ScrapeMetadata};
use anyhow::Result;
use scraper::{Html, Selector};

/// Scrape a URL and return markdown/html content.
pub async fn scrape_url(
    client: &reqwest::Client,
    url: &str,
    formats: &[String],
) -> Result<ScrapeData> {
    let response = client.get(url).send().await?;
    let status = response.status().as_u16();
    let html_text = response.text().await?;

    let document = Html::parse_document(&html_text);

    // Extract title
    let title_selector = Selector::parse("title").unwrap();
    let title = document
        .select(&title_selector)
        .next()
        .map(|t| t.text().collect::<String>())
        .unwrap_or_default();

    // Determine formats
    let want_markdown = formats.is_empty() || formats.contains(&"markdown".to_string());
    let want_html = formats.contains(&"html".to_string());

    let markdown = if want_markdown {
        Some(html_to_markdown(&html_text))
    } else {
        None
    };

    let html = if want_html {
        Some(html_text.clone())
    } else {
        None
    };

    Ok(ScrapeData {
        markdown,
        html,
        metadata: ScrapeMetadata {
            title,
            url: url.to_string(),
            status,
        },
    })
}

/// Convert HTML to markdown (basic extraction).
fn html_to_markdown(html: &str) -> String {
    // html2md returns String directly
    let md = html2md::parse_html(html);
    if !md.trim().is_empty() {
        md
    } else {
        // Fallback: extract text content
        extract_text(html)
    }
}

/// Fallback text extraction from HTML.
fn extract_text(html: &str) -> String {
    let document = Html::parse_document(html);

    // Remove script and style elements
    let body_selector = Selector::parse("body").unwrap();
    let mut text = String::new();

    if let Some(body) = document.select(&body_selector).next() {
        // Collect text from paragraphs, headings, etc.
        let content_selector =
            Selector::parse("p, h1, h2, h3, h4, h5, h6, li, td, th, span, div").unwrap();
        for element in body.select(&content_selector) {
            let t = element.text().collect::<String>().trim().to_string();
            if !t.is_empty() && t.len() > 2 {
                text.push_str(&t);
                text.push('\n');
            }
        }
    }

    // Deduplicate consecutive lines
    let mut result = String::new();
    let mut prev = String::new();
    for line in text.lines() {
        let trimmed = line.trim();
        if trimmed != prev {
            result.push_str(trimmed);
            result.push('\n');
            prev = trimmed.to_string();
        }
    }
    result
}

/// Discover all URLs on a page (site mapping).
pub async fn map_urls(client: &reqwest::Client, url: &str) -> Result<Vec<String>> {
    let response = client.get(url).send().await?;
    let html_text = response.text().await?;
    let document = Html::parse_document(&html_text);

    let base_url = url::Url::parse(url)?;
    let link_selector = Selector::parse("a[href]").unwrap();

    let mut links = Vec::new();
    let mut seen = std::collections::HashSet::new();

    for element in document.select(&link_selector) {
        if let Some(href) = element.value().attr("href") {
            // Resolve relative URLs
            if let Ok(resolved) = base_url.join(href) {
                let link = resolved.to_string();
                // Only include http(s) links
                if link.starts_with("http") && seen.insert(link.clone()) {
                    links.push(link);
                }
            }
        }
    }

    Ok(links)
}
