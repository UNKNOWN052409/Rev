// search.rs — Web search via SERP scraping (DuckDuckGo HTML)

use crate::SearchResult;
use anyhow::Result;
use scraper::{Html, Selector};

/// Perform web search by scraping DuckDuckGo HTML results.
pub async fn web_search(
    client: &reqwest::Client,
    query: &str,
    limit: usize,
) -> Result<Vec<SearchResult>> {
    // DuckDuckGo HTML endpoint (no JS required)
    let search_url = format!(
        "https://html.duckduckgo.com/html/?q={}",
        urlencoding(query)
    );

    let response = client
        .get(&search_url)
        .header("Accept", "text/html")
        .send()
        .await?;

    let html_text = response.text().await?;
    let document = Html::parse_document(&html_text);

    let mut results = Vec::new();

    // DuckDuckGo HTML result structure:
    // <div class="result">
    //   <a class="result__a" href="...">Title</a>
    //   <a class="result__snippet">Description</a>
    // </div>
    let result_selector = Selector::parse("div.result, div.web-result").unwrap();
    let title_selector = Selector::parse("a.result__a, a.result__title").unwrap();
    let snippet_selector = Selector::parse("a.result__snippet, .result__snippet").unwrap();

    for result_div in document.select(&result_selector) {
        if results.len() >= limit {
            break;
        }

        let title = result_div
            .select(&title_selector)
            .next()
            .map(|t| t.text().collect::<String>().trim().to_string())
            .unwrap_or_default();

        let url = result_div
            .select(&title_selector)
            .next()
            .and_then(|t| t.value().attr("href"))
            .map(|h| extract_ddg_url(h))
            .unwrap_or_default();

        let description = result_div
            .select(&snippet_selector)
            .next()
            .map(|s| s.text().collect::<String>().trim().to_string())
            .unwrap_or_default();

        if !title.is_empty() && !url.is_empty() {
            results.push(SearchResult {
                title,
                url,
                description,
                markdown: None,
            });
        }
    }

    // Fallback: if no results found with structured selectors,
    // try a more generic approach
    if results.is_empty() {
        results = generic_serp_parse(&html_text, limit);
    }

    Ok(results)
}

/// Extract actual URL from DuckDuckGo redirect link.
fn extract_ddg_url(href: &str) -> String {
    // DDG wraps URLs: //duckduckgo.com/l/?uddg=<encoded_url>
    if href.contains("uddg=") {
        if let Some(pos) = href.find("uddg=") {
            let encoded = &href[pos + 5..];
            let end = encoded.find('&').unwrap_or(encoded.len());
            if let Ok(decoded) = urlencoding_decode(&encoded[..end]) {
                return decoded;
            }
        }
    }
    // Direct URL
    if href.starts_with("http") {
        return href.to_string();
    }
    // Relative — prepend https
    if href.starts_with("//") {
        return format!("https:{}", href);
    }
    href.to_string()
}

/// Generic SERP parser fallback.
fn generic_serp_parse(html: &str, limit: usize) -> Vec<SearchResult> {
    let document = Html::parse_document(html);
    let mut results = Vec::new();

    // Look for any links with substantial text
    let link_selector = Selector::parse("a[href]").unwrap();
    let mut seen = std::collections::HashSet::new();

    for element in document.select(&link_selector) {
        if results.len() >= limit {
            break;
        }
        let text = element.text().collect::<String>();
        let text = text.trim();
        if text.len() > 15 && text.len() < 200 {
            if let Some(href) = element.value().attr("href") {
                let url = extract_ddg_url(href);
                if url.starts_with("http")
                    && !url.contains("duckduckgo.com")
                    && seen.insert(url.clone())
                {
                    results.push(SearchResult {
                        title: text.to_string(),
                        url,
                        description: String::new(),
                        markdown: None,
                    });
                }
            }
        }
    }

    results
}

/// URL-encode a query string.
fn urlencoding(s: &str) -> String {
    let mut result = String::new();
    for byte in s.bytes() {
        match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                result.push(byte as char)
            }
            b' ' => result.push('+'),
            _ => result.push_str(&format!("%{:02X}", byte)),
        }
    }
    result
}

/// URL-decode a string.
fn urlencoding_decode(s: &str) -> Result<String, ()> {
    let mut result = Vec::new();
    let bytes = s.as_bytes();
    let mut i = 0;
    while i < bytes.len() {
        match bytes[i] {
            b'%' if i + 2 < bytes.len() => {
                let hex = &s[i + 1..i + 3];
                if let Ok(byte) = u8::from_str_radix(hex, 16) {
                    result.push(byte);
                    i += 3;
                    continue;
                }
                result.push(bytes[i]);
                i += 1;
            }
            b'+' => {
                result.push(b' ');
                i += 1;
            }
            b => {
                result.push(b);
                i += 1;
            }
        }
    }
    String::from_utf8(result).map_err(|_| ())
}
