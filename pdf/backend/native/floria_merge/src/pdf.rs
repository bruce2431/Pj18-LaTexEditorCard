use flate2::write::ZlibEncoder;
use flate2::Compression;
use lopdf::{Dictionary, Document, Object, ObjectId, Stream, StringFormat};
use rayon::prelude::*;
use regex::bytes::Regex;
use serde::Deserialize;
use std::collections::{HashMap, HashSet};
use std::fs::{self, File};
use std::io::{BufWriter, Write};
use std::sync::OnceLock;

#[derive(Deserialize)]
pub struct PdfSpec {
    pub out: String,
    pub kit_pdf: String,
    pub kit_info: KitInfo,
    pub parts: Vec<PartSpec>,
    #[serde(default)]
    pub outline: Vec<OutlineEntry>,
    #[serde(default)]
    pub toc_pages: Vec<String>,
    #[serde(default)]
    pub label_pages: HashMap<String, String>,
}

#[derive(Deserialize)]
pub struct KitInfo {
    pub twoside: bool,
    pub plain_cx: f64,
    pub plain_y: f64,
    pub even_pg_x: f64,
    pub even_mk_rx: f64,
    pub odd_mk_lx: f64,
    pub odd_pg_rx: f64,
    pub head_y: f64,
    pub fonts: HashMap<String, KitFontMeta>,
}

#[derive(Deserialize)]
pub struct KitFontMeta {
    pub page_idx: usize,
    pub font_key: String,
    pub cids: Vec<String>,
    pub base_size: f64,
    pub digit_w: f64,
}

#[derive(Deserialize)]
#[serde(tag = "kind")]
pub enum PartSpec {
    #[serde(rename = "blanks")]
    Blanks {
        count: usize,
        width: f64,
        height: f64,
    },
    #[serde(rename = "pages")]
    Pages {
        path: String,
        start_idx: usize,
        end_idx: usize,
        abs_start: usize,
        #[serde(default)]
        fmeta: Option<FmetaSpec>,
    },
}

#[derive(Deserialize)]
pub struct FmetaSpec {
    #[serde(default)]
    pub pages: Vec<(String, i64, i64)>,
    #[serde(default)]
    pub has_atlas: bool,
    #[serde(default)]
    pub ftoc_pages: Option<Vec<usize>>,
    #[serde(default)]
    pub fpr_pages: Option<Vec<usize>>,
}

#[derive(Deserialize)]
pub struct OutlineEntry {
    pub level: String,
    pub title: String,
    pub page: usize,
}

struct KitFontRuntime {
    name: Vec<u8>,      // e.g. b"FKD0"
    name_slash: String, // e.g. "/FKD0"
    id: ObjectId,
    cids: Vec<String>,
    base_size: f64,
    digit_w: f64,
}

struct AtlasForm {
    id: ObjectId,
    width: f64,
    name: Vec<u8>,      // b"FAM1"
    name_slash: String, // "/FAM1"
}

struct RegexBank {
    cm: Regex,
    tf: Regex,
    ftoc: Regex,
    fpr: Regex,
    fatlas: Regex,
    prop_i: Regex,
    prop_id: Regex,
    prop_l: Regex,
}

fn regexes() -> &'static RegexBank {
    static BANK: OnceLock<RegexBank> = OnceLock::new();
    BANK.get_or_init(|| RegexBank {
        cm: Regex::new(r"(?-u)1\s+0\s+0\s+1\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s+cm")
            .unwrap(),
        tf: Regex::new(r"(?-u)(/F\d+)\s+(-?\d+(?:\.\d+)?)\s+Tf").unwrap(),
        ftoc: Regex::new(r"(?s-u)/FTOC\s*<<\s*(.*?)\s*>>\s*BDC(.*?)EMC").unwrap(),
        fpr: Regex::new(r"(?s-u)/FPR\s*<<\s*(.*?)\s*>>\s*BDC(.*?)EMC").unwrap(),
        fatlas: Regex::new(r"(?s-u)/FATLAS\s*<<\s*(.*?)\s*>>\s*BDC(.*?)EMC").unwrap(),
        prop_i: Regex::new(r"(?-u)/I\s+(\d+)").unwrap(),
        prop_id: Regex::new(r"(?-u)/Id\s+(\d+)").unwrap(),
        prop_l: Regex::new(r"(?s-u)/L\s*\(([^)]+)\)").unwrap(),
    })
}

#[inline]
fn contains_subslice(hay: &[u8], needle: &[u8]) -> bool {
    if needle.is_empty() || hay.len() < needle.len() {
        return false;
    }
    hay.windows(needle.len()).any(|w| w == needle)
}

#[inline]
fn parse_f64_ascii(b: &[u8]) -> Option<f64> {
    std::str::from_utf8(b).ok()?.parse::<f64>().ok()
}

fn encode_digits(s: &str, fspec: &KitFontRuntime) -> String {
    let mut out = String::with_capacity(s.len() * 4 + 2);
    out.push('<');
    for ch in s.bytes() {
        if (b'0'..=b'9').contains(&ch) {
            let idx = (ch - b'0') as usize;
            if let Some(cid) = fspec.cids.get(idx) {
                out.push_str(cid);
            }
        }
    }
    out.push('>');
    out
}

fn fmt_g4(v: f64) -> String {
    // Matches Python %.4g closely enough for PDF Tf font sizes (e.g. 9.963, 10.95, 7.97)
    let s = format!("{:.4}", v);
    let s = s.trim_end_matches('0').trim_end_matches('.');
    if s.is_empty() || s == "-0" {
        "0".to_string()
    } else {
        s.to_string()
    }
}

/// Moves reachable objects from `src` into `dst` without cloning big stream payloads.
struct Importer<'a> {
    src: &'a mut Document,
    dst: &'a mut Document,
    memo: HashMap<ObjectId, ObjectId>,
}

impl<'a> Importer<'a> {
    fn new(src: &'a mut Document, dst: &'a mut Document) -> Self {
        Self {
            src,
            dst,
            memo: HashMap::new(),
        }
    }

    fn import_id(&mut self, old_id: ObjectId) -> ObjectId {
        if let Some(&mapped) = self.memo.get(&old_id) {
            return mapped;
        }
        let new_id = self.dst.new_object_id();
        self.memo.insert(old_id, new_id);
        if let Some(obj) = self.src.objects.remove(&old_id) {
            let rewritten = self.rewrite_obj(obj, false);
            self.dst.objects.insert(new_id, rewritten);
        } else {
            self.dst.objects.insert(new_id, Object::Null);
        }
        new_id
    }

    fn rewrite_obj(&mut self, obj: Object, is_page_dict: bool) -> Object {
        match obj {
            Object::Reference(id) => Object::Reference(self.import_id(id)),
            Object::Array(arr) => {
                let mut out = Vec::with_capacity(arr.len());
                for item in arr {
                    out.push(self.rewrite_obj(item, false));
                }
                Object::Array(out)
            }
            Object::Dictionary(dict) => {
                let mut out = Dictionary::new();
                for (k, v) in dict {
                    if is_page_dict && k.as_slice() == b"Parent" {
                        continue;
                    }
                    // Never follow /Parent back into source /Pages tree from sub-objects
                    if k.as_slice() == b"Parent" {
                        continue;
                    }
                    let rv = self.rewrite_obj(v, false);
                    out.set(k, rv);
                }
                Object::Dictionary(out)
            }
            Object::Stream(mut stream) => {
                let mut out = Dictionary::new();
                for (k, v) in stream.dict {
                    let rv = self.rewrite_obj(v, false);
                    out.set(k, rv);
                }
                stream.dict = out;
                Object::Stream(stream)
            }
            other => other,
        }
    }
}

/// Extract inherited attribute (e.g. /MediaBox, /Resources, /CropBox, /Rotate) from a page or its /Pages ancestors.
fn get_inherited_attr(doc: &Document, page_id: ObjectId, key: &[u8]) -> Option<Object> {
    let mut cur = Some(page_id);
    let mut guard = 0;
    while let Some(id) = cur {
        if guard > 16 {
            break;
        }
        guard += 1;
        if let Ok(Object::Dictionary(d)) = doc.get_object(id) {
            if let Ok(v) = d.get(key) {
                return Some(v.clone());
            }
            cur = d.get(b"Parent").ok().and_then(|o| o.as_reference().ok());
        } else {
            break;
        }
    }
    None
}

/// Collect named destinations from a source Document: returns Vec<(dest_name_bytes, src_page_idx, tail_elems)>.
fn extract_named_dests(
    doc: &Document,
    page_id_to_idx: &HashMap<ObjectId, usize>,
) -> Vec<(Vec<u8>, usize, Vec<Object>)> {
    let mut raw_pairs: Vec<(Vec<u8>, Object)> = Vec::new();

    let catalog = match doc
        .trailer
        .get(b"Root")
        .ok()
        .and_then(|o| o.as_reference().ok())
        .and_then(|id| doc.get_object(id).ok())
        .and_then(|o| o.as_dict().ok())
    {
        Some(c) => c,
        None => return Vec::new(),
    };

    // 1. Legacy /Catalog -> /Dests dict
    if let Ok(dests_obj) = catalog.get(b"Dests") {
        if let Ok(dict) = resolve_dict(doc, dests_obj) {
            for (k, v) in dict.iter() {
                raw_pairs.push((k.clone(), v.clone()));
            }
        }
    }

    // 2. Modern /Catalog -> /Names -> /Dests name tree
    if let Ok(names_obj) = catalog.get(b"Names") {
        if let Ok(names_dict) = resolve_dict(doc, names_obj) {
            if let Ok(dests_tree) = names_dict.get(b"Dests") {
                walk_name_tree(doc, dests_tree, &mut raw_pairs, 0);
            }
        }
    }

    let mut out = Vec::with_capacity(raw_pairs.len());
    for (name, val) in raw_pairs {
        let resolved = resolve_obj(doc, &val);
        let arr_opt = match resolved {
            Some(Object::Array(arr)) => Some(arr),
            Some(Object::Dictionary(d)) => d
                .get(b"D")
                .ok()
                .and_then(|o| resolve_obj(doc, o))
                .and_then(|o| match o {
                    Object::Array(a) => Some(a),
                    _ => None,
                }),
            _ => None,
        };
        if let Some(arr) = arr_opt {
            if let Some(Object::Reference(pid)) = arr.first() {
                if let Some(&pidx) = page_id_to_idx.get(pid) {
                    // Preserve primitive tail elements (/XYZ x y z, /Fit, etc.)
                    let mut tail = Vec::new();
                    let mut simple = arr.len() >= 2;
                    for el in arr.iter().skip(1) {
                        match el {
                            Object::Name(_)
                            | Object::Integer(_)
                            | Object::Real(_)
                            | Object::Null => tail.push(el.clone()),
                            _ => {
                                simple = false;
                                break;
                            }
                        }
                    }
                    if !simple {
                        tail.clear();
                        tail.push(Object::Name(b"Fit".to_vec()));
                    }
                    out.push((name, pidx, tail));
                }
            }
        }
    }
    out
}

fn resolve_obj<'a>(doc: &'a Document, obj: &'a Object) -> Option<&'a Object> {
    let mut cur = obj;
    for _ in 0..8 {
        match cur {
            Object::Reference(id) => cur = doc.get_object(*id).ok()?,
            other => return Some(other),
        }
    }
    None
}

fn resolve_dict<'a>(doc: &'a Document, obj: &'a Object) -> Result<&'a Dictionary, ()> {
    match resolve_obj(doc, obj) {
        Some(Object::Dictionary(d)) => Ok(d),
        _ => Err(()),
    }
}

fn walk_name_tree(
    doc: &Document,
    node: &Object,
    out: &mut Vec<(Vec<u8>, Object)>,
    depth: usize,
) {
    if depth > 16 {
        return;
    }
    let dict = match resolve_dict(doc, node) {
        Ok(d) => d,
        Err(_) => return,
    };
    if let Ok(names_obj) = dict.get(b"Names") {
        if let Some(Object::Array(arr)) = resolve_obj(doc, names_obj) {
            let mut i = 0;
            while i + 1 < arr.len() {
                if let Object::String(k, _) = &arr[i] {
                    out.push((k.clone(), arr[i + 1].clone()));
                }
                i += 2;
            }
        }
    }
    if let Ok(kids_obj) = dict.get(b"Kids") {
        if let Some(Object::Array(kids)) = resolve_obj(doc, kids_obj) {
            for kid in kids {
                walk_name_tree(doc, kid, out, depth + 1);
            }
        }
    }
}

struct AtlasBox {
    id: i64,
    x0: f64,
    y0: f64,
    width: f64,
    inner: Vec<u8>,
}

fn extract_fatlas_boxes(stream_bytes: &[u8]) -> Vec<AtlasBox> {
    let rx = regexes();
    let mut results = Vec::new();
    for cap in rx.fatlas.captures_iter(stream_bytes) {
        let props = cap.get(1).map(|m| m.as_bytes()).unwrap_or(b"");
        let body = cap.get(2).map(|m| m.as_bytes()).unwrap_or(b"");
        let id = match rx.prop_id.captures(props).and_then(|c| c.get(1)) {
            Some(m) => match std::str::from_utf8(m.as_bytes())
                .ok()
                .and_then(|s| s.parse::<i64>().ok())
            {
                Some(v) => v,
                None => continue,
            },
            None => continue,
        };
        let cms: Vec<_> = rx.cm.captures_iter(body).collect();
        let (x0, y0, width, inner) = if cms.len() >= 2 {
            let x0 = -parse_f64_ascii(cms[0].get(1).unwrap().as_bytes()).unwrap_or(0.0);
            let y0 = -parse_f64_ascii(cms[0].get(2).unwrap().as_bytes()).unwrap_or(0.0);
            let last = &cms[cms.len() - 1];
            let x1 = parse_f64_ascii(last.get(1).unwrap().as_bytes()).unwrap_or(0.0);
            let st = cms[0].get(0).unwrap().end();
            let ed = last.get(0).unwrap().start();
            let slice = if ed >= st { &body[st..ed] } else { b"" };
            (x0, y0, x1 - x0, trim_ascii_ws(slice).to_vec())
        } else {
            (0.0, 0.0, 0.0, trim_ascii_ws(body).to_vec())
        };
        results.push(AtlasBox {
            id,
            x0,
            y0,
            width,
            inner,
        });
    }
    results
}

#[inline]
fn trim_ascii_ws(mut b: &[u8]) -> &[u8] {
    while let Some(&first) = b.first() {
        if first.is_ascii_whitespace() {
            b = &b[1..];
        } else {
            break;
        }
    }
    while let Some(&last) = b.last() {
        if last.is_ascii_whitespace() {
            b = &b[..b.len() - 1];
        } else {
            break;
        }
    }
    b
}

/// Ensure page has its own inline `/Resources` dict (shallow-cloned if indirect), returning mutable ref to it.
fn isolate_page_resources(doc: &mut Document, page_id: ObjectId) -> Dictionary {
    let res_obj = doc
        .get_object(page_id)
        .ok()
        .and_then(|o| o.as_dict().ok())
        .and_then(|d| d.get(b"Resources").ok())
        .cloned();
    match res_obj {
        Some(Object::Dictionary(d)) => d,
        Some(Object::Reference(rid)) => doc
            .get_object(rid)
            .ok()
            .and_then(|o| o.as_dict().ok())
            .cloned()
            .unwrap_or_default(),
        _ => Dictionary::new(),
    }
}

fn isolate_subdict(doc: &Document, parent: &Dictionary, key: &[u8]) -> Dictionary {
    match parent.get(key).ok() {
        Some(Object::Dictionary(d)) => d.clone(),
        Some(Object::Reference(rid)) => doc
            .get_object(*rid)
            .ok()
            .and_then(|o| o.as_dict().ok())
            .cloned()
            .unwrap_or_default(),
        _ => Dictionary::new(),
    }
}

fn font_is_bold(doc: &Document, fonts_dict: &Dictionary, font_key_slash: &[u8]) -> bool {
    let key = font_key_slash.strip_prefix(b"/").unwrap_or(font_key_slash);
    let fobj = match fonts_dict.get(key).ok() {
        Some(o) => o,
        None => return false,
    };
    let fdict = match resolve_dict(doc, fobj) {
        Ok(d) => d,
        Err(_) => return false,
    };
    if let Ok(Object::Name(bf)) = fdict.get(b"BaseFont") {
        return contains_subslice(bf, b"BX") || contains_subslice(bf, b"Bold");
    }
    false
}

fn build_stamp_ops(
    ps: &str,
    abs_pg: usize,
    lid: i64,
    rid: i64,
    kit: &KitInfo,
    dig_spec: &KitFontRuntime,
    atlas_forms: &HashMap<i64, AtlasForm>,
    xobj_dict: &mut Option<Dictionary>,
    doc: &Document,
    res: &Dictionary,
) -> Vec<String> {
    let pg_str = abs_pg.to_string();
    let pg_w = (pg_str.len() as f64) * dig_spec.digit_w;
    let pg_hx = encode_digits(&pg_str, dig_spec);
    let dig_sz = fmt_g4(dig_spec.base_size);
    let mut ops = Vec::with_capacity(2);

    if ps == "plain" {
        let px = kit.plain_cx - pg_w / 2.0;
        ops.push(format!(
            "q 0 G 0 g BT /FKD0 {} Tf {:.3} {:.3} Td[{}]TJ ET Q",
            dig_sz, px, kit.plain_y, pg_hx
        ));
    } else if ps == "headings" {
        if kit.twoside && (abs_pg % 2 == 0) {
            ops.push(format!(
                "q 0 G 0 g BT /FKD0 {} Tf {:.3} {:.3} Td[{}]TJ ET Q",
                dig_sz, kit.even_pg_x, kit.head_y, pg_hx
            ));
            if let Some(af) = atlas_forms.get(&lid) {
                let xd = xobj_dict.get_or_insert_with(|| isolate_subdict(doc, res, b"XObject"));
                xd.set(af.name.clone(), Object::Reference(af.id));
                let mx = kit.even_mk_rx - af.width;
                ops.push(format!(
                    "q 0 G 0 g 1 0 0 1 {:.3} {:.3} cm {} Do Q",
                    mx, kit.head_y, af.name_slash
                ));
            }
        } else {
            if let Some(af) = atlas_forms.get(&rid) {
                let xd = xobj_dict.get_or_insert_with(|| isolate_subdict(doc, res, b"XObject"));
                xd.set(af.name.clone(), Object::Reference(af.id));
                ops.push(format!(
                    "q 0 G 0 g 1 0 0 1 {:.3} {:.3} cm {} Do Q",
                    kit.odd_mk_lx, kit.head_y, af.name_slash
                ));
            }
            let px = kit.odd_pg_rx - pg_w;
            ops.push(format!(
                "q 0 G 0 g BT /FKD0 {} Tf {:.3} {:.3} Td[{}]TJ ET Q",
                dig_sz, px, kit.head_y, pg_hx
            ));
        }
    }
    ops
}

fn patch_ftoc_bytes(
    raw: &[u8],
    toc_pages: &[String],
    doc: &Document,
    fonts_dict: &Dictionary,
    digs: &KitFontRuntime,
    bdigs: &KitFontRuntime,
) -> Vec<u8> {
    let rx = regexes();
    rx.ftoc
        .replace_all(raw, |caps: &regex::bytes::Captures| -> Vec<u8> {
            let full = caps.get(0).unwrap().as_bytes();
            let props = caps.get(1).map(|m| m.as_bytes()).unwrap_or(b"");
            let body = caps.get(2).map(|m| m.as_bytes()).unwrap_or(b"");

            let idx = match rx
                .prop_i
                .captures(props)
                .and_then(|c| c.get(1))
                .and_then(|m| std::str::from_utf8(m.as_bytes()).ok())
                .and_then(|s| s.parse::<usize>().ok())
            {
                Some(i) => i,
                None => return full.to_vec(),
            };
            let new_s = match toc_pages.get(idx) {
                Some(s) if !s.is_empty() && s.bytes().all(|b| b.is_ascii_digit()) => s,
                _ => return full.to_vec(),
            };
            let cms: Vec<_> = rx.cm.captures_iter(body).collect();
            let tf = match rx.tf.captures(body) {
                Some(t) => t,
                None => return full.to_vec(),
            };
            if cms.len() < 2 {
                return full.to_vec();
            }
            let x0 = -parse_f64_ascii(cms[0].get(1).unwrap().as_bytes()).unwrap_or(0.0);
            let y0 = -parse_f64_ascii(cms[0].get(2).unwrap().as_bytes()).unwrap_or(0.0);
            let last = &cms[cms.len() - 1];
            let x1 = parse_f64_ascii(last.get(1).unwrap().as_bytes()).unwrap_or(0.0);
            let y1 = parse_f64_ascii(last.get(2).unwrap().as_bytes()).unwrap_or(0.0);
            let fk = tf.get(1).unwrap().as_bytes();
            let sz = parse_f64_ascii(tf.get(2).unwrap().as_bytes()).unwrap_or(digs.base_size);

            let fspec = if font_is_bold(doc, fonts_dict, fk) {
                bdigs
            } else {
                digs
            };
            let dw = fspec.digit_w * (sz / fspec.base_size);
            let nx0 = x1 - (new_s.len() as f64) * dw;
            let hx = encode_digits(new_s, fspec);

            let mut out = Vec::with_capacity(full.len() + 32);
            out.extend_from_slice(b"/FTOC << ");
            out.extend_from_slice(props);
            let tail = format!(
                " >> BDC 1 0 0 1 {:.3} {:.3} cm BT {} {} Tf {:.3} {:.3} Td[{}]TJ ET 1 0 0 1 {:.3} {:.3} cm EMC",
                -x0, -y0, fspec.name_slash, fmt_g4(sz), nx0, y0, hx, x1, y1
            );
            out.extend_from_slice(tail.as_bytes());
            out
        })
        .into_owned()
}

fn patch_fpr_bytes(
    raw: &[u8],
    lbl_map: &HashMap<String, String>,
    doc: &Document,
    fonts_dict: &Dictionary,
    digs: &KitFontRuntime,
    bdigs: &KitFontRuntime,
    sdigs: &KitFontRuntime,
) -> Vec<u8> {
    let rx = regexes();
    rx.fpr
        .replace_all(raw, |caps: &regex::bytes::Captures| -> Vec<u8> {
            let full = caps.get(0).unwrap().as_bytes();
            let props = caps.get(1).map(|m| m.as_bytes()).unwrap_or(b"");
            let body = caps.get(2).map(|m| m.as_bytes()).unwrap_or(b"");

            let lbl_bytes = match rx.prop_l.captures(props).and_then(|c| c.get(1)) {
                Some(m) => m.as_bytes(),
                None => return full.to_vec(),
            };
            let lbl_str = match std::str::from_utf8(lbl_bytes) {
                Ok(s) => s,
                Err(_) => return full.to_vec(),
            };
            let new_s = match lbl_map.get(lbl_str) {
                Some(s) if !s.is_empty() && s.bytes().all(|b| b.is_ascii_digit()) => s,
                _ => return full.to_vec(),
            };
            let cms: Vec<_> = rx.cm.captures_iter(body).collect();
            let tf = match rx.tf.captures(body) {
                Some(t) => t,
                None => return full.to_vec(),
            };
            if cms.len() < 2 {
                return full.to_vec();
            }
            let x0 = -parse_f64_ascii(cms[0].get(1).unwrap().as_bytes()).unwrap_or(0.0);
            let y0 = -parse_f64_ascii(cms[0].get(2).unwrap().as_bytes()).unwrap_or(0.0);
            let last = &cms[cms.len() - 1];
            let x1 = parse_f64_ascii(last.get(1).unwrap().as_bytes()).unwrap_or(0.0);
            let y1 = parse_f64_ascii(last.get(2).unwrap().as_bytes()).unwrap_or(0.0);
            let fk = tf.get(1).unwrap().as_bytes();
            let sz = parse_f64_ascii(tf.get(2).unwrap().as_bytes()).unwrap_or(digs.base_size);

            let fspec = if sz < digs.base_size * 0.85 {
                sdigs
            } else if font_is_bold(doc, fonts_dict, fk) {
                bdigs
            } else {
                digs
            };
            let dw = fspec.digit_w * (sz / fspec.base_size);
            let w_box = (x1 - x0).max(0.5);
            let w_new = (new_s.len() as f64) * dw;
            let (tz_cmd, nx0) = if w_new <= w_box + 0.08 {
                (String::new(), x0 + (w_box - w_new) / 2.0)
            } else {
                let tz = (100.0 * w_box / w_new).max(50.0);
                (format!("{:.2} Tz ", tz), x0)
            };
            let hx = encode_digits(new_s, fspec);

            let mut out = Vec::with_capacity(full.len() + 32);
            out.extend_from_slice(b"/FPR << ");
            out.extend_from_slice(props);
            let tail = format!(
                " >> BDC 1 0 0 1 {:.3} {:.3} cm BT {} {} Tf {}{:.3} {:.3} Td[{}]TJ ET 1 0 0 1 {:.3} {:.3} cm EMC",
                -x0, -y0, fspec.name_slash, fmt_g4(sz), tz_cmd, nx0, y0, hx, x1, y1
            );
            out.extend_from_slice(tail.as_bytes());
            out
        })
        .into_owned()
}

fn append_stamp_stream(doc: &mut Document, page_id: ObjectId, stamp_ops: &[String]) {
    let mut bytes = Vec::with_capacity(128);
    for op in stamp_ops {
        bytes.extend_from_slice(op.as_bytes());
        bytes.push(b'\n');
    }
    let st = Stream::new(Dictionary::new(), bytes);
    let sref = doc.add_object(Object::Stream(st));

    let orig_contents = doc
        .get_object(page_id)
        .ok()
        .and_then(|o| o.as_dict().ok())
        .and_then(|d| d.get(b"Contents").ok())
        .cloned();

    let new_contents = match orig_contents {
        None => Object::Reference(sref),
        Some(Object::Array(mut arr)) => {
            arr.push(Object::Reference(sref));
            Object::Array(arr)
        }
        Some(Object::Reference(cid)) => {
            // Could be a reference to a Stream OR a reference to an Array of stream references
            let resolved_arr = doc
                .get_object(cid)
                .ok()
                .and_then(|o| match o {
                    Object::Array(a) => Some(a.clone()),
                    _ => None,
                });
            if let Some(mut arr) = resolved_arr {
                arr.push(Object::Reference(sref));
                Object::Array(arr)
            } else {
                Object::Array(vec![Object::Reference(cid), Object::Reference(sref)])
            }
        }
        Some(other) => Object::Array(vec![other, Object::Reference(sref)]),
    };

    if let Ok(Object::Dictionary(d)) = doc.get_object_mut(page_id) {
        d.set(b"Contents", new_contents);
    }
}

fn deflate_fast(data: &[u8]) -> Result<Vec<u8>, String> {
    let mut enc = ZlibEncoder::new(Vec::with_capacity(data.len() / 2 + 64), Compression::fast());
    enc.write_all(data).map_err(|e| e.to_string())?;
    enc.finish().map_err(|e| e.to_string())
}

fn encode_utf16be_bom(s: &str) -> Vec<u8> {
    let mut out = Vec::with_capacity(2 + s.len() * 2);
    out.push(0xFE);
    out.push(0xFF);
    for u in s.encode_utf16() {
        out.extend_from_slice(&u.to_be_bytes());
    }
    out
}

fn build_outlines(
    doc: &mut Document,
    entries: &[OutlineEntry],
    page_ids: &[ObjectId],
) -> Option<ObjectId> {
    struct Node {
        id: ObjectId,
        title: String,
        page_id: ObjectId,
        parent: Option<usize>,
        children: Vec<usize>,
        descendants: usize,
    }

    let mut nodes: Vec<Node> = Vec::new();
    let mut roots: Vec<usize> = Vec::new();
    let mut stack: [Option<usize>; 4] = [None; 4];

    for e in entries {
        let rk = match e.level.as_str() {
            "part" => 0,
            "chapter" => 1,
            "section" => 2,
            "subsection" => 3,
            _ => continue,
        };
        if e.page == 0 || e.page > page_ids.len() {
            continue;
        }
        let pid = page_ids[e.page - 1];
        for slot in stack.iter_mut().skip(rk) {
            *slot = None;
        }
        let parent = if rk > 0 {
            // Find nearest non-None ancestor
            (0..rk).rev().find_map(|k| stack[k])
        } else {
            None
        };
        let idx = nodes.len();
        let id = doc.new_object_id();
        nodes.push(Node {
            id,
            title: e.title.clone(),
            page_id: pid,
            parent,
            children: Vec::new(),
            descendants: 0,
        });
        if let Some(pidx) = parent {
            nodes[pidx].children.push(idx);
        } else {
            roots.push(idx);
        }
        stack[rk] = Some(idx);
    }

    if roots.is_empty() {
        return None;
    }

    // Compute descendant counts bottom-up (children always have higher indices than parents)
    for i in (0..nodes.len()).rev() {
        let count: usize = nodes[i]
            .children
            .iter()
            .map(|&c| 1 + nodes[c].descendants)
            .sum();
        nodes[i].descendants = count;
    }

    let outlines_root_id = doc.new_object_id();

    // Helper to build prev/next links for a sibling list
    let mut prev_of = vec![None; nodes.len()];
    let mut next_of = vec![None; nodes.len()];
    let link_siblings = |sibs: &[usize], prev_of: &mut [Option<ObjectId>], next_of: &mut [Option<ObjectId>]| {
        for w in sibs.windows(2) {
            next_of[w[0]] = Some(nodes[w[1]].id);
            prev_of[w[1]] = Some(nodes[w[0]].id);
        }
    };
    link_siblings(&roots, &mut prev_of, &mut next_of);
    for n in &nodes {
        if n.children.len() > 1 {
            link_siblings(&n.children, &mut prev_of, &mut next_of);
        }
    }

    for (i, n) in nodes.iter().enumerate() {
        let mut d = Dictionary::new();
        d.set(
            b"Title",
            Object::String(encode_utf16be_bom(&n.title), StringFormat::Hexadecimal),
        );
        let parent_id = n.parent.map(|p| nodes[p].id).unwrap_or(outlines_root_id);
        d.set(b"Parent", Object::Reference(parent_id));
        if let Some(pr) = prev_of[i] {
            d.set(b"Prev", Object::Reference(pr));
        }
        if let Some(nx) = next_of[i] {
            d.set(b"Next", Object::Reference(nx));
        }
        if let (Some(&first), Some(&last)) = (n.children.first(), n.children.last()) {
            d.set(b"First", Object::Reference(nodes[first].id));
            d.set(b"Last", Object::Reference(nodes[last].id));
            d.set(b"Count", Object::Integer(n.descendants as i64));
        }
        d.set(
            b"Dest",
            Object::Array(vec![
                Object::Reference(n.page_id),
                Object::Name(b"Fit".to_vec()),
            ]),
        );
        doc.objects.insert(n.id, Object::Dictionary(d));
    }

    let mut root_dict = Dictionary::new();
    root_dict.set(b"Type", Object::Name(b"Outlines".to_vec()));
    root_dict.set(b"First", Object::Reference(nodes[roots[0]].id));
    root_dict.set(
        b"Last",
        Object::Reference(nodes[*roots.last().unwrap()].id),
    );
    root_dict.set(b"Count", Object::Integer(nodes.len() as i64));
    doc.objects
        .insert(outlines_root_id, Object::Dictionary(root_dict));
    Some(outlines_root_id)
}

fn build_dests_names_dict(
    doc: &mut Document,
    dests: &HashMap<Vec<u8>, (ObjectId, Vec<Object>)>,
) -> Option<ObjectId> {
    if dests.is_empty() {
        return None;
    }
    let mut items: Vec<(&Vec<u8>, &(ObjectId, Vec<Object>))> = dests.iter().collect();
    items.sort_by(|a, b| a.0.cmp(b.0));

    let mut arr = Vec::with_capacity(items.len() * 2);
    for (name, (pid, tail)) in items {
        arr.push(Object::String(name.clone(), StringFormat::Literal));
        let mut dest_arr = Vec::with_capacity(1 + tail.len());
        dest_arr.push(Object::Reference(*pid));
        dest_arr.extend(tail.iter().cloned());
        arr.push(Object::Array(dest_arr));
    }

    let mut dests_tree = Dictionary::new();
    dests_tree.set(b"Names", Object::Array(arr));
    let dests_id = doc.add_object(Object::Dictionary(dests_tree));

    let mut names_dict = Dictionary::new();
    names_dict.set(b"Dests", Object::Reference(dests_id));
    Some(doc.add_object(Object::Dictionary(names_dict)))
}

struct LoadedPart {
    doc: Document,
    pages: Vec<ObjectId>,
    named_dests: Vec<(Vec<u8>, usize, Vec<Object>)>,
}

pub fn run(spec: &PdfSpec) -> Result<usize, String> {
    // 1. Load kit.pdf and all chunk PDFs in parallel via rayon
    let page_paths: Vec<(usize, &str)> = spec
        .parts
        .iter()
        .enumerate()
        .filter_map(|(idx, p)| match p {
            PartSpec::Pages { path, .. } => Some((idx, path.as_str())),
            PartSpec::Blanks { .. } => None,
        })
        .collect();

    let (kit_res, chunks_res): (Result<Document, String>, Result<Vec<(usize, LoadedPart)>, String>) =
        rayon::join(
            || {
                Document::load(&spec.kit_pdf)
                    .map_err(|e| format!("load kit {}: {}", spec.kit_pdf, e))
            },
            || {
                page_paths
                    .par_iter()
                    .map(|&(idx, path)| {
                        let doc = Document::load(path)
                            .map_err(|e| format!("load chunk {}: {}", path, e))?;
                        let pages: Vec<ObjectId> = doc.get_pages().into_values().collect();
                        let pid_map: HashMap<ObjectId, usize> =
                            pages.iter().enumerate().map(|(i, &id)| (id, i)).collect();
                        let named_dests = extract_named_dests(&doc, &pid_map);
                        Ok((
                            idx,
                            LoadedPart {
                                doc,
                                pages,
                                named_dests,
                            },
                        ))
                    })
                    .collect()
            },
        );

    let mut kit_doc = kit_res?;
    let mut loaded_map: HashMap<usize, LoadedPart> = chunks_res?.into_iter().collect();

    // 2. Initialize output PDF document
    let mut out_doc = Document::with_version("1.5");
    let pages_root_id = out_doc.new_object_id();
    let catalog_id = out_doc.new_object_id();

    // 3. Import kit digit fonts (DIGS, BDIGS, SDIGS)
    let kit_pages: Vec<ObjectId> = kit_doc.get_pages().into_values().collect();
    let mut kit_importer = Importer::new(&mut kit_doc, &mut out_doc);
    let mut kit_fonts: HashMap<&str, KitFontRuntime> = HashMap::new();

    for (dk, fname, fname_slash) in [
        ("DIGS", b"FKD0".as_slice(), "/FKD0"),
        ("BDIGS", b"FKDB".as_slice(), "/FKDB"),
        ("SDIGS", b"FKDS".as_slice(), "/FKDS"),
    ] {
        let fi = spec
            .kit_info
            .fonts
            .get(dk)
            .ok_or_else(|| format!("missing kit font {}", dk))?;
        let kpage_id = *kit_pages
            .get(fi.page_idx)
            .ok_or_else(|| format!("kit page_idx {} out of range", fi.page_idx))?;
        let font_key = fi.font_key.trim_start_matches('/').as_bytes();
        let font_ref_or_obj = {
            let res_obj = get_inherited_attr(kit_importer.src, kpage_id, b"Resources")
                .ok_or_else(|| format!("kit page {} missing Resources", fi.page_idx))?;
            let res_dict = resolve_dict(kit_importer.src, &res_obj)
                .map_err(|_| "kit Resources not a dict".to_string())?;
            let font_dict_obj = res_dict
                .get(b"Font")
                .map_err(|_| "kit Resources missing Font".to_string())?;
            let font_dict = resolve_dict(kit_importer.src, font_dict_obj)
                .map_err(|_| "kit Font not a dict".to_string())?;
            font_dict
                .get(font_key)
                .map_err(|_| format!("kit font key {} not found", fi.font_key))?
                .clone()
        };
        let imported_font_id = match font_ref_or_obj {
            Object::Reference(fid) => kit_importer.import_id(fid),
            other => {
                let rw = kit_importer.rewrite_obj(other, false);
                kit_importer.dst.add_object(rw)
            }
        };
        kit_fonts.insert(
            dk,
            KitFontRuntime {
                name: fname.to_vec(),
                name_slash: fname_slash.to_string(),
                id: imported_font_id,
                cids: fi.cids.clone(),
                base_size: fi.base_size,
                digit_w: fi.digit_w,
            },
        );
    }
    drop(kit_importer);

    let digs = kit_fonts.get("DIGS").unwrap();
    let bdigs = kit_fonts.get("BDIGS").unwrap();
    let sdigs = kit_fonts.get("SDIGS").unwrap();

    // 4. Process parts in order
    let mut out_page_ids: Vec<ObjectId> = Vec::with_capacity(200);
    let mut dests: HashMap<Vec<u8>, (ObjectId, Vec<Object>)> = HashMap::new();
    let mut annot_action_ids: Vec<ObjectId> = Vec::new();
    let mut pages_with_inline_annots: Vec<ObjectId> = Vec::new();

    for (part_idx, part) in spec.parts.iter().enumerate() {
        match part {
            PartSpec::Blanks {
                count,
                width,
                height,
            } => {
                for _ in 0..*count {
                    let mut pdict = Dictionary::new();
                    pdict.set(b"Type", Object::Name(b"Page".to_vec()));
                    pdict.set(b"Parent", Object::Reference(pages_root_id));
                    pdict.set(
                        b"MediaBox",
                        Object::Array(vec![
                            Object::Real(0.0),
                            Object::Real(0.0),
                            Object::Real(*width as f32),
                            Object::Real(*height as f32),
                        ]),
                    );
                    pdict.set(b"Resources", Object::Dictionary(Dictionary::new()));
                    let pid = out_doc.add_object(Object::Dictionary(pdict));
                    out_page_ids.push(pid);
                }
            }
            PartSpec::Pages {
                start_idx,
                end_idx,
                abs_start,
                fmeta,
                ..
            } => {
                let mut loaded = loaded_map
                    .remove(&part_idx)
                    .ok_or_else(|| format!("missing loaded part {}", part_idx))?;
                let n_total = loaded.pages.len();
                let a = *start_idx;
                let body_end = (*end_idx).min(n_total);

                // Pre-extract Atlas streams & inherited MediaBox/Resources before moving objects out
                let mut atlas_raw: Vec<(Vec<AtlasBox>, Option<Object>)> = Vec::new();
                if fmeta.as_ref().map(|f| f.has_atlas).unwrap_or(false) && body_end < n_total {
                    for &apid in &loaded.pages[body_end..n_total] {
                        let content = loaded.doc.get_page_content(apid);
                        if contains_subslice(&content, b"/FATLAS") {
                            let boxes = extract_fatlas_boxes(&content);
                            if !boxes.is_empty() {
                                let ares = get_inherited_attr(&loaded.doc, apid, b"Resources");
                                atlas_raw.push((boxes, ares));
                            }
                        }
                    }
                }

                // Pre-capture inherited attributes for kept pages in case MediaBox/Resources live on /Pages
                let mut inherited_boxes: Vec<(Option<Object>, Option<Object>)> =
                    Vec::with_capacity(body_end.saturating_sub(a));
                for &spid in &loaded.pages[a..body_end] {
                    let has_mb = loaded
                        .doc
                        .get_object(spid)
                        .ok()
                        .and_then(|o| o.as_dict().ok())
                        .map(|d| d.has(b"MediaBox"))
                        .unwrap_or(false);
                    let mb = if has_mb {
                        None
                    } else {
                        get_inherited_attr(&loaded.doc, spid, b"MediaBox")
                    };
                    let has_res = loaded
                        .doc
                        .get_object(spid)
                        .ok()
                        .and_then(|o| o.as_dict().ok())
                        .map(|d| d.has(b"Resources"))
                        .unwrap_or(false);
                    let res = if has_res {
                        None
                    } else {
                        get_inherited_attr(&loaded.doc, spid, b"Resources")
                    };
                    inherited_boxes.push((mb, res));
                }

                // Import Atlas Form XObjects + kept body pages via move-based Importer
                let mut importer = Importer::new(&mut loaded.doc, &mut out_doc);
                let mut atlas_forms: HashMap<i64, AtlasForm> = HashMap::new();

                for (boxes, ares_opt) in atlas_raw {
                    let ares_imported = match ares_opt {
                        Some(obj) => {
                            let rw = importer.rewrite_obj(obj, false);
                            match rw {
                                Object::Reference(id) => Object::Reference(id),
                                other => Object::Reference(importer.dst.add_object(other)),
                            }
                        }
                        None => Object::Dictionary(Dictionary::new()),
                    };
                    for ab in boxes {
                        let mut fx_bytes = Vec::with_capacity(ab.inner.len() + 64);
                        let prefix = format!("1 0 0 1 {:.4} {:.4} cm\n", -ab.x0, -ab.y0);
                        fx_bytes.extend_from_slice(prefix.as_bytes());
                        fx_bytes.extend_from_slice(&ab.inner);
                        fx_bytes.push(b'\n');

                        let mut fdict = Dictionary::new();
                        fdict.set(b"Type", Object::Name(b"XObject".to_vec()));
                        fdict.set(b"Subtype", Object::Name(b"Form".to_vec()));
                        fdict.set(
                            b"BBox",
                            Object::Array(vec![
                                Object::Real(-5.0),
                                Object::Real(-10.0),
                                Object::Real((ab.width.max(1.0) + 5.0) as f32),
                                Object::Real(28.0),
                            ]),
                        );
                        fdict.set(b"Resources", ares_imported.clone());
                        let fid = importer
                            .dst
                            .add_object(Object::Stream(Stream::new(fdict, fx_bytes)));
                        let name_str = format!("FAM{}", ab.id);
                        atlas_forms.insert(
                            ab.id,
                            AtlasForm {
                                id: fid,
                                width: ab.width,
                                name: name_str.as_bytes().to_vec(),
                                name_slash: format!("/{}", name_str),
                            },
                        );
                    }
                }

                // Import kept pages
                let mut part_out_pages: Vec<ObjectId> =
                    Vec::with_capacity(body_end.saturating_sub(a));
                for (local_i, (&spid, (inh_mb, inh_res))) in loaded.pages[a..body_end]
                    .iter()
                    .zip(inherited_boxes.into_iter())
                    .enumerate()
                {
                    let new_pid = importer.dst.new_object_id();
                    importer.memo.insert(spid, new_pid);
                    let mut pdict = match importer.src.objects.remove(&spid) {
                        Some(Object::Dictionary(d)) => match importer.rewrite_obj(Object::Dictionary(d), true) {
                            Object::Dictionary(rd) => rd,
                            _ => Dictionary::new(),
                        },
                        _ => Dictionary::new(),
                    };
                    pdict.set(b"Type", Object::Name(b"Page".to_vec()));
                    pdict.set(b"Parent", Object::Reference(pages_root_id));
                    if let Some(mb) = inh_mb {
                        let rw = importer.rewrite_obj(mb, false);
                        pdict.set(b"MediaBox", rw);
                    }
                    if let Some(res) = inh_res {
                        let rw = importer.rewrite_obj(res, false);
                        pdict.set(b"Resources", rw);
                    }
                    importer.dst.objects.insert(new_pid, Object::Dictionary(pdict));
                    part_out_pages.push(new_pid);
                    let _ = local_i;
                }
                drop(importer);

                // Register named destinations for kept pages
                for (name, pi, tail) in loaded.named_dests {
                    if pi >= a && pi < body_end {
                        let new_pid = part_out_pages[pi - a];
                        dests.entry(name).or_insert((new_pid, tail));
                    }
                }

                // Prepare patch index sets
                let fmeta_pages = fmeta.as_ref().map(|f| f.pages.as_slice()).unwrap_or(&[]);
                let ftoc_set: Option<HashSet<usize>> = fmeta
                    .as_ref()
                    .and_then(|f| f.ftoc_pages.as_ref())
                    .map(|v| v.iter().copied().collect());
                let fpr_set: Option<HashSet<usize>> = fmeta
                    .as_ref()
                    .and_then(|f| f.fpr_pages.as_ref())
                    .map(|v| v.iter().copied().collect());
                let has_patch_index = ftoc_set.is_some() && fpr_set.is_some();

                // Stamp & hot-patch each kept page
                for (local_idx, &pg_id) in part_out_pages.iter().enumerate() {
                    let i = a + local_idx;
                    let abs_pg = abs_start + local_idx;
                    out_page_ids.push(pg_id);

                    // Collect GoToR annotation actions on this page
                    collect_gotor_annots(
                        &out_doc,
                        pg_id,
                        &mut annot_action_ids,
                        &mut pages_with_inline_annots,
                    );

                    let (ps, lid, rid) = fmeta_pages
                        .get(i)
                        .map(|(p, l, r)| (p.as_str(), *l, *r))
                        .unwrap_or(("empty", -1, -1));
                    let need_stamp = ps == "plain" || ps == "headings";

                    let need_decode = if has_patch_index {
                        let may_ftoc = !spec.toc_pages.is_empty()
                            && ftoc_set.as_ref().unwrap().contains(&i);
                        let may_fpr = !spec.label_pages.is_empty()
                            && fpr_set.as_ref().unwrap().contains(&i);
                        may_ftoc || may_fpr
                    } else {
                        !spec.toc_pages.is_empty() || !spec.label_pages.is_empty()
                    };

                    if !need_decode {
                        // Zero-decompress fast path (90%+ pages)
                        if !need_stamp {
                            continue;
                        }
                        let mut res = isolate_page_resources(&mut out_doc, pg_id);
                        let mut fonts = isolate_subdict(&out_doc, &res, b"Font");
                        fonts.set(digs.name.clone(), Object::Reference(digs.id));
                        let mut xobj_opt: Option<Dictionary> = None;
                        let stamp_ops = build_stamp_ops(
                            ps,
                            abs_pg,
                            lid,
                            rid,
                            &spec.kit_info,
                            digs,
                            &atlas_forms,
                            &mut xobj_opt,
                            &out_doc,
                            &res,
                        );
                        if !stamp_ops.is_empty() {
                            res.set(b"Font", Object::Dictionary(fonts));
                            if let Some(xd) = xobj_opt {
                                res.set(b"XObject", Object::Dictionary(xd));
                            }
                            if let Ok(Object::Dictionary(pd)) = out_doc.get_object_mut(pg_id) {
                                pd.set(b"Resources", Object::Dictionary(res));
                            }
                            append_stamp_stream(&mut out_doc, pg_id, &stamp_ops);
                        }
                        continue;
                    }

                    // Hot-patch path (/FTOC and/or /FPR pages)
                    let raw = out_doc.get_page_content(pg_id);
                    let has_ftoc =
                        !spec.toc_pages.is_empty() && contains_subslice(&raw, b"/FTOC");
                    let has_fpr =
                        !spec.label_pages.is_empty() && contains_subslice(&raw, b"/FPR");

                    if !has_ftoc && !has_fpr {
                        if !need_stamp {
                            continue;
                        }
                        let mut res = isolate_page_resources(&mut out_doc, pg_id);
                        let mut fonts = isolate_subdict(&out_doc, &res, b"Font");
                        fonts.set(digs.name.clone(), Object::Reference(digs.id));
                        let mut xobj_opt: Option<Dictionary> = None;
                        let stamp_ops = build_stamp_ops(
                            ps,
                            abs_pg,
                            lid,
                            rid,
                            &spec.kit_info,
                            digs,
                            &atlas_forms,
                            &mut xobj_opt,
                            &out_doc,
                            &res,
                        );
                        if !stamp_ops.is_empty() {
                            res.set(b"Font", Object::Dictionary(fonts));
                            if let Some(xd) = xobj_opt {
                                res.set(b"XObject", Object::Dictionary(xd));
                            }
                            if let Ok(Object::Dictionary(pd)) = out_doc.get_object_mut(pg_id) {
                                pd.set(b"Resources", Object::Dictionary(res));
                            }
                            append_stamp_stream(&mut out_doc, pg_id, &stamp_ops);
                        }
                        continue;
                    }

                    let mut res = isolate_page_resources(&mut out_doc, pg_id);
                    let mut fonts = isolate_subdict(&out_doc, &res, b"Font");
                    let mut patched = raw;

                    if has_ftoc {
                        fonts.set(digs.name.clone(), Object::Reference(digs.id));
                        fonts.set(bdigs.name.clone(), Object::Reference(bdigs.id));
                        patched = patch_ftoc_bytes(
                            &patched,
                            &spec.toc_pages,
                            &out_doc,
                            &fonts,
                            digs,
                            bdigs,
                        );
                    }
                    if has_fpr {
                        fonts.set(digs.name.clone(), Object::Reference(digs.id));
                        fonts.set(bdigs.name.clone(), Object::Reference(bdigs.id));
                        fonts.set(sdigs.name.clone(), Object::Reference(sdigs.id));
                        patched = patch_fpr_bytes(
                            &patched,
                            &spec.label_pages,
                            &out_doc,
                            &fonts,
                            digs,
                            bdigs,
                            sdigs,
                        );
                    }

                    let mut xobj_opt: Option<Dictionary> = None;
                    if need_stamp {
                        fonts.set(digs.name.clone(), Object::Reference(digs.id));
                        let stamp_ops = build_stamp_ops(
                            ps,
                            abs_pg,
                            lid,
                            rid,
                            &spec.kit_info,
                            digs,
                            &atlas_forms,
                            &mut xobj_opt,
                            &out_doc,
                            &res,
                        );
                        if !stamp_ops.is_empty() {
                            patched.push(b'\n');
                            for op in &stamp_ops {
                                patched.extend_from_slice(op.as_bytes());
                                patched.push(b'\n');
                            }
                        }
                    }

                    res.set(b"Font", Object::Dictionary(fonts));
                    if let Some(xd) = xobj_opt {
                        res.set(b"XObject", Object::Dictionary(xd));
                    }

                    let compressed = deflate_fast(&patched)?;
                    let mut sdict = Dictionary::new();
                    sdict.set(b"Filter", Object::Name(b"FlateDecode".to_vec()));
                    let ns_id = out_doc.add_object(Object::Stream(Stream::new(sdict, compressed)));

                    if let Ok(Object::Dictionary(pd)) = out_doc.get_object_mut(pg_id) {
                        pd.set(b"Resources", Object::Dictionary(res));
                        pd.set(b"Contents", Object::Reference(ns_id));
                    }
                }
            }
        }
    }

    // 5. Rewrite GoToR -> GoTo for internal cross-chunk links
    for act_id in annot_action_ids {
        if let Ok(Object::Dictionary(ad)) = out_doc.get_object_mut(act_id) {
            rewrite_gotor_dict(ad, &dests);
        }
    }
    for pg_id in pages_with_inline_annots {
        rewrite_inline_gotor_on_page(&mut out_doc, pg_id, &dests);
    }

    // 6. Build /Pages root, /Outlines, /Names->/Dests, and /Catalog
    let total_pages = out_page_ids.len();
    let kids: Vec<Object> = out_page_ids.iter().map(|&id| Object::Reference(id)).collect();
    let mut pages_dict = Dictionary::new();
    pages_dict.set(b"Type", Object::Name(b"Pages".to_vec()));
    pages_dict.set(b"Kids", Object::Array(kids));
    pages_dict.set(b"Count", Object::Integer(total_pages as i64));
    out_doc
        .objects
        .insert(pages_root_id, Object::Dictionary(pages_dict));

    let outlines_id = build_outlines(&mut out_doc, &spec.outline, &out_page_ids);
    let names_id = build_dests_names_dict(&mut out_doc, &dests);

    let mut catalog = Dictionary::new();
    catalog.set(b"Type", Object::Name(b"Catalog".to_vec()));
    catalog.set(b"Pages", Object::Reference(pages_root_id));
    if let Some(oid) = outlines_id {
        catalog.set(b"Outlines", Object::Reference(oid));
    }
    if let Some(nid) = names_id {
        catalog.set(b"Names", Object::Reference(nid));
    }
    out_doc
        .objects
        .insert(catalog_id, Object::Dictionary(catalog));
    out_doc.trailer.set(b"Root", Object::Reference(catalog_id));

    // 7. Save with large buffered writer and atomic rename
    let tmp = format!("{}.tmp", spec.out);
    {
        let f = File::create(&tmp).map_err(|e| format!("create {} failed: {}", tmp, e))?;
        let mut bw = BufWriter::with_capacity(1024 * 1024, f);
        out_doc
            .save_to(&mut bw)
            .map_err(|e| format!("save PDF failed: {}", e))?;
        bw.flush().map_err(|e| e.to_string())?;
    }
    fs::rename(&tmp, &spec.out)
        .map_err(|e| format!("rename {} -> {} failed: {}", tmp, spec.out, e))?;

    Ok(total_pages)
}

fn collect_gotor_annots(
    doc: &Document,
    page_id: ObjectId,
    action_ids: &mut Vec<ObjectId>,
    inline_pages: &mut Vec<ObjectId>,
) {
    let annots_obj = match doc
        .get_object(page_id)
        .ok()
        .and_then(|o| o.as_dict().ok())
        .and_then(|d| d.get(b"Annots").ok())
    {
        Some(a) => a,
        None => return,
    };
    let arr = match resolve_obj(doc, annots_obj) {
        Some(Object::Array(a)) => a,
        _ => return,
    };
    let mut has_inline = false;
    for item in arr {
        match item {
            Object::Reference(aid) => {
                if let Ok(Object::Dictionary(ad)) = doc.get_object(*aid) {
                    match ad.get(b"A").ok() {
                        Some(Object::Reference(act_id)) => {
                            if let Ok(Object::Dictionary(act)) = doc.get_object(*act_id) {
                                if is_gotor(act) {
                                    action_ids.push(*act_id);
                                }
                            }
                        }
                        Some(Object::Dictionary(act)) => {
                            if is_gotor(act) {
                                // Inline /A inside indirect annot dict: handle by pushing annot id or inline_pages
                                has_inline = true;
                            }
                        }
                        _ => {}
                    }
                }
            }
            Object::Dictionary(ad) => {
                if let Some(Object::Dictionary(act)) = ad.get(b"A").ok() {
                    if is_gotor(act) {
                        has_inline = true;
                    }
                } else if let Some(Object::Reference(act_id)) = ad.get(b"A").ok() {
                    if let Ok(Object::Dictionary(act)) = doc.get_object(*act_id) {
                        if is_gotor(act) {
                            action_ids.push(*act_id);
                        }
                    }
                }
            }
            _ => {}
        }
    }
    if has_inline {
        inline_pages.push(page_id);
    }
}

#[inline]
fn is_gotor(act: &Dictionary) -> bool {
    matches!(act.get(b"S").ok(), Some(Object::Name(n)) if n.as_slice() == b"GoToR")
}

fn rewrite_gotor_dict(act: &mut Dictionary, dests: &HashMap<Vec<u8>, (ObjectId, Vec<Object>)>) {
    if !is_gotor(act) {
        return;
    }
    let d_bytes = match act.get(b"D").ok() {
        Some(Object::String(b, _)) => b.clone(),
        _ => return,
    };
    if dests.contains_key(&d_bytes) {
        act.set(b"S", Object::Name(b"GoTo".to_vec()));
        act.set(b"D", Object::String(d_bytes, StringFormat::Literal));
        act.remove(b"F");
    }
}

fn rewrite_inline_gotor_on_page(
    doc: &mut Document,
    page_id: ObjectId,
    dests: &HashMap<Vec<u8>, (ObjectId, Vec<Object>)>,
) {
    // First handle indirect annotation dicts with inline /A
    let annot_ids: Vec<ObjectId> = {
        let annots_obj = doc
            .get_object(page_id)
            .ok()
            .and_then(|o| o.as_dict().ok())
            .and_then(|d| d.get(b"Annots").ok());
        match annots_obj.and_then(|a| resolve_obj(doc, a)) {
            Some(Object::Array(arr)) => arr
                .iter()
                .filter_map(|o| o.as_reference().ok())
                .collect(),
            _ => Vec::new(),
        }
    };
    for aid in annot_ids {
        if let Ok(Object::Dictionary(ad)) = doc.get_object_mut(aid) {
            if let Ok(Object::Dictionary(act)) = ad.get_mut(b"A") {
                rewrite_gotor_dict(act, dests);
            }
        }
    }
    // Also handle fully inline annotations inside /Annots array
    if let Ok(Object::Dictionary(pd)) = doc.get_object_mut(page_id) {
        if let Ok(Object::Array(arr)) = pd.get_mut(b"Annots") {
            for item in arr.iter_mut() {
                if let Object::Dictionary(ad) = item {
                    if let Ok(Object::Dictionary(act)) = ad.get_mut(b"A") {
                        rewrite_gotor_dict(act, dests);
                    }
                }
            }
        }
    }
}
