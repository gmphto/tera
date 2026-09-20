#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

//! The tera development shell: one window, one service, one snapshot.
//!
//! The window is a read-only consumer of #27's contract. It starts the project
//! interpreter on the loopback interface, shows what it is doing, and makes sure
//! it does not outlive the window that owns it.

mod paths;
mod service;

use tauri::{RunEvent, WindowEvent};

use service::Supervisor;

#[tauri::command]
fn service_status(supervisor: tauri::State<'_, Supervisor>) -> serde_json::Value {
    supervisor.snapshot()
}

#[tauri::command]
fn service_start(supervisor: tauri::State<'_, Supervisor>) -> serde_json::Value {
    supervisor.start();
    supervisor.snapshot()
}

#[tauri::command]
fn service_stop(supervisor: tauri::State<'_, Supervisor>) -> serde_json::Value {
    supervisor.stop();
    supervisor.snapshot()
}

fn main() {
    let supervisor = Supervisor::new();
    let starting = supervisor.clone();
    let exiting = supervisor.clone();

    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .manage(supervisor)
        .invoke_handler(tauri::generate_handler![
            service_status,
            service_start,
            service_stop
        ])
        // Starting the service spawns a thread and returns at once; the UI thread
        // is never blocked waiting for the child.
        .setup(move |app| {
            starting.attach(app.handle().clone());
            starting.start();
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("the tera shell could not build its window")
        .run(move |_handle, event| match event {
            RunEvent::WindowEvent {
                label,
                event: WindowEvent::CloseRequested { .. },
                ..
            } => {
                if label == "main" {
                    exiting.shutdown();
                }
            }
            RunEvent::ExitRequested { .. } => {
                exiting.shutdown();
            }
            _ => {}
        });
}
