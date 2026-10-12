// Prevents additional console window on Windows in release, DO NOT REMOVE!!
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    // 提权停止服务：桌面端用 `runas` 重新拉起自己并带上 `--stop-pid=<n>`，
    // 这个实例只结束目标进程就退出——不开窗口、不建托盘，避免和已在跑的实例打架。
    if let Some(pid) = wb_switch_rust_lib::elevated_stop_target(std::env::args()) {
        std::process::exit(if wb_switch_rust_lib::terminate_pid(pid) { 0 } else { 1 });
    }
    wb_switch_rust_lib::run()
}
