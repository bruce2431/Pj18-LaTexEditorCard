use crate::postchunk::{self, PostChunkSpec};
use regex::Regex;
use serde::{Deserialize, Serialize};
use std::collections::{HashMap, HashSet};
use std::fs;
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::{mpsc, Mutex, OnceLock};
use std::thread;
use std::time::{Duration, Instant};

#[cfg(windows)]
use std::os::windows::process::CommandExt;

#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x08000000;

#[derive(Deserialize)]
pub struct BatchSpec {
    pub book_dir: String,
    pub xelatex: String,
    pub xdvipdfmx: String,
    #[serde(default)]
    pub is_miktex: bool,
    #[serde(default = "default_parallel")]
    pub max_parallel: usize,
    #[serde(default = "default_timeout")]
    pub timeout_secs: u64,
    #[serde(default)]
    pub stream_events: bool,
    #[serde(default)]
    pub env: HashMap<String, String>,
    pub jobs: Vec<ChunkJob>,
}

fn default_parallel() -> usize {
    4
}

fn default_timeout() -> u64 {
    420
}

#[derive(Deserialize)]
pub struct ChunkJob {
    pub cid: String,
    pub wrapper_stripped: String,
    #[serde(default)]
    pub wrapper_full: Option<String>,
    #[serde(default)]
    pub toc_seed: Option<String>,
    #[serde(default)]
    pub single_pass: bool,
    #[serde(default)]
    pub ignore_abspage: bool,
    #[serde(default)]
    pub decoupled: bool,
    #[serde(default)]
    pub page_refs: bool,
    #[serde(default)]
    pub ref_names: Option<Vec<String>>,
    #[serde(default)]
    pub counter_deltas: HashMap<String, i64>,
}

#[derive(Serialize)]
pub struct ChunkRunResult {
    pub cid: String,
    pub ok: bool,
    pub secs: f64,
    pub xelatex_ms: f64,
    pub xdvipdfmx_ms: f64,
    pub post_ms: f64,
    pub passes: usize,
    pub retried_full: bool,
    pub out: String,
    pub pdf: String,
    pub synctex: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub body_pages: Option<usize>,
}

#[derive(Serialize)]
pub struct BatchResult {
    pub event: &'static str,
    pub ok: bool,
    pub total_ms: f64,
    pub results: Vec<ChunkRunResult>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

fn write_if_changed(path: &Path, content: &[u8]) -> io::Result<()> {
    if let Ok(existing) = fs::read(path) {
        if existing == content {
            return Ok(());
        }
    }
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let tmp = path.with_extension("tmp_w");
    fs::write(&tmp, content)?;
    fs::rename(&tmp, path)
}

fn take_snapshot(outdir: &Path) -> Vec<(PathBuf, Option<Vec<u8>>)> {
    const FILES: [&str; 4] = ["wrapper.aux", "wrapper.out", "wrapper.toc", "wrapper.fmeta"];
    FILES
        .iter()
        .map(|name| {
            let p = outdir.join(name);
            let data = fs::read(&p).ok();
            (p, data)
        })
        .collect()
}

fn restore_snapshot(snap: &[(PathBuf, Option<Vec<u8>>)]) {
    for (p, orig) in snap {
        match orig {
            Some(bytes) => {
                let tmp = p.with_extension("snap_tmp");
                if fs::write(&tmp, bytes).is_ok() {
                    let _ = fs::rename(&tmp, p);
                } else {
                    let _ = fs::write(p, bytes);
                }
            }
            None => {
                let _ = fs::remove_file(p);
            }
        }
    }
}

fn run_cmd_with_timeout(
    program: &str,
    args: &[&str],
    cwd: &Path,
    env_map: &HashMap<String, String>,
    timeout_secs: u64,
) -> (i32, String) {
    let mut cmd = Command::new(program);
    cmd.args(args)
        .current_dir(cwd)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());

    if !env_map.is_empty() {
        cmd.env_clear();
        for (k, v) in env_map {
            cmd.env(k, v);
        }
    }

    #[cfg(windows)]
    cmd.creation_flags(CREATE_NO_WINDOW);

    let mut child = match cmd.spawn() {
        Ok(c) => c,
        Err(e) => return (-1, format!("spawn {} failed: {}", program, e)),
    };

    let stdout_pipe = child.stdout.take();
    let stderr_pipe = child.stderr.take();

    let t_out = thread::spawn(move || {
        let mut buf = Vec::new();
        if let Some(mut s) = stdout_pipe {
            let _ = s.read_to_end(&mut buf);
        }
        buf
    });
    let t_err = thread::spawn(move || {
        let mut buf = Vec::new();
        if let Some(mut s) = stderr_pipe {
            let _ = s.read_to_end(&mut buf);
        }
        buf
    });

    let (tx, rx) = mpsc::channel();
    let waiter = thread::spawn(move || {
        let st = child.wait();
        let _ = tx.send((st, child));
    });

    let code = match rx.recv_timeout(Duration::from_secs(timeout_secs)) {
        Ok((Ok(status), _child)) => status.code().unwrap_or(1),
        Ok((Err(_), _child)) => 1,
        Err(_) => {
            // Timed out — best effort kill via taskkill on Windows if still alive
            return (124, format!("timeout after {}s: {}", timeout_secs, program));
        }
    };
    let _ = waiter.join();

    let out_b = t_out.join().unwrap_or_default();
    let err_b = t_err.join().unwrap_or_default();
    let mut combined = String::with_capacity(out_b.len() + err_b.len());
    combined.push_str(&String::from_utf8_lossy(&out_b));
    combined.push_str(&String::from_utf8_lossy(&err_b));
    (code, combined)
}

fn parse_newlabel_num_anchor(line: &str) -> Option<(String, String, String)> {
    let ln = line.trim_end_matches('\r');
    if !ln.starts_with("\\newlabel{") {
        return None;
    }
    let bytes = ln.as_bytes();
    let (name, i1) = crate::prep::read_group(bytes, "\\newlabel".len())?;
    let (body, _) = crate::prep::read_group(bytes, i1)?;
    let bbytes = body.as_bytes();
    let (g1, j1) = crate::prep::read_group(bbytes, 0)?;
    let (_g2, j2) = crate::prep::read_group(bbytes, j1)?;
    let anchor = if let Some((_g3, j3)) = crate::prep::read_group(bbytes, j2) {
        crate::prep::read_group(bbytes, j3)
            .map(|(g4, _)| g4.to_string())
            .unwrap_or_default()
    } else {
        String::new()
    };
    Some((name.to_string(), g1.to_string(), anchor))
}

fn shift_counter_newlabel_line(line: &str, deltas: &HashMap<String, i64>) -> String {
    let ln = line.trim_end_matches('\r');
    if !ln.starts_with("\\newlabel{") {
        return ln.to_string();
    }
    let bytes = ln.as_bytes();
    let Some((name, i1)) = crate::prep::read_group(bytes, "\\newlabel".len()) else {
        return ln.to_string();
    };
    let Some((body, i2)) = crate::prep::read_group(bytes, i1) else {
        return ln.to_string();
    };
    let bbytes = body.as_bytes();
    let Some((g1, j1)) = crate::prep::read_group(bbytes, 0) else {
        return ln.to_string();
    };
    let Some((g2, j2)) = crate::prep::read_group(bbytes, j1) else {
        return ln.to_string();
    };
    let Some((g3, j3)) = crate::prep::read_group(bbytes, j2) else {
        return ln.to_string();
    };
    let Some((g4, j4)) = crate::prep::read_group(bbytes, j3) else {
        return ln.to_string();
    };

    let dot_parts: Vec<&str> = g4.split('.').collect();
    if dot_parts.len() < 2 {
        return ln.to_string();
    }
    let raw_cname = dot_parts[0];
    let cname = if raw_cname == "Hfootnote" {
        "footnote"
    } else {
        raw_cname
    };

    let mut g4_segs: Vec<String> = dot_parts.iter().map(|s| (*s).to_string()).collect();
    let mut g1_segs: Vec<String> = g1.split('.').map(|s| s.to_string()).collect();
    let mut changed = false;

    let shift_pair = |g4_s: &mut Vec<String>,
                      g1_s: &mut Vec<String>,
                      idx_from_end: usize,
                      delta: i64|
     -> bool {
        if delta == 0 || g4_s.len() < idx_from_end + 1 || g1_s.len() < idx_from_end {
            return false;
        }
        let i4 = g4_s.len() - idx_from_end;
        let i1_pos = g1_s.len() - idx_from_end;
        if let (Ok(v4), Ok(v1)) = (g4_s[i4].parse::<i64>(), g1_s[i1_pos].parse::<i64>()) {
            if v4 + delta >= 1 && v1 + delta >= 1 {
                g4_s[i4] = (v4 + delta).to_string();
                g1_s[i1_pos] = (v1 + delta).to_string();
                return true;
            }
        }
        false
    };

    let dchap = deltas.get("chapter").copied().unwrap_or(0);
    if dchap != 0 {
        if cname == "chapter" {
            if shift_pair(&mut g4_segs, &mut g1_segs, 1, dchap) {
                changed = true;
            }
        } else if g4_segs.len() >= 3 && !g1_segs.is_empty() {
            if let (Ok(v4), Ok(v1)) = (g4_segs[1].parse::<i64>(), g1_segs[0].parse::<i64>()) {
                if v4 == v1 && v4 + dchap >= 1 {
                    g4_segs[1] = (v4 + dchap).to_string();
                    g1_segs[0] = (v1 + dchap).to_string();
                    changed = true;
                }
            }
        }
    }

    if cname == "subsection" {
        let ds = deltas.get("section").copied().unwrap_or(0);
        if shift_pair(&mut g4_segs, &mut g1_segs, 2, ds) {
            changed = true;
        }
    } else if cname == "subsubsection" {
        let ds = deltas.get("section").copied().unwrap_or(0);
        if shift_pair(&mut g4_segs, &mut g1_segs, 3, ds) {
            changed = true;
        }
        let dsub = deltas.get("subsection").copied().unwrap_or(0);
        if shift_pair(&mut g4_segs, &mut g1_segs, 2, dsub) {
            changed = true;
        }
    }

    if cname != "chapter" {
        let dc = deltas.get(cname).copied().unwrap_or(0);
        if shift_pair(&mut g4_segs, &mut g1_segs, 1, dc) {
            changed = true;
        }
    }

    if !changed {
        return ln.to_string();
    }

    format!(
        "\\newlabel{{{}}}{{{{{}}}{{{}}}{{{}}}{{{}}}{}}}{}",
        name,
        g1_segs.join("."),
        g2,
        g3,
        g4_segs.join("."),
        &body[j4..],
        &ln[i2..]
    )
}

fn preshift_aux_counters(outdir: &Path, deltas: &HashMap<String, i64>) {
    if deltas.is_empty() || deltas.values().all(|&v| v == 0) {
        return;
    }
    let aux_p = outdir.join("wrapper.aux");
    let Ok(raw) = fs::read(&aux_p) else {
        return;
    };
    let s = String::from_utf8_lossy(&raw);
    let mut out_lines = Vec::new();
    let mut any_changed = false;
    for ln in s.split('\n') {
        let clean = ln.trim_end_matches('\r');
        if clean.starts_with("\\newlabel{") {
            let shifted = shift_counter_newlabel_line(clean, deltas);
            if shifted != clean {
                any_changed = true;
            }
            out_lines.push(shifted);
        } else {
            out_lines.push(clean.to_string());
        }
    }
    if any_changed {
        let joined = out_lines.join("\n");
        let _ = write_if_changed(&aux_p, joined.as_bytes());
    }
}

fn newlabel_name_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"\\newlabel\{([^{}]*)\}").unwrap())
}

fn undef_ref_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"Reference `([^']*)' .*undefined").unwrap())
}

fn missing_file_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"LaTeX Error: File `([^']+)' not found").unwrap())
}

fn undef_cs_macro_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"\\([A-Za-z@]+)\s*$").unwrap())
}

fn label_num_sig(outdir: &Path, names: &HashSet<String>) -> HashMap<String, String> {
    if names.is_empty() {
        return HashMap::new();
    }
    let s = match fs::read(outdir.join("wrapper.aux")) {
        Ok(b) => String::from_utf8_lossy(&b).into_owned(),
        Err(_) => return HashMap::new(),
    };
    let mut d = HashMap::new();
    for ln in s.lines() {
        if let Some((nm, g1, anchor)) = parse_newlabel_num_anchor(ln) {
            if names.contains(&nm) {
                d.insert(nm, format!("{}\0{}", g1, anchor));
            }
        }
    }
    d
}

fn own_aux_labels(outdir: &Path) -> HashSet<String> {
    let s = match fs::read(outdir.join("wrapper.aux")) {
        Ok(b) => String::from_utf8_lossy(&b).into_owned(),
        Err(_) => return HashSet::new(),
    };
    newlabel_name_re()
        .captures_iter(&s)
        .map(|c| c[1].to_string())
        .collect()
}

fn log_needs_rerun(outdir: &Path) -> bool {
    let lg = match fs::read(outdir.join("wrapper.log")) {
        Ok(b) => String::from_utf8_lossy(&b).into_owned(),
        Err(_) => return false,
    };
    if lg.contains("Table widths have changed") {
        return true;
    }
    let mut undefs = HashSet::new();
    for cap in undef_ref_re().captures_iter(&lg) {
        undefs.insert(cap[1].to_string());
    }
    if !undefs.is_empty() {
        let own = own_aux_labels(outdir);
        if undefs.iter().any(|u| own.contains(u)) {
            return true;
        }
    }
    false
}

fn aux_state(outdir: &Path, ignore_abspage: bool) -> [Vec<u8>; 3] {
    const NAMES: [&str; 3] = ["wrapper.aux", "wrapper.out", "wrapper.toc"];
    let mut res = [Vec::new(), Vec::new(), Vec::new()];
    for (idx, &n) in NAMES.iter().enumerate() {
        if let Ok(data) = fs::read(outdir.join(n)) {
            if ignore_abspage && idx == 0 {
                let mut filtered = Vec::with_capacity(data.len());
                for ln in data.split(|&b| b == b'\n') {
                    if !ln.starts_with(b"\\gdef \\@abspage@last") {
                        filtered.extend_from_slice(ln);
                        filtered.push(b'\n');
                    }
                }
                res[idx] = filtered;
            } else {
                res[idx] = data;
            }
        }
    }
    res
}

/// Decide whether a pass-0 xelatex failure with stripped preamble could plausibly
/// be caused by `strip_preamble`, or is a definite user syntax/file error where
/// retrying with the full preamble would just waste another 3-4 seconds.
fn should_retry_with_full_preamble(out: &str, outdir: &Path) -> bool {
    let log_text = fs::read(outdir.join("wrapper.log"))
        .map(|b| String::from_utf8_lossy(&b).into_owned())
        .unwrap_or_default();
    let combined = if log_text.is_empty() {
        out
    } else {
        log_text.as_str()
    };

    const SYNTAX_PATTERNS: [&str; 18] = [
        "File ended while scanning use of",
        "Runaway argument?",
        "Paragraph ended before",
        "Missing $ inserted",
        "Display math should end with $$",
        "Missing { inserted",
        "Missing } inserted",
        "Extra }, or forgotten",
        "Too many }'s",
        "Misplaced alignment tab character &",
        "Extra alignment tab has been changed",
        "Misplaced \\noalign",
        "There's no line here to end",
        "Lonely \\item",
        "Text line contains an invalid character",
        "Unable to load picture or PDF file",
        "Double subscript",
        "Double superscript",
    ];
    for pat in &SYNTAX_PATTERNS {
        if combined.contains(pat) {
            return false;
        }
    }

    // Mismatched \begin{X} ... ended by \end{Y}
    if combined.contains("LaTeX Error: \\begin{") && combined.contains("ended by \\end{") {
        return false;
    }

    // Missing image / input file (as opposed to missing .sty/.cls/.def/.fd/.cfg)
    for cap in missing_file_re().captures_iter(combined) {
        let fname = cap[1].to_ascii_lowercase();
        let is_pkg_file = fname.ends_with(".sty")
            || fname.ends_with(".cls")
            || fname.ends_with(".def")
            || fname.ends_with(".fd")
            || fname.ends_with(".cfg")
            || fname.ends_with(".clo")
            || fname.ends_with(".tex");
        if !is_pkg_file {
            return false;
        }
    }

    // Undefined control sequence: check if the macro could come from any stripped package
    if let Some(idx) = combined.find("Undefined control sequence") {
        let after = &combined[idx..];
        let mut lines = after.lines();
        let _ = lines.next(); // "Undefined control sequence." line
        for _ in 0..4 {
            let Some(ln) = lines.next() else { break };
            if let Some(cap) = undef_cs_macro_re().captures(ln) {
                let m = &cap[1];
                if m.contains('@') {
                    return true;
                }
                const STRIPPED_PREFIXES: &[&str] = &[
                    "tikz",
                    "pgf",
                    "chem",
                    "scheme",
                    "setchem",
                    "definesubmol",
                    "lewis",
                    "arrow",
                    "polymerdelim",
                    "ce",
                    "pu",
                    "toprule",
                    "midrule",
                    "bottomrule",
                    "cmidrule",
                    "addlinespace",
                    "specialrule",
                    "subcaption",
                    "subfigure",
                    "subtable",
                    "caption",
                    "longtable",
                    "endfirsthead",
                    "endhead",
                    "endfoot",
                    "endlastfoot",
                    "tabularx",
                    "array",
                    "extrarowheight",
                    "newcolumntype",
                    "addplot",
                    "axis",
                    "float",
                    "restylefloat",
                    "newfloat",
                    "color",
                    "textcolor",
                    "colorbox",
                    "definecolor",
                ];
                const TIKZ_CMDS: [&str; 8] = [
                    "draw",
                    "node",
                    "fill",
                    "filldraw",
                    "path",
                    "clip",
                    "coordinate",
                    "shadedraw",
                ];
                let may_be_stripped = STRIPPED_PREFIXES.iter().any(|p| m.starts_with(p))
                    || TIKZ_CMDS.contains(&m);
                return may_be_stripped;
            }
        }
    }

    true
}

fn run_one_job(
    spec: &BatchSpec,
    job: &ChunkJob,
    stdout_lock: Option<&Mutex<()>>,
) -> ChunkRunResult {
    let t0 = Instant::now();
    let book_dir = Path::new(&spec.book_dir);
    let outdir = book_dir.join(".build").join("floria").join("out").join(&job.cid);
    let _ = fs::create_dir_all(&outdir);

    let snap = take_snapshot(&outdir);
    preshift_aux_counters(&outdir, &job.counter_deltas);
    let wrapper_path = outdir.join("wrapper.tex");
    let _ = write_if_changed(&wrapper_path, job.wrapper_stripped.as_bytes());

    let rel_out = format!(".build/floria/out/{}", job.cid);
    let rel_wrap = format!("{}/wrapper.tex", rel_out);
    let rel_pdf = format!("{}/chapter.pdf", rel_out);
    let rel_xdv = format!("{}/wrapper.xdv", rel_out);
    let out_dir_arg = format!("-output-directory={}", rel_out);

    let seed_toc = |seed_opt: &Option<String>| {
        if let Some(s) = seed_opt {
            let _ = fs::write(outdir.join("wrapper.toc"), s.as_bytes());
        }
    };

    let ref_set: Option<HashSet<String>> = job
        .ref_names
        .as_ref()
        .map(|v| v.iter().cloned().collect());

    let max_passes = if job.single_pass { 1usize } else { 4usize };
    let mut rc = 1i32;
    let mut out = String::new();
    let mut passes_run = 0usize;
    let mut retried_full = false;

    let t_xe0 = Instant::now();
    let mut xe_args: Vec<&str> = Vec::with_capacity(10);
    if spec.is_miktex {
        xe_args.push("--disable-installer");
    }
    xe_args.extend_from_slice(&[
        "-no-pdf",
        "-synctex=1",
        "-interaction=nonstopmode",
        "-halt-on-error",
        "-file-line-error",
        out_dir_arg.as_str(),
        rel_wrap.as_str(),
    ]);

    for p_idx in 0..max_passes {
        let before_aux = if !job.decoupled {
            Some(aux_state(&outdir, job.ignore_abspage))
        } else {
            None
        };
        let mut refs_before = ref_set
            .as_ref()
            .map(|rs| label_num_sig(&outdir, rs))
            .unwrap_or_default();

        seed_toc(&job.toc_seed);
        passes_run += 1;
        let (code, cmd_out) = run_cmd_with_timeout(
            &spec.xelatex,
            &xe_args,
            book_dir,
            &spec.env,
            spec.timeout_secs,
        );
        rc = code;
        out.push_str(&cmd_out);

        if rc != 0 && p_idx == 0 {
            if let Some(ref full_wrap) = job.wrapper_full {
                if should_retry_with_full_preamble(&out, &outdir) {
                    retried_full = true;
                    out.clear();
                    restore_snapshot(&snap);
                    preshift_aux_counters(&outdir, &job.counter_deltas);
                    refs_before = ref_set
                        .as_ref()
                        .map(|rs| label_num_sig(&outdir, rs))
                        .unwrap_or_default();
                    let _ = write_if_changed(&wrapper_path, full_wrap.as_bytes());
                    seed_toc(&job.toc_seed);
                    passes_run += 1;
                    let (code2, cmd_out2) = run_cmd_with_timeout(
                        &spec.xelatex,
                        &xe_args,
                        book_dir,
                        &spec.env,
                        spec.timeout_secs,
                    );
                    rc = code2;
                    out.push_str(&cmd_out2);
                }
            }
        }

        if rc != 0 {
            break;
        }

        if job.decoupled {
            if let Some(ref rs) = ref_set {
                if label_num_sig(&outdir, rs) == refs_before && !log_needs_rerun(&outdir) {
                    break;
                }
            }
        } else {
            if let Some(ref b_aux) = before_aux {
                if &aux_state(&outdir, job.ignore_abspage) == b_aux {
                    break;
                }
            }
            if let Some(ref rs) = ref_set {
                if !job.page_refs && label_num_sig(&outdir, rs) == refs_before {
                    break;
                }
            }
        }
    }
    let xelatex_ms = t_xe0.elapsed().as_secs_f64() * 1000.0;

    if rc == 0 {
        if let Some(lk) = stdout_lock {
            if let Ok(_guard) = lk.lock() {
                let evt = serde_json::json!({
                    "event": "xelatex_done",
                    "cid": job.cid,
                });
                println!("{}", evt);
                let _ = io::stdout().flush();
            }
        }
    }

    let mut xdvipdfmx_ms = 0.0;
    if rc == 0 {
        let t_xdv0 = Instant::now();
        let xdv_args = ["-E", "-z", "1", "-o", &rel_pdf, &rel_xdv];
        let (code_xdv, out_xdv) = run_cmd_with_timeout(
            &spec.xdvipdfmx,
            &xdv_args,
            book_dir,
            &spec.env,
            spec.timeout_secs,
        );
        rc = code_xdv;
        out.push_str(&out_xdv);
        xdvipdfmx_ms = t_xdv0.elapsed().as_secs_f64() * 1000.0;
    }

    let pdf_full = outdir.join("chapter.pdf");
    let sync_full = outdir.join("wrapper.synctex.gz");
    let mut post_ms = 0.0;
    let mut body_pages = None;

    if rc == 0 && pdf_full.is_file() {
        let t_post0 = Instant::now();
        let post_spec = PostChunkSpec {
            pdf_path: pdf_full.to_string_lossy().into_owned(),
            fmeta_path: if job.decoupled {
                Some(outdir.join("wrapper.fmeta").to_string_lossy().into_owned())
            } else {
                None
            },
            sync_gz_path: sync_full.to_string_lossy().into_owned(),
            slim_out_path: outdir
                .join("wrapper.synctex.slim")
                .to_string_lossy()
                .into_owned(),
            chunk_name: format!("{}.tex", job.cid),
            decoupled: job.decoupled,
            max_pages: None,
        };
        if let Ok(pr) = postchunk::run_one(&post_spec) {
            body_pages = pr.body_pages;
        }
        post_ms = t_post0.elapsed().as_secs_f64() * 1000.0;
    } else {
        // Transactional rollback: never leave half-written .aux/.out/.toc/.fmeta on error!
        restore_snapshot(&snap);
    }

    ChunkRunResult {
        cid: job.cid.clone(),
        ok: rc == 0 && pdf_full.is_file(),
        secs: t0.elapsed().as_secs_f64(),
        xelatex_ms,
        xdvipdfmx_ms,
        post_ms,
        passes: passes_run,
        retried_full,
        out,
        pdf: pdf_full.to_string_lossy().into_owned(),
        synctex: sync_full.to_string_lossy().into_owned(),
        body_pages,
    }
}

pub fn run(spec: &BatchSpec) -> Result<BatchResult, String> {
    let t0 = Instant::now();
    let stdout_lock = Mutex::new(());
    let lock_ref = if spec.stream_events {
        Some(&stdout_lock)
    } else {
        None
    };

    let results: Vec<ChunkRunResult> = if spec.jobs.len() <= 1 || spec.max_parallel <= 1 {
        spec.jobs
            .iter()
            .map(|job| run_one_job(spec, job, lock_ref))
            .collect()
    } else {
        let n_threads = spec.max_parallel.min(spec.jobs.len()).max(1);
        let pool = rayon::ThreadPoolBuilder::new()
            .num_threads(n_threads)
            .build()
            .map_err(|e| format!("build thread pool failed: {}", e))?;
        pool.install(|| {
            use rayon::prelude::*;
            spec.jobs
                .par_iter()
                .map(|job| run_one_job(spec, job, lock_ref))
                .collect()
        })
    };

    let all_ok = results.iter().all(|r| r.ok);
    Ok(BatchResult {
        event: "result",
        ok: all_ok,
        total_ms: t0.elapsed().as_secs_f64() * 1000.0,
        results,
        error: None,
    })
}
