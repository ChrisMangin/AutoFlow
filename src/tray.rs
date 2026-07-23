//! System tray icon with menu: Show, Record, Stop, Quit.
//! Must be created on the main thread.

use tray_icon::{TrayIcon, TrayIconBuilder, menu::{Menu, MenuEvent, MenuItem, PredefinedMenuItem}};
use tracing::info;
use crate::{state::SharedState, ws::{WsTx, emit_empty}};

pub struct Tray {
    _icon: TrayIcon,
}

impl Tray {
    pub fn new(ws_tx: WsTx, _app: SharedState) -> anyhow::Result<Self> {
        let menu = Menu::new();

        let item_show   = MenuItem::new("Show AutoFlow",    true, None);
        let item_record = MenuItem::new("Start Recording",  true, None);
        let item_stop   = MenuItem::new("Stop",             true, None);
        let item_sep    = PredefinedMenuItem::separator();
        let item_quit   = MenuItem::new("Quit",             true, None);

        menu.append_items(&[&item_show, &item_record, &item_stop, &item_sep, &item_quit])?;

        let show_id   = item_show.id().clone();
        let record_id = item_record.id().clone();
        let stop_id   = item_stop.id().clone();
        let quit_id   = item_quit.id().clone();

        let icon = load_icon();
        let tray = TrayIconBuilder::new()
            .with_menu(Box::new(menu))
            .with_tooltip("AutoFlow — Macro Recorder")
            .with_icon(icon)
            .build()?;

        let ws_tx2 = ws_tx.clone();
        std::thread::spawn(move || {
            let ch = MenuEvent::receiver();
            loop {
                if let Ok(event) = ch.recv() {
                    let id = event.id;
                    if id == show_id {
                        let _ = open::that("http://localhost:7878");
                    } else if id == record_id {
                        emit_empty(&ws_tx2, "start_record");
                    } else if id == stop_id {
                        emit_empty(&ws_tx2, "stop_record");
                        emit_empty(&ws_tx2, "stop_play");
                    } else if id == quit_id {
                        info!("Quit from tray");
                        std::process::exit(0);
                    }
                }
            }
        });

        Ok(Self { _icon: tray })
    }
}

fn load_icon() -> tray_icon::Icon {
    let icon_path = std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(|d| d.join("static").join("icon.png")));

    if let Some(path) = icon_path {
        if let Ok(bytes) = std::fs::read(&path) {
            if let Ok(img) = image::load_from_memory(&bytes) {
                let rgba = img.to_rgba8();
                let (w, h) = rgba.dimensions();
                if let Ok(icon) = tray_icon::Icon::from_rgba(rgba.into_raw(), w, h) {
                    return icon;
                }
            }
        }
    }

    // Fallback: blue 16×16 square
    let size: u32 = 16;
    let mut pixels = vec![0u8; (size * size * 4) as usize];
    for i in 0..(size * size) as usize {
        pixels[i*4] = 30; pixels[i*4+1] = 120; pixels[i*4+2] = 220; pixels[i*4+3] = 255;
    }
    tray_icon::Icon::from_rgba(pixels, size, size).expect("fallback icon")
}
