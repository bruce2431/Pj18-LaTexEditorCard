mod pdf;
mod synctex;

use serde::{Deserialize, Serialize};
use std::env;
use std::fs;
use std::io::{self, Read};
use std::process;
use std::time::Instant;

#[derive(Deserialize)]
struct JobSpec {
    #[serde(default)]
    pdf: Option<pdf::PdfSpec>,
    #[serde(default)]
    synctex: Option<synctex::SynctexSpec>,
}

#[derive(Serialize)]
struct JobResult {
    ok: bool,
    total_pages: usize,
    pdf_ms: f64,
    synctex_ms: f64,
    total_ms: f64,
    #[serde(skip_serializing_if = "Option::is_none")]
    error: Option<String>,
}

fn run_job(raw: &[u8]) -> Result<JobResult, String> {
    let t0 = Instant::now();
    let spec: JobSpec =
        serde_json::from_slice(raw).map_err(|e| format!("invalid spec json: {}", e))?;

    let (pdf_res, stx_res): (Result<(usize, f64), String>, Result<f64, String>) = rayon::join(
        || match &spec.pdf {
            Some(ps) => {
                let tp = Instant::now();
                let pages = pdf::run(ps)?;
                Ok((pages, tp.elapsed().as_secs_f64() * 1000.0))
            }
            None => Ok((0, 0.0)),
        },
        || match &spec.synctex {
            Some(ss) => {
                let ts = Instant::now();
                synctex::run(ss)?;
                Ok(ts.elapsed().as_secs_f64() * 1000.0)
            }
            None => Ok(0.0),
        },
    );

    let (total_pages, pdf_ms) = pdf_res?;
    let synctex_ms = stx_res?;
    let total_ms = t0.elapsed().as_secs_f64() * 1000.0;

    Ok(JobResult {
        ok: true,
        total_pages,
        pdf_ms,
        synctex_ms,
        total_ms,
        error: None,
    })
}

fn main() {
    let args: Vec<String> = env::args().collect();
    if args.len() < 2 {
        eprintln!("usage: floria_merge <spec.json | ->");
        process::exit(2);
    }

    let raw = if args[1] == "-" {
        let mut buf = Vec::new();
        if let Err(e) = io::stdin().read_to_end(&mut buf) {
            eprintln!("read stdin failed: {}", e);
            process::exit(1);
        }
        buf
    } else {
        match fs::read(&args[1]) {
            Ok(b) => b,
            Err(e) => {
                eprintln!("read {} failed: {}", args[1], e);
                process::exit(1);
            }
        }
    };

    match run_job(&raw) {
        Ok(res) => {
            println!("{}", serde_json::to_string(&res).unwrap());
        }
        Err(err) => {
            let res = JobResult {
                ok: false,
                total_pages: 0,
                pdf_ms: 0.0,
                synctex_ms: 0.0,
                total_ms: 0.0,
                error: Some(err.clone()),
            };
            eprintln!("{}", serde_json::to_string(&res).unwrap());
            process::exit(1);
        }
    }
}
