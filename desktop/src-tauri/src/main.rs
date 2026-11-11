// Windows 下不弹控制台窗口；其余平台无影响。
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    self_engine_desktop_lib::run()
}
