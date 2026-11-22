mod pdf;
mod postchunk;
mod prep;
mod runner;
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

fn run_merge_job(raw: &[u8]) -> Result<JobResult, String> {
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

fn read_input(arg: &str) -> Vec<u8> {
    if arg == "-" {
        let mut buf = Vec::new();
        if let Err(e) = io::stdin().read_to_end(&mut buf) {
            eprintln!("read stdin failed: {}", e);
            process::exit(1);
        }
        buf
    } else {
        match fs::read(arg) {
            Ok(b) => b,
            Err(e) => {
                eprintln!("read {} failed: {}", arg, e);
                process::exit(1);
            }
        }
    }
}

fn main() {
    let args: Vec<String> = env::args().collect();
    if args.len() < 2 {
        eprintln!("usage: floria_merge [merge|prep|postchunk|runbatch] <spec.json | ->");
        process::exit(2);
    }

    let (mode, spec_arg) = match args[1].as_str() {
        "merge" | "prep" | "postchunk" | "runbatch" => {
            if args.len() < 3 {
                eprintln!("usage: floria_merge {} <spec.json | ->", args[1]);
                process::exit(2);
            }
            (args[1].as_str(), args[2].as_str())
        }
        _ => ("merge", args[1].as_str()),
    };

    let raw = read_input(spec_arg);

    match mode {
        "prep" => {
            let res: Result<prep::PrepResult, String> = serde_json::from_slice::<prep::PrepSpec>(&raw)
                .map_err(|e| format!("invalid prep json: {}", e))
                .and_then(|s| prep::run(&s));
            match res {
                Ok(r) => println!("{}", serde_json::to_string(&r).unwrap()),
                Err(err) => {
                    eprintln!(
                        "{}",
                        serde_json::json!({"ok": false, "error": err})
                    );
                    process::exit(1);
                }
            }
        }
        "postchunk" => {
            let res: Result<postchunk::PostChunkResult, String> =
                serde_json::from_slice::<postchunk::PostChunkSpec>(&raw)
                    .map_err(|e| format!("invalid postchunk json: {}", e))
                    .and_then(|s| postchunk::run_one(&s));
            match res {
                Ok(r) => println!("{}", serde_json::to_string(&r).unwrap()),
                Err(err) => {
                    eprintln!(
                        "{}",
                        serde_json::json!({"ok": false, "error": err})
                    );
                    process::exit(1);
                }
            }
        }
        "runbatch" => {
            let res: Result<runner::BatchResult, String> =
                serde_json::from_slice::<runner::BatchSpec>(&raw)
                    .map_err(|e| format!("invalid runbatch json: {}", e))
                    .and_then(|s| runner::run(&s));
            match res {
                Ok(r) => println!("{}", serde_json::to_string(&r).unwrap()),
                Err(err) => {
                    eprintln!(
                        "{}",
                        serde_json::json!({"event": "result", "ok": false, "error": err})
                    );
                    process::exit(1);
                }
            }
        }
        _ => match run_merge_job(&raw) {
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
                    error: Some(err),
                };
                eprintln!("{}", serde_json::to_string(&res).unwrap());
                process::exit(1);
            }
        },
    }
}
