//! PDF report, Python script, and ZIP runnable-package export.
//! PDF: we return a fully-styled HTML page; the browser prints it as PDF.
//! Script/ZIP: pure string generation.

use crate::state::Step;
use anyhow::Result;
use serde::{Deserialize, Serialize};
use std::io::Write;

// ── Request payloads ──────────────────────────────────────────────────────────

#[derive(Debug, Deserialize)]
pub struct ExportRequest {
    pub name:      String,
    #[serde(default)]
    pub steps:     Vec<Step>,
    #[serde(default)]
    pub variables: serde_json::Map<String, serde_json::Value>,
    pub created:   Option<f64>,
}

// ── Helpers ───────────────────────────────────────────────────────────────────

fn esc(s: &str) -> String {
    s.replace('&', "&amp;")
     .replace('<', "&lt;")
     .replace('>', "&gt;")
     .replace('"', "&quot;")
}

fn step_icon(kind: &str) -> &'static str {
    match kind {
        "click"         => "🖱️",  "type"         => "⌨️",
        "hotkey"        => "⚡",  "scroll"       => "🔄",
        "wait"          => "⏱️",  "navigate"     => "🌐",
        "loop"          => "🔁",  "loop_end"     => "↩️",
        "if"            => "❓",  "else"         => "↕️",
        "end_if"        => "✓",   "set_variable" => "📦",
        "run_script"    => "⚙️",  "screenshot"   => "📸",
        "comment"       => "💬",  "error_handler"=> "🛡️",
        "launch_browser"=> "🚀",  "show_message" => "📢",
        "wait_for_element"=>"⏳","wait_for_window"=>"🪟",
        "get_clipboard" => "📋",  "set_clipboard"=> "📌",
        "image_click"   => "🖼️",  "read_file"    => "📄",
        "write_file"    => "📝",  "copy_file"    => "📑",
        "move_file"     => "📦",  "delete_file"  => "🗑️",
        "http_request"  => "🌍",  "kill_process" => "⛔",
        "close_window"  => "❌",  "open_file"    => "📂",
        "play_sound"    => "🔊",
        _               => "❓",
    }
}

fn step_desc(step: &Step) -> String {
    let d = &step.data;
    let g = |k: &str| d.get(k).and_then(|v| v.as_str()).unwrap_or("").to_string();
    let gn = |k: &str| d.get(k).and_then(|v| v.as_f64()).unwrap_or(0.0);
    match step.kind.as_str() {
        "click" => {
            let base = format!("({}, {}) — {} click", gn("x") as i64, gn("y") as i64, g("button").if_empty("left"));
            if let Some(el) = d.get("element") {
                let name = el.get("name").and_then(|v| v.as_str()).unwrap_or("");
                let win  = el.get("window").and_then(|v| v.as_str()).unwrap_or("");
                if !name.is_empty() {
                    return format!("<strong>{}</strong>{} &nbsp;·&nbsp; {}", esc(name),
                        if win.is_empty() { String::new() } else { format!(" in <em>{}</em>", esc(win)) },
                        base);
                }
            }
            base
        }
        "type"         => format!("Type: <code>\"{}\"</code>", esc(&g("text"))),
        "hotkey"       => format!("Hotkey: <code>{}</code>", esc(&g("combo"))),
        "wait"         => format!("Wait {} ms", gn("ms") as u64),
        "navigate"     => format!("Navigate to <code>{}</code>", esc(&g("url"))),
        "loop"         => format!("Loop &times;{}", gn("count") as u64),
        "loop_end"     => "End Loop".into(),
        "if"           => format!("If <code>{{{}}}</code> = &ldquo;{}&rdquo;", g("var"), esc(&g("value"))),
        "else"         => "Else".into(),
        "end_if"       => "End If".into(),
        "set_variable" => format!("Set <code>{{{}}}</code> = &ldquo;{}&rdquo;", g("name"), esc(&g("value"))),
        "run_script"   => format!("Run: <code>{}</code>", esc(&g("command").chars().take(80).collect::<String>())),
        "comment"      => format!("<em>{}</em>", esc(&g("text"))),
        "screenshot"   => "Capture screenshot".into(),
        "wait_for_window" => format!("Wait for window &ldquo;{}&rdquo; ({} ms)", esc(&g("title")), gn("timeout_ms") as u64),
        "open_file"    => format!("Open <code>{}</code>", esc(&g("path"))),
        "show_message" => format!("Show: <strong>{}</strong> — {}", esc(&g("title")), esc(&g("message"))),
        "write_file"   => format!("Write file <code>{}</code>", esc(&g("path"))),
        "read_file"    => format!("Read <code>{}</code> → <code>{{{}}}</code>", esc(&g("path")), g("variable")),
        "http_request" => format!("{} <code>{}</code>", g("method").if_empty("GET"), esc(&g("url"))),
        "kill_process" => format!("Kill <code>{}</code>", esc(&g("name"))),
        "close_window" => format!("Close &ldquo;{}&rdquo;", esc(&g("title"))),
        _              => esc(&step.kind),
    }
}

trait IfEmpty {
    fn if_empty(self, fallback: &str) -> String;
}
impl IfEmpty for String {
    fn if_empty(self, fallback: &str) -> String {
        if self.is_empty() { fallback.into() } else { self }
    }
}

// ── PDF (HTML) ────────────────────────────────────────────────────────────────

pub fn generate_pdf_html(req: &ExportRequest) -> String {
    let created = req.created.unwrap_or(0.0);
    let created_str = {
        use std::time::{Duration, UNIX_EPOCH};
        let d = UNIX_EPOCH + Duration::from_secs_f64(created.max(0.0));
        let secs = d.duration_since(UNIX_EPOCH).unwrap_or_default().as_secs();
        // simple ISO-like formatting
        let s = secs % 60; let m = (secs / 60) % 60; let h = (secs / 3600) % 24;
        let days = secs / 86400;
        let year = 1970 + days / 365;
        format!("{}-{:02}-{:02} {:02}:{:02}:{:02}", year, (days % 365)/30+1, days%30+1, h, m, s)
    };

    let mut steps_html = String::new();
    for (i, step) in req.steps.iter().enumerate() {
        let icon = step_icon(&step.kind);
        let desc = step_desc(step);
        let note_html = step.note.as_deref().map(|n| {
            format!(r#"<div class="step-note"><span class="note-label">📝 Note</span>{}</div>"#, esc(n))
        }).unwrap_or_default();
        let disabled_tag = if step.disabled {
            r#"<span class="disabled-tag">SKIPPED</span>"#
        } else { "" };
        let img_html = step.data.get("screenshot_full")
            .or_else(|| step.data.get("screenshot"))
            .and_then(|v| v.as_str())
            .map(|b64| format!(r#"<div class="step-img"><img src="data:image/jpeg;base64,{}" alt="screenshot"></div>"#, b64))
            .unwrap_or_default();
        steps_html.push_str(&format!(r#"
        <div class="step{dis}">
          <div class="step-hdr">
            <span class="step-num">{num}</span>
            <span class="step-icon">{icon}</span>
            <span class="step-type">{kind}</span>
            {dtag}
          </div>
          {note}
          <div class="step-body"><div class="step-desc">{desc}</div>{img}</div>
        </div>"#,
            dis   = if step.disabled { " disabled" } else { "" },
            num   = i + 1,
            icon  = icon,
            kind  = step.kind.replace('_', " ").to_uppercase(),
            dtag  = disabled_tag,
            note  = note_html,
            desc  = desc,
            img   = img_html,
        ));
    }

    let vars_html = if req.variables.is_empty() {
        String::new()
    } else {
        let rows: String = req.variables.iter()
            .map(|(k, v)| format!("<tr><td><code>{{{{{}}}}}</code></td><td>{}</td></tr>",
                esc(k), esc(&v.as_str().unwrap_or("").to_string())))
            .collect();
        format!(r#"<div class="section"><h2>Variables</h2>
          <table class="vars-table"><thead><tr><th>Name</th><th>Value</th></tr></thead>
          <tbody>{}</tbody></table></div>"#, rows)
    };

    format!(r#"<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8">
<title>{name} — AutoFlow Report</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,"Segoe UI",system-ui,sans-serif;font-size:13px;color:#1a2030;background:#f4f6fb;padding:24px}}
h1{{font-size:22px;color:#1a2030;margin-bottom:4px}}
.meta{{color:#6b7a96;font-size:12px;margin-bottom:28px}}
h2{{font-size:14px;font-weight:700;text-transform:uppercase;letter-spacing:.6px;color:#6b7a96;margin:28px 0 10px;padding-bottom:6px;border-bottom:2px solid #dde4f0}}
.section{{background:#fff;border-radius:8px;padding:16px 20px;margin-bottom:20px;box-shadow:0 1px 4px rgba(0,0,0,.07)}}
.steps{{display:flex;flex-direction:column;gap:12px}}
.step{{background:#fff;border-radius:8px;border:1px solid #dde4f0;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.05)}}
.step.disabled{{opacity:.5}}
.step-hdr{{display:flex;align-items:center;gap:10px;padding:10px 14px;background:#f8f9fc;border-bottom:1px solid #dde4f0}}
.step-num{{width:24px;height:24px;border-radius:50%;background:#4f8ef7;color:#fff;display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:700;flex-shrink:0}}
.step-icon{{font-size:16px;flex-shrink:0}}
.step-type{{font-size:10.5px;font-weight:700;text-transform:uppercase;letter-spacing:.6px;color:#4f8ef7;flex:1}}
.disabled-tag{{font-size:10px;font-weight:700;text-transform:uppercase;color:#e05252;border:1px solid #e05252;border-radius:3px;padding:1px 6px}}
.step-note{{background:#e8fdf2;border-left:3px solid #3ec97a;padding:6px 12px;font-size:12px;color:#1a5c38;margin:0}}
.note-label{{font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;color:#3ec97a;display:block;margin-bottom:2px}}
.step-body{{padding:10px 14px}}
.step-desc{{font-size:13px;color:#2d3a4e;line-height:1.6}}
.step-img{{margin-top:10px}}
.step-img img{{max-width:100%;border-radius:4px;border:1px solid #dde4f0}}
code{{background:#eef0f6;border-radius:3px;padding:1px 4px;font-size:11.5px;font-family:"Consolas","Courier New",monospace;color:#4f8ef7}}
.vars-table{{width:100%;border-collapse:collapse;font-size:12px}}
.vars-table th{{background:#f4f6fb;color:#6b7a96;font-weight:600;text-align:left;padding:6px 10px;border-bottom:1px solid #dde4f0}}
.vars-table td{{padding:5px 10px;border-bottom:1px solid #f0f2f7}}
@media print{{body{{background:#fff;padding:0}}}}
</style></head><body>
<h1>⚡ {name}</h1>
<p class="meta">Created {created} &nbsp;·&nbsp; {count} steps &nbsp;·&nbsp; AutoFlow v1.0.0</p>
{vars}
<div class="section">
  <h2>Steps</h2>
  <div class="steps">{steps}</div>
</div>
</body></html>"#,
        name    = esc(&req.name),
        created = created_str,
        count   = req.steps.iter().filter(|s| !s.disabled).count(),
        vars    = vars_html,
        steps   = steps_html,
    )
}

// ── Python script export ──────────────────────────────────────────────────────

pub fn generate_script(req: &ExportRequest) -> String {
    let mut lines: Vec<String> = vec![
        "#!/usr/bin/env python3".into(),
        format!("# AutoFlow export — {}", req.name),
        "# Auto-installs pyautogui if needed.".into(),
        "import sys, subprocess, time, os, webbrowser".into(),
        "try: import pyautogui".into(),
        "except ImportError:".into(),
        "    subprocess.check_call([sys.executable,'-m','pip','install','pyautogui'])".into(),
        "    import pyautogui".into(),
        "import pyautogui".into(),
        "pyautogui.FAILSAFE = False".into(),
        String::new(),
    ];

    // Variables
    if !req.variables.is_empty() {
        lines.push("# Variables".into());
        for (k, v) in &req.variables {
            lines.push(format!("{} = {}", k, serde_json::to_string(v).unwrap_or_default()));
        }
        lines.push(String::new());
    }

    for step in &req.steps {
        if step.disabled {
            lines.push(format!("# [DISABLED] {}", step.kind));
            continue;
        }
        if let Some(note) = &step.note {
            lines.push(format!("# {}", note));
        }
        let d = &step.data;
        let g = |k: &str| d.get(k).and_then(|v| v.as_str()).unwrap_or("").to_string();
        let gn = |k: &str| d.get(k).and_then(|v| v.as_f64()).unwrap_or(0.0);
        let line = match step.kind.as_str() {
            "click"    => format!("pyautogui.click({}, {})", gn("x") as i64, gn("y") as i64),
            "type"     => format!("pyautogui.write({}, interval=0.03)", serde_json::to_string(&g("text")).unwrap()),
            "hotkey"   => {
                let keys: Vec<String> = g("combo").split('+').map(|k| format!("{:?}", k.trim())).collect();
                format!("pyautogui.hotkey({})", keys.join(", "))
            },
            "wait"     => format!("time.sleep({})", gn("ms") / 1000.0),
            "scroll"   => format!("pyautogui.scroll({}, x={}, y={})", gn("dy") as i64, gn("x") as i64, gn("y") as i64),
            "navigate" => format!("webbrowser.open({:?})", g("url")),
            "loop"     => format!("for _i in range({}):", gn("count") as u64),
            "loop_end" => "    pass  # end loop".into(),
            "comment"  => format!("# {}", g("text")),
            "set_variable" => format!("{} = {:?}", g("name"), g("value")),
            "run_script"   => g("command"),
            "open_file"    => format!("os.startfile({:?})", g("path")),
            "show_message" => format!("import tkinter.messagebox; tkinter.messagebox.showinfo({:?}, {:?})", g("title"), g("message")),
            "read_file"    => format!("with open({:?}) as _f: {} = _f.read()", g("path"), g("variable").if_empty("_content")),
            "write_file"   => format!("with open({:?}, {:?}) as _f: _f.write({:?})", g("path"), if d.get("append").and_then(|v| v.as_bool()).unwrap_or(false) { "a" } else { "w" }, g("text")),
            "kill_process" => format!("subprocess.run(['taskkill','/f','/im',{:?}])", g("name")),
            "close_window" => format!("import pygetwindow; [w.close() for w in pygetwindow.getWindowsWithTitle({:?})]", g("title")),
            "http_request" => format!("import urllib.request; {} = urllib.request.urlopen({:?}).read().decode()", g("variable").if_empty("_resp"), g("url")),
            "screenshot"   => "pyautogui.screenshot()".into(),
            "run_workflow"  => {
                let file = g("file");
                format!("exec(open(os.path.join(os.environ.get('APPDATA',''), 'AutoFlow', 'workflows', {f:?} + '.json')).read())  # run_workflow: {file}",
                    f = file)
            },
            _ => format!("# TODO: {} not yet supported in script export", step.kind),
        };
        lines.push(line);
    }

    lines.push(String::new());
    lines.push("print('Workflow complete.')".into());
    lines.join("\n")
}

// ── ZIP package ───────────────────────────────────────────────────────────────

pub fn generate_zip(req: &ExportRequest) -> Result<Vec<u8>> {
    let script = generate_script(req);
    let name = &req.name;

    let bat = format!(
        "@echo off\necho Running {name}...\npython --version >nul 2>&1 || (echo Python not found. Install from python.org && pause && exit /b 1)\npython -m pip install pyautogui -q\npython {name}.py\npause\n"
    );
    let readme = format!(
        "AutoFlow Runnable Package\n=========================\nWorkflow: {name}\n\nTo run:\n  1. Double-click run.bat\n  2. Python must be installed (python.org)\n     Check 'Add Python to PATH' during install.\n"
    );
    let req_txt = "pyautogui>=0.9.54\n";

    let buf = Vec::new();
    let cursor = std::io::Cursor::new(buf);
    let mut zip = zip::ZipWriter::new(cursor);
    let opts = zip::write::SimpleFileOptions::default()
        .compression_method(zip::CompressionMethod::Deflated);

    zip.start_file(format!("{name}.py"), opts)?;
    zip.write_all(script.as_bytes())?;
    zip.start_file("run.bat", opts)?;
    zip.write_all(bat.as_bytes())?;
    zip.start_file("requirements.txt", opts)?;
    zip.write_all(req_txt.as_bytes())?;
    zip.start_file("README.txt", opts)?;
    zip.write_all(readme.as_bytes())?;

    Ok(zip.finish()?.into_inner())
}


// ── Schedule ZIP (Windows Task Scheduler) ─────────────────────────────────────

#[derive(serde::Deserialize)]
pub struct ScheduleRequest {
    pub name:      String,
    #[serde(default)]
    pub steps:     Vec<crate::state::Step>,
    #[serde(default)]
    pub variables: std::collections::HashMap<String, serde_json::Value>,
    pub created:   Option<f64>,
    /// ISO-8601 trigger time, e.g. "2026-07-25T09:00:00"
    #[serde(default)]
    pub trigger_time: String,
    /// "once" | "daily" | "weekly"  (default "once")
    #[serde(default)]
    pub trigger_type: String,
}

// Export-request alias so the server can pass ExportRequest directly
pub fn generate_schedule_zip(req: &ExportRequest) -> Result<Vec<u8>> {
    let script  = generate_script(req);
    let name    = req.name.replace(' ', "_");
    let trigger = "2026-07-25T09:00:00"; // placeholder — user edits in Task Scheduler

    let bat = format!(
        "@echo off\ncd /d \"%~dp0\"\npython --version >nul 2>&1 || (echo Python not found && pause && exit /b 1)\npython -m pip install pyautogui -q\npython \"{name}.py\"\n"
    );

    let xml = format!(r#"<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>AutoFlow workflow: {wf_name}</Description>
    <Author>AutoFlow</Author>
  </RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <StartBoundary>{trigger}</StartBoundary>
      <Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT2H</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>cmd.exe</Command>
      <Arguments>/c "%SystemDrive%\AutoFlow_{name}\run.bat"</Arguments>
      <WorkingDirectory>%SystemDrive%\AutoFlow_{name}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>"#,
        wf_name  = &req.name,
        name     = name,
        trigger  = trigger,
    );

    let readme = format!(
        "AutoFlow Scheduled Workflow\n===========================\nWorkflow: {wf_name}\n\nSetup:\n\
        1. Extract this ZIP to a folder (e.g. C:\\AutoFlow_{name}\\)\n\
        2. Open Windows Task Scheduler (taskschd.msc)\n\
        3. Action -> Import Task -> select 'schedule.xml'\n\
        4. Set your desired trigger time in the Triggers tab\n\
        5. Click OK — the workflow will run automatically\n\n\
        To test manually: double-click run.bat\n",
        wf_name = &req.name,
        name    = name,
    );

    let buf    = Vec::new();
    let cursor = std::io::Cursor::new(buf);
    let mut zip = zip::ZipWriter::new(cursor);
    let opts = zip::write::SimpleFileOptions::default()
        .compression_method(zip::CompressionMethod::Deflated);

    zip.start_file(format!("{name}.py"), opts)?;
    zip.write_all(script.as_bytes())?;
    zip.start_file("run.bat", opts)?;
    zip.write_all(bat.as_bytes())?;
    zip.start_file("schedule.xml", opts)?;
    zip.write_all(xml.as_bytes())?;
    zip.start_file("README.txt", opts)?;
    zip.write_all(readme.as_bytes())?;

    Ok(zip.finish()?.into_inner())
}
