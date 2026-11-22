use lopdf::{Document, Object};
use rayon::prelude::*;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::fs::{self, File};
use std::io::Read;
use std::path::{Path, PathBuf};
use std::time::{Instant, UNIX_EPOCH};

#[derive(Deserialize)]
pub struct PrepSpec {
    pub book_dir: String,
    #[serde(default)]
    pub chunk_ids: Vec<String>,
    #[serde(default)]
    pub deltas: Option<HashMap<String, i64>>,
    #[serde(default)]
    pub include_front: bool,
    #[serde(default = "default_true")]
    pub write_agg_aux: bool,
    #[serde(default = "default_true")]
    pub scan_images: bool,
    #[serde(default = "default_true")]
    pub upgrade_tikz_dp: bool,
}

fn default_true() -> bool {
    true
}

#[derive(Serialize)]
pub struct PrepResult {
    pub ok: bool,
    pub label_pages: HashMap<String, String>,
    pub labels: HashMap<String, String>,
    pub agg_aux_changed: bool,
    pub images: HashMap<String, [f64; 3]>,
    pub tikz_upgraded: usize,
    pub total_ms: f64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

#[derive(Serialize, Deserialize, Clone)]
struct CachedImgDim {
    size: u64,
    mtime_sec: u64,
    mtime_nsec: u32,
    w_pt: f64,
    h_pt: f64,
    is_pdf: bool,
}

/// Read balanced `{...}` group starting at `s[i] == '{'`, skipping `\` escapes.
pub(crate) fn read_group(bytes: &[u8], i: usize) -> Option<(&str, usize)> {
    if i >= bytes.len() || bytes[i] != b'{' {
        return None;
    }
    let mut depth = 0usize;
    let mut j = i;
    while j < bytes.len() {
        let b = bytes[j];
        if b == b'\\' {
            j += 2;
            continue;
        }
        if b == b'{' {
            depth += 1;
        } else if b == b'}' {
            depth -= 1;
            if depth == 0 {
                let inner = std::str::from_utf8(&bytes[i + 1..j]).ok()?;
                return Some((inner, j + 1));
            }
        }
        j += 1;
    }
    None
}

/// Mirrors `pdf_stamp.shift_newlabel_line(line, delta)` exactly.
pub fn shift_newlabel_line(line: &str, delta: i64) -> (Option<String>, String, Option<String>) {
    if !line.starts_with("\\newlabel{") {
        return (None, line.to_string(), None);
    }
    let bytes = line.as_bytes();
    let (name, i1) = match read_group(bytes, "\\newlabel".len()) {
        Some(v) => v,
        None => return (None, line.to_string(), None),
    };
    let name_owned = name.to_string();
    let (body, i2) = match read_group(bytes, i1) {
        Some(v) => v,
        None => return (Some(name_owned), line.to_string(), None),
    };
    let bbytes = body.as_bytes();
    let (_num, j1) = match read_group(bbytes, 0) {
        Some(v) => v,
        None => return (Some(name_owned), line.to_string(), None),
    };
    let (pg, j2) = match read_group(bbytes, j1) {
        Some(v) => v,
        None => return (Some(name_owned), line.to_string(), None),
    };
    let pg_s = pg.trim();
    let parsed_pg = match pg_s.parse::<i64>() {
        Ok(v) => v,
        Err(_) => {
            return (
                Some(name_owned),
                line.to_string(),
                Some(pg_s.to_string()),
            );
        }
    };
    let abs_pg = (parsed_pg + delta).to_string();
    if delta == 0 {
        return (Some(name_owned), line.to_string(), Some(abs_pg));
    }
    let mut new_line = String::with_capacity(line.len() + 8);
    new_line.push_str(&line[..i1]);
    new_line.push('{');
    new_line.push_str(&body[..j1]);
    new_line.push('{');
    new_line.push_str(&abs_pg);
    new_line.push('}');
    new_line.push_str(&body[j2..]);
    new_line.push('}');
    new_line.push_str(&line[i2..]);
    (Some(name_owned), new_line, Some(abs_pg))
}

fn agg_aux_internal(
    book_dir: &Path,
    chunk_ids: &[String],
    deltas: Option<&HashMap<String, i64>>,
    include_front: bool,
    write_agg: bool,
) -> (HashMap<String, String>, HashMap<String, String>, bool) {
    let mut cids = Vec::with_capacity(chunk_ids.len() + 1);
    if include_front {
        cids.push("front".to_string());
    }
    cids.extend_from_slice(chunk_ids);

    let out_base = book_dir.join(".build").join("floria").join("out");
    let per_chunk: Vec<Vec<(Option<String>, String, Option<String>)>> = cids
        .par_iter()
        .map(|cid| {
            let p = out_base.join(cid).join("wrapper.aux");
            let raw_bytes = match fs::read(&p) {
                Ok(b) => b,
                Err(_) => return Vec::new(),
            };
            let delta = deltas.and_then(|m| m.get(cid).copied()).unwrap_or(0);
            let text = String::from_utf8_lossy(&raw_bytes);
            let mut entries = Vec::new();
            for ln in text.split('\n') {
                let trimmed = ln.strip_suffix('\r').unwrap_or(ln);
                if !trimmed.starts_with("\\newlabel") {
                    continue;
                }
                entries.push(shift_newlabel_line(trimmed, delta));
            }
            entries
        })
        .collect();

    let mut all_lines = Vec::new();
    let mut label_pages = HashMap::new();
    let mut labels = HashMap::new();

    for entries in per_chunk {
        for (name_opt, shifted, abs_pg_opt) in entries {
            if let Some(ref nm) = name_opt {
                if let Some(pg) = abs_pg_opt {
                    if !pg.is_empty() {
                        label_pages.insert(nm.clone(), pg);
                    }
                }
                labels.insert(nm.clone(), shifted.clone());
            } else if shifted.starts_with("\\newlabel{") {
                if let Some(j) = shifted.find('}') {
                    if j > 10 {
                        labels.insert(shifted[10..j].to_string(), shifted.clone());
                    }
                }
            }
            all_lines.push(shifted);
        }
    }

    let mut changed = false;
    if write_agg && !all_lines.is_empty() {
        let mut content = String::with_capacity(all_lines.len() * 64);
        for ln in &all_lines {
            content.push_str(ln);
            content.push('\n');
        }
        let agg_path = book_dir.join(".build").join("floria").join("agg").join("main.aux");
        let existing_same = fs::read(&agg_path)
            .map(|b| b == content.as_bytes())
            .unwrap_or(false);
        if !existing_same {
            if let Some(parent) = agg_path.parent() {
                let _ = fs::create_dir_all(parent);
            }
            let tmp = agg_path.with_extension("aux.tmp");
            if fs::write(&tmp, content.as_bytes()).is_ok() {
                let _ = fs::rename(&tmp, &agg_path);
                changed = true;
            }
        }
    }

    (label_pages, labels, changed)
}

fn parse_exif_dpi(tiff: &[u8]) -> Option<(f64, f64)> {
    if tiff.len() < 8 {
        return None;
    }
    let le = match &tiff[0..4] {
        b"II*\0" => true,
        b"MM\0*" => false,
        _ => return None,
    };
    let rd_u16 = |off: usize| -> Option<u16> {
        let s = tiff.get(off..off + 2)?;
        Some(if le {
            u16::from_le_bytes([s[0], s[1]])
        } else {
            u16::from_be_bytes([s[0], s[1]])
        })
    };
    let rd_u32 = |off: usize| -> Option<u32> {
        let s = tiff.get(off..off + 4)?;
        Some(if le {
            u32::from_le_bytes([s[0], s[1], s[2], s[3]])
        } else {
            u32::from_be_bytes([s[0], s[1], s[2], s[3]])
        })
    };
    let ifd_off = rd_u32(4)? as usize;
    let count = rd_u16(ifd_off)? as usize;
    let mut xres: Option<f64> = None;
    let mut yres: Option<f64> = None;
    let mut unit: u16 = 2; // 2 = inches, 3 = cm
    for idx in 0..count.min(64) {
        let eoff = ifd_off + 2 + idx * 12;
        if eoff + 12 > tiff.len() {
            break;
        }
        let tag = rd_u16(eoff)?;
        let typ = rd_u16(eoff + 2)?;
        if tag == 0x0128 && typ == 3 {
            if let Some(u) = rd_u16(eoff + 8) {
                unit = u;
            }
        } else if (tag == 0x011A || tag == 0x011B) && typ == 5 {
            let val_off = rd_u32(eoff + 8)? as usize;
            if let (Some(num), Some(den)) = (rd_u32(val_off), rd_u32(val_off + 4)) {
                if num > 0 && den > 0 {
                    let v = (num as f64) / (den as f64);
                    if tag == 0x011A {
                        xres = Some(v);
                    } else {
                        yres = Some(v);
                    }
                }
            }
        }
    }
    let (xr, yr) = (xres?, yres?);
    if xr <= 0.0 || yr <= 0.0 {
        return None;
    }
    match unit {
        2 => Some((xr, yr)),
        3 => Some((xr * 2.54, yr * 2.54)),
        _ => None,
    }
}

fn obj_to_f64(o: &Object) -> Option<f64> {
    match o {
        Object::Integer(i) => Some(*i as f64),
        Object::Real(f) => Some(*f as f64),
        _ => None,
    }
}

fn pdf_first_page_dims_pt(path: &Path) -> Option<(f64, f64)> {
    let doc = Document::load(path).ok()?;
    let pages = doc.get_pages();
    let (_, &first_id) = pages.iter().next()?;
    // Walk page + parent chain for CropBox, fallback MediaBox
    let mut cur_id = Some(first_id);
    let mut crop_box: Option<[f64; 4]> = None;
    let mut media_box: Option<[f64; 4]> = None;
    let mut guard = 0usize;
    while let Some(id) = cur_id {
        if guard > 8 {
            break;
        }
        guard += 1;
        let dict = match doc.get_object(id).and_then(Object::as_dict) {
            Ok(d) => d,
            Err(_) => break,
        };
        if crop_box.is_none() {
            if let Ok(obj) = dict.get(b"CropBox") {
                let resolved = match obj {
                    Object::Reference(r) => doc.get_object(*r).ok(),
                    other => Some(other),
                };
                if let Some(Object::Array(arr)) = resolved {
                    if arr.len() >= 4 {
                        if let (Some(x0), Some(y0), Some(x1), Some(y1)) = (
                            obj_to_f64(&arr[0]),
                            obj_to_f64(&arr[1]),
                            obj_to_f64(&arr[2]),
                            obj_to_f64(&arr[3]),
                        ) {
                            crop_box = Some([x0, y0, x1, y1]);
                        }
                    }
                }
            }
        }
        if media_box.is_none() {
            if let Ok(obj) = dict.get(b"MediaBox") {
                let resolved = match obj {
                    Object::Reference(r) => doc.get_object(*r).ok(),
                    other => Some(other),
                };
                if let Some(Object::Array(arr)) = resolved {
                    if arr.len() >= 4 {
                        if let (Some(x0), Some(y0), Some(x1), Some(y1)) = (
                            obj_to_f64(&arr[0]),
                            obj_to_f64(&arr[1]),
                            obj_to_f64(&arr[2]),
                            obj_to_f64(&arr[3]),
                        ) {
                            media_box = Some([x0, y0, x1, y1]);
                        }
                    }
                }
            }
        }
        if crop_box.is_some() && media_box.is_some() {
            break;
        }
        cur_id = dict.get(b"Parent").ok().and_then(|o| o.as_reference().ok());
    }
    let bx = crop_box.or(media_box)?;
    let w_bp = (bx[2] - bx[0]).abs();
    let h_bp = (bx[3] - bx[1]).abs();
    if w_bp <= 0.0 || h_bp <= 0.0 {
        return None;
    }
    Some((w_bp * 72.27 / 72.0, h_bp * 72.27 / 72.0))
}

fn parse_image_dims_pt(path: &Path) -> Option<(f64, f64, bool)> {
    let ext = path
        .extension()
        .and_then(|e| e.to_str())?
        .to_ascii_lowercase();
    if ext == "pdf" {
        let (w, h) = pdf_first_page_dims_pt(path)?;
        return Some((w, h, true));
    }

    let mut f = File::open(path).ok()?;
    let mut hdr = vec![0u8; 65536];
    let n = f.read(&mut hdr).ok()?;
    hdr.truncate(n);

    if ext == "png" && hdr.len() >= 24 && &hdr[0..8] == b"\x89PNG\r\n\x1a\n" {
        let w = u32::from_be_bytes([hdr[16], hdr[17], hdr[18], hdr[19]]) as f64;
        let h = u32::from_be_bytes([hdr[20], hdr[21], hdr[22], hdr[23]]) as f64;
        if w <= 0.0 || h <= 0.0 {
            return None;
        }
        let mut dpi_x = 72.0;
        let mut dpi_y = 72.0;
        let mut pos = 8usize;
        while pos + 8 <= hdr.len() {
            let ln = u32::from_be_bytes([hdr[pos], hdr[pos + 1], hdr[pos + 2], hdr[pos + 3]]) as usize;
            let typ = &hdr[pos + 4..pos + 8];
            if typ == b"pHYs" && pos + 17 <= hdr.len() {
                let px = u32::from_be_bytes([hdr[pos + 8], hdr[pos + 9], hdr[pos + 10], hdr[pos + 11]]);
                let py = u32::from_be_bytes([hdr[pos + 12], hdr[pos + 13], hdr[pos + 14], hdr[pos + 15]]);
                let unit = hdr[pos + 16];
                if unit == 1 && px > 0 && py > 0 {
                    dpi_x = (px as f64) * 0.0254;
                    dpi_y = (py as f64) * 0.0254;
                }
                break;
            }
            if typ == b"IDAT" {
                break;
            }
            pos = match pos.checked_add(12 + ln) {
                Some(v) => v,
                None => break,
            };
        }
        return Some((w * 72.27 / dpi_x, h * 72.27 / dpi_y, false));
    }

    if (ext == "jpg" || ext == "jpeg") && hdr.len() >= 4 && hdr[0] == 0xFF && hdr[1] == 0xD8 {
        let mut jfif_dpi: Option<(f64, f64)> = None;
        let mut exif_dpi: Option<(f64, f64)> = None;
        let mut sof_wh: Option<(f64, f64)> = None;
        let mut pos = 2usize;
        while pos + 4 <= hdr.len() {
            if hdr[pos] != 0xFF {
                pos += 1;
                continue;
            }
            while pos < hdr.len() && hdr[pos] == 0xFF {
                pos += 1;
            }
            if pos >= hdr.len() {
                break;
            }
            let marker = hdr[pos];
            pos += 1;
            if marker == 0xD8 || marker == 0xD9 || (0xD0..=0xD7).contains(&marker) {
                continue;
            }
            if marker == 0xDA {
                break; // SOS
            }
            if pos + 2 > hdr.len() {
                break;
            }
            let seg_len = u16::from_be_bytes([hdr[pos], hdr[pos + 1]]) as usize;
            if seg_len < 2 {
                break;
            }
            let seg_end = (pos + seg_len).min(hdr.len());
            if marker == 0xE0 && seg_len >= 14 && pos + 14 <= hdr.len() && &hdr[pos + 2..pos + 7] == b"JFIF\0" {
                let units = hdr[pos + 9];
                let xd = u16::from_be_bytes([hdr[pos + 10], hdr[pos + 11]]);
                let yd = u16::from_be_bytes([hdr[pos + 12], hdr[pos + 13]]);
                if units == 1 && xd > 0 && yd > 0 {
                    jfif_dpi = Some((xd as f64, yd as f64));
                } else if units == 2 && xd > 0 && yd > 0 {
                    jfif_dpi = Some(((xd as f64) * 2.54, (yd as f64) * 2.54));
                }
            } else if marker == 0xE1 && seg_len >= 10 && pos + 8 <= hdr.len() && &hdr[pos + 2..pos + 8] == b"Exif\0\0" {
                exif_dpi = parse_exif_dpi(&hdr[pos + 8..seg_end]);
            } else if matches!(
                marker,
                0xC0 | 0xC1 | 0xC2 | 0xC3 | 0xC5 | 0xC6 | 0xC7 | 0xC9 | 0xCA | 0xCB | 0xCD | 0xCE | 0xCF
            ) && pos + 7 <= hdr.len()
            {
                let h = u16::from_be_bytes([hdr[pos + 3], hdr[pos + 4]]) as f64;
                let w = u16::from_be_bytes([hdr[pos + 5], hdr[pos + 6]]) as f64;
                if w > 0.0 && h > 0.0 {
                    sof_wh = Some((w, h));
                }
                break;
            }
            pos += seg_len;
        }
        if let Some((w, h)) = sof_wh {
            let (dpi_x, dpi_y) = jfif_dpi.or(exif_dpi).unwrap_or((72.0, 72.0));
            return Some((w * 72.27 / dpi_x, h * 72.27 / dpi_y, false));
        }
    }

    if ext == "bmp" && hdr.len() >= 46 && &hdr[0..2] == b"BM" {
        let dib_sz = u32::from_le_bytes([hdr[14], hdr[15], hdr[16], hdr[17]]);
        if dib_sz >= 40 {
            let w = i32::from_le_bytes([hdr[18], hdr[19], hdr[20], hdr[21]]).unsigned_abs() as f64;
            let h = i32::from_le_bytes([hdr[22], hdr[23], hdr[24], hdr[25]]).unsigned_abs() as f64;
            let xpels = i32::from_le_bytes([hdr[38], hdr[39], hdr[40], hdr[41]]);
            let ypels = i32::from_le_bytes([hdr[42], hdr[43], hdr[44], hdr[45]]);
            let dpi_x = if xpels > 0 { (xpels as f64) * 0.0254 } else { 72.0 };
            let dpi_y = if ypels > 0 { (ypels as f64) * 0.0254 } else { 72.0 };
            if w > 0.0 && h > 0.0 {
                return Some((w * 72.27 / dpi_x, h * 72.27 / dpi_y, false));
            }
        }
    }

    if ext == "gif" && hdr.len() >= 10 && (&hdr[0..6] == b"GIF87a" || &hdr[0..6] == b"GIF89a") {
        let w = u16::from_le_bytes([hdr[6], hdr[7]]) as f64;
        let h = u16::from_le_bytes([hdr[8], hdr[9]]) as f64;
        if w > 0.0 && h > 0.0 {
            return Some((w * 72.27 / 72.0, h * 72.27 / 72.0, false));
        }
    }

    None
}

fn scan_book_images(book_dir: &Path) -> HashMap<String, [f64; 3]> {
    let img_dir = book_dir.join("image");
    let rd = match fs::read_dir(&img_dir) {
        Ok(r) => r,
        Err(_) => return HashMap::new(),
    };

    let cache_path = book_dir
        .join(".build")
        .join("floria")
        .join("img_dims.json");
    let cached: HashMap<String, CachedImgDim> = fs::read(&cache_path)
        .ok()
        .and_then(|b| serde_json::from_slice(&b).ok())
        .unwrap_or_default();

    let mut files: Vec<(String, PathBuf, u64, u64, u32)> = Vec::new();
    for entry in rd.flatten() {
        let ft = match entry.file_type() {
            Ok(t) => t,
            Err(_) => continue,
        };
        if !ft.is_file() {
            continue;
        }
        let name = match entry.file_name().into_string() {
            Ok(s) => s,
            Err(_) => continue,
        };
        let meta = match entry.metadata() {
            Ok(m) => m,
            Err(_) => continue,
        };
        let (msec, mnsec) = meta
            .modified()
            .ok()
            .and_then(|t| t.duration_since(UNIX_EPOCH).ok())
            .map(|d| (d.as_secs(), d.subsec_nanos()))
            .unwrap_or((0, 0));
        files.push((name, entry.path(), meta.len(), msec, mnsec));
    }

    let mut new_cache: HashMap<String, CachedImgDim> = HashMap::with_capacity(files.len());
    let mut misses: Vec<(String, PathBuf, u64, u64, u32)> = Vec::new();
    for (name, path, sz, msec, mnsec) in files {
        if let Some(c) = cached.get(&name) {
            if c.size == sz && c.mtime_sec == msec && c.mtime_nsec == mnsec {
                new_cache.insert(name, c.clone());
                continue;
            }
        }
        misses.push((name, path, sz, msec, mnsec));
    }

    let dirty = !misses.is_empty() || new_cache.len() != cached.len();
    if !misses.is_empty() {
        let parsed: Vec<(String, CachedImgDim)> = misses
            .into_par_iter()
            .filter_map(|(name, path, size, mtime_sec, mtime_nsec)| {
                let (w_pt, h_pt, is_pdf) = parse_image_dims_pt(&path)?;
                Some((
                    name,
                    CachedImgDim {
                        size,
                        mtime_sec,
                        mtime_nsec,
                        w_pt,
                        h_pt,
                        is_pdf,
                    },
                ))
            })
            .collect();
        for (k, v) in parsed {
            new_cache.insert(k, v);
        }
    }

    if dirty {
        if let Some(parent) = cache_path.parent() {
            let _ = fs::create_dir_all(parent);
        }
        if let Ok(json_bytes) = serde_json::to_vec(&new_cache) {
            let tmp = cache_path.with_extension("json.tmp");
            if fs::write(&tmp, &json_bytes).is_ok() {
                let _ = fs::rename(&tmp, &cache_path);
            }
        }
    }

    let mut out = HashMap::with_capacity(new_cache.len());
    for (k, v) in new_cache {
        out.insert(k, [v.w_pt, v.h_pt, if v.is_pdf { 1.0 } else { 0.0 }]);
    }
    out
}

fn upgrade_tikz_cache_dims(book_dir: &Path) -> usize {
    let tdir = book_dir.join(".build").join("floria").join("tikz");
    let rd = match fs::read_dir(&tdir) {
        Ok(r) => r,
        Err(_) => return 0,
    };

    let mut todo: Vec<(PathBuf, PathBuf, f64)> = Vec::new();
    for entry in rd.flatten() {
        let p = entry.path();
        if p.extension().and_then(|e| e.to_str()) != Some("dp") {
            continue;
        }
        let s = match fs::read_to_string(&p) {
            Ok(v) => v,
            Err(_) => continue,
        };
        let nums: Vec<&str> = s.split_whitespace().collect();
        if nums.len() >= 3 {
            continue;
        }
        if let Some(&first) = nums.first() {
            if let Ok(dp_pt) = first.parse::<f64>() {
                let pdf_p = p.with_extension("pdf");
                if pdf_p.is_file() {
                    todo.push((p, pdf_p, dp_pt));
                }
            }
        }
    }

    if todo.is_empty() {
        return 0;
    }

    todo.into_par_iter()
        .map(|(dp_p, pdf_p, dp_pt)| {
            if let Some((wd_pt, tot_pt)) = pdf_first_page_dims_pt(&pdf_p) {
                let content = format!("{:.5} {:.5} {:.5}\n", dp_pt, wd_pt, tot_pt);
                let tmp = dp_p.with_extension("dp.tmp");
                if fs::write(&tmp, content.as_bytes()).is_ok() {
                    if fs::rename(&tmp, &dp_p).is_ok() {
                        return 1usize;
                    }
                }
            }
            0usize
        })
        .sum()
}

pub fn run(spec: &PrepSpec) -> Result<PrepResult, String> {
    let t0 = Instant::now();
    let book_dir = Path::new(&spec.book_dir);

    let ((label_pages, labels, agg_aux_changed), (images, tikz_upgraded)) = rayon::join(
        || {
            agg_aux_internal(
                book_dir,
                &spec.chunk_ids,
                spec.deltas.as_ref(),
                spec.include_front,
                spec.write_agg_aux,
            )
        },
        || {
            rayon::join(
                || {
                    if spec.scan_images {
                        scan_book_images(book_dir)
                    } else {
                        HashMap::new()
                    }
                },
                || {
                    if spec.upgrade_tikz_dp {
                        upgrade_tikz_cache_dims(book_dir)
                    } else {
                        0
                    }
                },
            )
        },
    );

    Ok(PrepResult {
        ok: true,
        label_pages,
        labels,
        agg_aux_changed,
        images,
        tikz_upgraded,
        total_ms: t0.elapsed().as_secs_f64() * 1000.0,
        error: None,
    })
}
