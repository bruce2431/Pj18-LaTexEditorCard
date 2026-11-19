use flate2::write::GzEncoder;
use flate2::Compression;
use rayon::prelude::*;
use serde::Deserialize;
use std::collections::HashMap;
use std::fs::{self, File};
use std::io::{BufWriter, Write};

const HDR_KEYS: [&str; 4] = ["Magnification", "Unit", "X Offset", "Y Offset"];

#[derive(Deserialize)]
pub struct SynctexSpec {
    pub out: String,
    pub items: Vec<SynctexItem>,
}

#[derive(Deserialize)]
pub struct SynctexItem {
    pub slim: String,
    #[serde(default)]
    pub offset: i64,
    #[serde(default = "default_one")]
    pub chunk_line: i64,
}

fn default_one() -> i64 {
    1
}

struct ParsedSlim {
    hdr: Vec<(String, String)>,
    npages: i64,
    has_toc: bool,
    line_count: usize,
    body: Vec<u8>,
}

#[inline]
fn is_line_kind(b: u8) -> bool {
    matches!(
        b,
        b'-' | b'[' | b']' | b'(' | b')' | b'<' | b'>' | b'x' | b'k' | b'g' | b'v' | b'h' | b'r'
            | b'$'
    )
}

#[inline]
fn parse_u64_ascii(bytes: &[u8]) -> Option<i64> {
    if bytes.is_empty() {
        return None;
    }
    let mut v: i64 = 0;
    for &b in bytes {
        if !b.is_ascii_digit() {
            return None;
        }
        v = v.checked_mul(10)?.checked_add((b - b'0') as i64)?;
    }
    Some(v)
}

fn process_item(it: &SynctexItem) -> Result<ParsedSlim, String> {
    let raw = fs::read(&it.slim)
        .map_err(|e| format!("read slim {} failed: {}", it.slim, e))?;
    let mut hdr = Vec::with_capacity(4);
    let mut npages: i64 = 0;
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
        if ln.starts_with(b"H ") {
            if let Some(colon) = ln[2..].iter().position(|&b| b == b':') {
                let k = String::from_utf8_lossy(&ln[2..2 + colon]).into_owned();
                let v = String::from_utf8_lossy(&ln[3 + colon..]).into_owned();
                hdr.push((k, v));
            }
            pos = if nl < n { nl + 1 } else { n };
        } else if ln.starts_with(b"PAGES ") {
            if let Ok(s) = std::str::from_utf8(&ln[6..]) {
                if let Ok(p) = s.trim().parse::<i64>() {
                    npages = p;
                }
            }
            pos = if nl < n { nl + 1 } else { n };
        } else {
            break;
        }
    }

    // Strip trailing FLORIA_SLIM_END marker
    let mut end = n;
    let marker = b"FLORIA_SLIM_END";
    if let Some(mpos) = raw[pos..].windows(marker.len()).rposition(|w| w == marker) {
        end = pos + mpos;
    }

    let mut seg = &raw[pos..end];
    while seg.starts_with(b"\n") || seg.starts_with(b"\r") {
        seg = &seg[1..];
    }
    while seg.ends_with(b"\n") || seg.ends_with(b"\r") {
        seg = &seg[..seg.len() - 1];
    }

    let offset = it.offset;
    let shift = it.chunk_line - 1;
    let mut out = Vec::with_capacity(seg.len() + seg.len() / 16 + 64);
    let mut has_toc = false;
    let mut line_count = 0usize;
    let mut num_buf = [0u8; 32];

    if !seg.is_empty() {
        let mut i = 0usize;
        let slen = seg.len();
        while i < slen {
            let nl = seg[i..]
                .iter()
                .position(|&b| b == b'\n')
                .map(|p| i + p)
                .unwrap_or(slen);
            let mut ln = &seg[i..nl];
            if ln.ends_with(b"\r") {
                ln = &ln[..ln.len() - 1];
            }
            line_count += 1;

            if !has_toc && ln.len() >= 3 && ln[1] == b'2' && ln[2] == b',' {
                has_toc = true;
            }

            if offset != 0
                && ln.len() >= 2
                && (ln[0] == b'{' || ln[0] == b'}')
                && parse_u64_ascii(&ln[1..]).is_some()
            {
                let pg = parse_u64_ascii(&ln[1..]).unwrap() + offset;
                out.push(ln[0]);
                write_i64(&mut out, pg, &mut num_buf);
            } else if shift != 0
                && ln.len() >= 5
                && is_line_kind(ln[0])
                && ln[1] == b'1'
                && ln[2] == b','
            {
                if let Some(colon_rel) = ln[3..].iter().position(|&b| b == b':') {
                    if let Some(old_ln) = parse_u64_ascii(&ln[3..3 + colon_rel]) {
                        out.push(ln[0]);
                        out.extend_from_slice(b"1,");
                        write_i64(&mut out, old_ln + shift, &mut num_buf);
                        out.extend_from_slice(&ln[3 + colon_rel..]);
                    } else {
                        out.extend_from_slice(ln);
                    }
                } else {
                    out.extend_from_slice(ln);
                }
            } else {
                out.extend_from_slice(ln);
            }
            out.push(b'\n');
            i = if nl < slen { nl + 1 } else { slen };
        }
    }

    Ok(ParsedSlim {
        hdr,
        npages,
        has_toc,
        line_count,
        body: out,
    })
}

#[inline]
fn write_i64(dst: &mut Vec<u8>, mut v: i64, buf: &mut [u8; 32]) {
    if v == 0 {
        dst.push(b'0');
        return;
    }
    if v < 0 {
        dst.push(b'-');
        v = -v;
    }
    let mut k = 32usize;
    while v > 0 {
        k -= 1;
        buf[k] = b'0' + (v % 10) as u8;
        v /= 10;
    }
    dst.extend_from_slice(&buf[k..]);
}

pub fn run(spec: &SynctexSpec) -> Result<(), String> {
    let parsed: Vec<ParsedSlim> = spec
        .items
        .par_iter()
        .map(process_item)
        .collect::<Result<Vec<_>, _>>()?;

    let mut hdr: HashMap<String, String> = HashMap::new();
    let mut has_toc = false;
    let mut line_count: usize = 0;
    let mut total_pages: i64 = 0;

    for (item, p) in spec.items.iter().zip(parsed.iter()) {
        for (k, v) in &p.hdr {
            if let Some(prev) = hdr.get(k) {
                if prev != v {
                    return Err(format!(
                        "synctex 头部参数不一致（{}: {} != {}）",
                        k, prev, v
                    ));
                }
            } else {
                hdr.insert(k.clone(), v.clone());
            }
        }
        if p.has_toc {
            has_toc = true;
        }
        line_count += p.line_count;
        let end_pg = item.offset + p.npages;
        if end_pg > total_pages {
            total_pages = end_pg;
        }
    }

    let tmp = format!("{}.tmp", spec.out);
    {
        let file = File::create(&tmp).map_err(|e| format!("create {} failed: {}", tmp, e))?;
        let bw = BufWriter::with_capacity(512 * 1024, file);
        let mut gz = GzEncoder::new(bw, Compression::fast());

        gz.write_all(b"SyncTeX Version:1\nInput:1:main.tex\n")
            .map_err(|e| e.to_string())?;
        if has_toc {
            gz.write_all(b"Input:2:wrapper.toc\n")
                .map_err(|e| e.to_string())?;
        }
        gz.write_all(b"Output:pdf\n").map_err(|e| e.to_string())?;
        for k in HDR_KEYS {
            let def = if k == "Magnification" { "1000" } else { "1" };
            let val = hdr.get(k).map(|s| s.as_str()).unwrap_or(def);
            writeln!(gz, "{}:{}", k, val).map_err(|e| e.to_string())?;
        }
        gz.write_all(b"Content:\n").map_err(|e| e.to_string())?;

        for p in &parsed {
            if !p.body.is_empty() {
                gz.write_all(&p.body).map_err(|e| e.to_string())?;
            }
        }

        writeln!(
            gz,
            "Postamble:\nCount:{}\n!{}\nPost scriptum:",
            line_count, total_pages
        )
        .map_err(|e| e.to_string())?;

        let mut bw = gz.finish().map_err(|e| e.to_string())?;
        bw.flush().map_err(|e| e.to_string())?;
    }

    fs::rename(&tmp, &spec.out)
        .map_err(|e| format!("rename {} -> {} failed: {}", tmp, spec.out, e))?;
    Ok(())
}
