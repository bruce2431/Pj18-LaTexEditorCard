use flate2::read::GzDecoder;
use lopdf::{Document, ObjectId};
use rayon::prelude::*;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::fs::{self, File};
use std::io::{BufWriter, Read, Write};
use std::path::Path;
use std::time::Instant;

const HDR_KEYS: [&str; 4] = ["Magnification", "Unit", "X Offset", "Y Offset"];

#[derive(Deserialize, Clone)]
pub struct PostChunkSpec {
    pub pdf_path: String,
    #[serde(default)]
    pub fmeta_path: Option<String>,
    pub sync_gz_path: String,
    pub slim_out_path: String,
    pub chunk_name: String,
    #[serde(default)]
    pub decoupled: bool,
    #[serde(default)]
    pub max_pages: Option<i64>,
}

#[derive(Serialize)]
pub struct PostChunkResult {
    pub ok: bool,
    pub body_pages: Option<usize>,
    pub total_ms: f64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

#[inline]
fn contains_subslice(haystack: &[u8], needle: &[u8]) -> bool {
    if needle.is_empty() || haystack.len() < needle.len() {
        return false;
    }
    let first = needle[0];
    let max_i = haystack.len() - needle.len();
    let mut i = 0usize;
    while i <= max_i {
        if haystack[i] == first && &haystack[i..i + needle.len()] == needle {
            return true;
        }
        i += 1;
    }
    false
}

pub fn parse_fmeta_body_pages(path: &Path) -> Option<usize> {
    let content = fs::read_to_string(path).ok()?;
    let mut p_count = 0usize;
    let mut body_pages: Option<usize> = None;
    for ln in content.lines() {
        let s = ln.trim();
        if s.is_empty() || s.starts_with("PATCH") {
            continue;
        }
        let mut parts = s.split_whitespace();
        match parts.next() {
            Some("P") => {
                if parts.count() >= 4 {
                    p_count += 1;
                }
            }
            Some("BODY_PAGES") => {
                if let Some(v) = parts.next().and_then(|x| x.parse::<usize>().ok()) {
                    body_pages = Some(v);
                }
            }
            _ => {}
        }
    }
    let bp = body_pages.unwrap_or(p_count);
    if p_count != bp {
        return None;
    }
    Some(bp)
}

pub fn annotate_fmeta_patch_pages(pdf_path: &Path, fmeta_path: &Path, body_pages: usize) {
    let doc = match Document::load(pdf_path) {
        Ok(d) => d,
        Err(_) => return,
    };
    let page_ids: Vec<ObjectId> = doc.get_pages().values().copied().collect();
    let end = body_pages.min(page_ids.len());
    let flags: Vec<(usize, bool, bool)> = (0..end)
        .into_par_iter()
        .filter_map(|idx| {
            let data = doc.get_page_content(page_ids[idx]);
            let ftoc = contains_subslice(&data, b"/FTOC");
            let fpr = contains_subslice(&data, b"/FPR");
            if ftoc || fpr {
                Some((idx, ftoc, fpr))
            } else {
                None
            }
        })
        .collect();

    let mut ftoc_idx = Vec::new();
    let mut fpr_idx = Vec::new();
    for (idx, ftoc, fpr) in flags {
        if ftoc {
            ftoc_idx.push(idx.to_string());
        }
        if fpr {
            fpr_idx.push(idx.to_string());
        }
    }

    let existing = match fs::read_to_string(fmeta_path) {
        Ok(s) => s,
        Err(_) => return,
    };
    let mut out = String::with_capacity(existing.len() + 64);
    for ln in existing.lines() {
        if ln.trim_start().starts_with("PATCH") {
            continue;
        }
        out.push_str(ln);
        out.push('\n');
    }
    out.push_str("PATCH ");
    out.push_str(&ftoc_idx.join(","));
    out.push('|');
    out.push_str(&fpr_idx.join(","));
    out.push('\n');

    let tmp = fmeta_path.with_extension("fmeta.tmp");
    if fs::write(&tmp, out.as_bytes()).is_ok() {
        let _ = fs::rename(&tmp, fmeta_path);
    }
}

#[inline]
fn parse_ascii_i64(bytes: &[u8]) -> Option<i64> {
    if bytes.is_empty() {
        return None;
    }
    let mut sign = 1i64;
    let mut idx = 0usize;
    if bytes[0] == b'-' {
        sign = -1;
        idx = 1;
        if idx >= bytes.len() {
            return None;
        }
    }
    let mut v = 0i64;
    while idx < bytes.len() {
        let b = bytes[idx];
        if !b.is_ascii_digit() {
            return None;
        }
        v = v.checked_mul(10)?.checked_add((b - b'0') as i64)?;
        idx += 1;
    }
    Some(v * sign)
}

pub fn build_slim_synctex(
    sync_gz_path: &Path,
    slim_out_path: &Path,
    chunk_name: &str,
    max_pages: Option<i64>,
) -> Result<(), String> {
    let f = File::open(sync_gz_path)
        .map_err(|e| format!("open synctex {} failed: {}", sync_gz_path.display(), e))?;
    let mut gz = GzDecoder::new(f);
    let mut raw = Vec::with_capacity(512 * 1024);
    gz.read_to_end(&mut raw)
        .map_err(|e| format!("decompress synctex {} failed: {}", sync_gz_path.display(), e))?;

    let mut keep_tag: HashMap<i64, u8> = HashMap::new();
    let mut hdr: HashMap<&'static str, Vec<u8>> = HashMap::new();
    let mut pages: i64 = 0;

    // Pass 1: collect Input tags, headers, and !page markers; locate Content..Postamble slice
    let mut content_start: Option<usize> = None;
    let mut content_end: usize = raw.len();
    let mut in_content = false;
    let mut pos = 0usize;
    let n = raw.len();

    while pos < n {
        let nl = raw[pos..]
            .iter()
            .position(|&b| b == b'\n')
            .map(|p| pos + p)
            .unwrap_or(n);
        let mut ln = &raw[pos..nl];
        if ln.ends_with(b"\r") {
            ln = &ln[..ln.len() - 1];
        }
        let next_pos = if nl < n { nl + 1 } else { n };

        if !in_content {
            if ln.starts_with(b"Input:") {
                let rest = &ln[6..];
                if let Some(colon) = rest.iter().position(|&b| b == b':') {
                    if let Some(tag) = parse_ascii_i64(&rest[..colon]) {
                        let p_str = String::from_utf8_lossy(&rest[colon + 1..]);
                        let bn = p_str
                            .rsplit(|c| c == '/' || c == '\\')
                            .next()
                            .unwrap_or("");
                        if !chunk_name.is_empty() && bn == chunk_name {
                            keep_tag.insert(tag, 1);
                        } else if bn.to_ascii_lowercase().ends_with(".toc") {
                            keep_tag.insert(tag, 2);
                        }
                    }
                }
            } else if ln == b"Content:" {
                in_content = true;
                content_start = Some(next_pos);
            } else if ln.starts_with(b"!") {
                if let Some(p) = parse_ascii_i64(&ln[1..]) {
                    if p > pages {
                        pages = p;
                    }
                }
            } else {
                for &k in &HDR_KEYS {
                    let kb = k.as_bytes();
                    if ln.len() > kb.len() && ln.starts_with(kb) && ln[kb.len()] == b':' {
                        hdr.insert(k, ln[kb.len() + 1..].to_vec());
                        break;
                    }
                }
            }
        } else if ln.starts_with(b"Postamble:") {
            in_content = false;
            content_end = pos;
        } else if ln.starts_with(b"!") {
            if let Some(p) = parse_ascii_i64(&ln[1..]) {
                if p > pages {
                    pages = p;
                }
            }
        }

        pos = next_pos;
    }

    let tmp_path = slim_out_path.with_extension("slim.tmp");
    let out_file = File::create(&tmp_path)
        .map_err(|e| format!("create slim tmp {} failed: {}", tmp_path.display(), e))?;
    let mut w = BufWriter::with_capacity(256 * 1024, out_file);

    for &k in &HDR_KEYS {
        if let Some(val) = hdr.get(k) {
            w.write_all(b"H ").map_err(|e| e.to_string())?;
            w.write_all(k.as_bytes()).map_err(|e| e.to_string())?;
            w.write_all(b":").map_err(|e| e.to_string())?;
            w.write_all(val).map_err(|e| e.to_string())?;
            w.write_all(b"\n").map_err(|e| e.to_string())?;
        }
    }

    let npages = match max_pages {
        Some(mp) if mp > 0 => pages.min(mp),
        _ => pages,
    };
    writeln!(w, "PAGES {}", npages).map_err(|e| e.to_string())?;

    if let Some(c_start) = content_start {
        let slice = &raw[c_start..content_end.min(raw.len())];
        let mut cpos = 0usize;
        let cn = slice.len();
        let mut skip_page = false;

        while cpos < cn {
            let nl = slice[cpos..]
                .iter()
                .position(|&b| b == b'\n')
                .map(|p| cpos + p)
                .unwrap_or(cn);
            let mut ln = &slice[cpos..nl];
            if ln.ends_with(b"\r") {
                ln = &ln[..ln.len() - 1];
            }
            cpos = if nl < cn { nl + 1 } else { cn };

            if ln.is_empty() || ln[0] == b'!' {
                continue;
            }
            let c = ln[0];
            if c == b'{' {
                let cur_page = parse_ascii_i64(&ln[1..]).unwrap_or(0);
                skip_page = max_pages.map(|mp| cur_page > mp).unwrap_or(false);
                if !skip_page {
                    w.write_all(ln).map_err(|e| e.to_string())?;
                    w.write_all(b"\n").map_err(|e| e.to_string())?;
                }
                continue;
            }
            if c == b'}' {
                if !skip_page {
                    w.write_all(ln).map_err(|e| e.to_string())?;
                    w.write_all(b"\n").map_err(|e| e.to_string())?;
                }
                skip_page = false;
                continue;
            }
            if skip_page || !matches!(c, b'(' | b'[' | b'h' | b'v' | b'x' | b'r') {
                continue;
            }
            // Match ^([\[\]()hvgk$x])(\d+),(\d+):(.*)$
            let rest = &ln[1..];
            let comma = match rest.iter().position(|&b| b == b',') {
                Some(i) if i > 0 => i,
                _ => continue,
            };
            let tag_bytes = &rest[..comma];
            let after_comma = &rest[comma + 1..];
            let colon = match after_comma.iter().position(|&b| b == b':') {
                Some(i) if i > 0 => i,
                _ => continue,
            };
            let line_bytes = &after_comma[..colon];
            if !line_bytes.iter().all(|b| b.is_ascii_digit()) {
                continue;
            }
            let tag = match parse_ascii_i64(tag_bytes) {
                Some(t) => t,
                None => continue,
            };
            let nt = match keep_tag.get(&tag) {
                Some(&v) => v,
                None => continue,
            };
            let tail = &after_comma[colon + 1..];
            w.write_all(&[c, b'0' + nt, b',']).map_err(|e| e.to_string())?;
            w.write_all(line_bytes).map_err(|e| e.to_string())?;
            w.write_all(b":").map_err(|e| e.to_string())?;
            w.write_all(tail).map_err(|e| e.to_string())?;
            w.write_all(b"\n").map_err(|e| e.to_string())?;
        }
    }

    w.write_all(b"FLORIA_SLIM_END\n").map_err(|e| e.to_string())?;
    w.flush().map_err(|e| e.to_string())?;
    drop(w);
    fs::rename(&tmp_path, slim_out_path)
        .map_err(|e| format!("rename slim {} failed: {}", slim_out_path.display(), e))?;
    Ok(())
}

pub fn run_one(spec: &PostChunkSpec) -> Result<PostChunkResult, String> {
    let t0 = Instant::now();
    let pdf_p = Path::new(&spec.pdf_path);
    let sync_p = Path::new(&spec.sync_gz_path);
    let slim_p = Path::new(&spec.slim_out_path);

    let mut body_pages: Option<usize> = None;
    let mut eff_max_pages: Option<i64> = spec.max_pages;

    if spec.decoupled {
        if let Some(ref fm_str) = spec.fmeta_path {
            let fm_p = Path::new(fm_str);
            if let Some(bp) = parse_fmeta_body_pages(fm_p) {
                body_pages = Some(bp);
                eff_max_pages = Some(bp as i64);
            }
        }
    }

    let (_, slim_res) = rayon::join(
        || {
            if spec.decoupled {
                if let (Some(fm_str), Some(bp)) = (&spec.fmeta_path, body_pages) {
                    annotate_fmeta_patch_pages(pdf_p, Path::new(fm_str), bp);
                }
            }
        },
        || build_slim_synctex(sync_p, slim_p, &spec.chunk_name, eff_max_pages),
    );
    let _ = slim_res; // Non-fatal if synctex slim fails, matching fastpath.py behavior

    Ok(PostChunkResult {
        ok: true,
        body_pages,
        total_ms: t0.elapsed().as_secs_f64() * 1000.0,
        error: None,
    })
}
