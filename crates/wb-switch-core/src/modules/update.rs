//! 自动更新：检查公开 GitHub Releases 版本 + 更新源配置。
//!
//! 对照 server.py `load_github_config` / `save_github_config` /
//! `compare_versions` / `update_check`。下载安装走 tauri-plugin-updater（整包更新）。
//!
//! 版本检查不走 GitHub API（避免 60 次/小时/IP 限流）：
//! 1. 主端点：release 资产的 updater manifest（下载不计 API 配额）；
//! 2. 兜底端点：`/releases/latest` 的 302 `Location` 头解析 tag；
//! 3. 成功结果进程级缓存 6 小时，缓存命中不发网络请求。

use serde_json::{json, Value};
use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Mutex;

use crate::modules::config::{
    atomic_write, http_request_raw, http_request_with_proxy, now_secs, store_dir,
};

/// 应用当前版本（来自 Cargo.toml package.version）。
pub const APP_VERSION: &str = env!("CARGO_PKG_VERSION");
/// 二改后的版本检查指向自己的仓库。
pub const GITHUB_OWNER: &str = "Hyact";
pub const GITHUB_REPO: &str = "workbuddy-switch-plus";

/// 成功结果缓存有效期（6 小时）。自动轮询（默认 30 分钟）命中缓存，不发网络请求；
/// 设置页手动检查传 force=true 绕过缓存强制刷新。
const CACHE_TTL_SECS: i64 = 6 * 60 * 60;

/// 自动检查间隔的可选范围（分钟）。
///
/// 下限 5 分钟：再密也不会更快——检查命中 6 小时缓存，实际只在缓存过期后
/// 才真的发请求，间隔小于缓存时长纯粹是白跑一轮循环。
/// 上限 24 小时：这是「自动检查」而不是「关闭」，想关请用 auto_check=false。
pub const MIN_INTERVAL_MINUTES: i64 = 5;
pub const MAX_INTERVAL_MINUTES: i64 = 24 * 60;
pub const DEFAULT_INTERVAL_MINUTES: i64 = 30;

/// 归一化自动检查间隔：非数字 / 越界一律钳到合法区间。
///
/// 钳而不是报错：这个值来自一个下拉框，用户能选的都在范围内；钳制是为了兜住
/// 「手工改过配置文件」这种输入，让应用照常启动而不是卡在配置解析上。
pub fn norm_interval_minutes(raw: Option<&Value>) -> i64 {
    let value = raw
        .and_then(|v| v.as_i64().or_else(|| v.as_f64().map(|f| f as i64)))
        .unwrap_or(DEFAULT_INTERVAL_MINUTES);
    value.clamp(MIN_INTERVAL_MINUTES, MAX_INTERVAL_MINUTES)
}

/// 进程级内存缓存，只缓存 ok=true 的结果；失败不写缓存。
struct CachedCheck {
    checked_at: i64,
    value: Value,
}

static CACHE: Mutex<Option<CachedCheck>> = Mutex::new(None);

pub fn github_config_file() -> PathBuf {
    store_dir().join("github_config.json")
}

/// 读取更新源配置（兼容旧配置文件，但永不返回 token）。
///
/// 除 owner/repo/proxy 外还带三个自动更新字段（设置页「自动更新」区块）：
///   auto_check            是否后台自动检查（默认 true，与旧版本行为一致）
///   check_interval_minutes 自动检查间隔，钳在 [5, 1440]
///   auto_download         发现新版本后是否自动下载（默认 false）
///
/// 三者都有默认值，因此旧配置文件（只有 owner/repo/proxy）读出来照样完整，
/// 不需要迁移。
pub fn load_github_config() -> Value {
    let mut owner = GITHUB_OWNER.to_string();
    let mut repo = GITHUB_REPO.to_string();
    let mut proxy = String::new();
    let mut auto_check = true;
    let mut auto_download = false;
    let mut interval = DEFAULT_INTERVAL_MINUTES;
    let f = github_config_file();
    let mut should_normalize = false;
    if f.exists() {
        if let Ok(text) = std::fs::read_to_string(&f) {
            if let Ok(v) = serde_json::from_str::<Value>(&text) {
                if let Some(value) = v.get("owner").and_then(|v| v.as_str()) {
                    if !value.trim().is_empty() {
                        owner = value.to_string();
                    }
                }
                if let Some(value) = v.get("repo").and_then(|v| v.as_str()) {
                    if !value.trim().is_empty() {
                        repo = value.to_string();
                    }
                }
                if let Some(value) = v.get("proxy").and_then(|v| v.as_str()) {
                    proxy = value.trim().to_string();
                }
                if let Some(value) = v.get("auto_check").and_then(|v| v.as_bool()) {
                    auto_check = value;
                }
                if let Some(value) = v.get("auto_download").and_then(|v| v.as_bool()) {
                    auto_download = value;
                }
                let normalized_interval = norm_interval_minutes(v.get("check_interval_minutes"));
                // 旧配置文件没有这个键（None != Some(30)），或写了越界值时会命中这里，
                // 于是第一次读取就把新字段补写进文件——这正是我们要的：读一次之后
                // 文件里就有完整的六项，用户去手改也能看到该写什么。
                if v.get("check_interval_minutes").and_then(|v| v.as_i64()) != Some(normalized_interval) {
                    should_normalize = true;
                }
                interval = normalized_interval;
                should_normalize = should_normalize || v.get("token").is_some();
            }
        }
    }
    // 二改前存下来的配置里 owner 还是上游的 changexbc（repo 可能是 wb-switch 或
    // workbuddy-switch），它会覆盖上面的新默认值，版本检查就还打在上游仓库上。
    // 所以只要 owner 还是 changexbc 就一并归正；用户自己填的其它 owner 不动。
    if owner == "changexbc" {
        owner = GITHUB_OWNER.to_string();
        repo = GITHUB_REPO.to_string();
        should_normalize = true;
    }
    let normalized = json!({
        "owner": owner,
        "repo": repo,
        "proxy": proxy,
        "auto_check": auto_check,
        "check_interval_minutes": interval,
        "auto_download": auto_download,
    });
    if should_normalize {
        let _ = atomic_write(
            &f,
            &serde_json::to_string_pretty(&normalized).unwrap_or_default(),
        );
    }
    normalized
}

/// 保存更新源配置；公开仓库不需要也不保存 GitHub token。
pub fn save_github_config(cfg: &Value) -> std::io::Result<()> {
    let owner = cfg
        .get("owner")
        .and_then(|v| v.as_str())
        .filter(|v| !v.trim().is_empty())
        .unwrap_or(GITHUB_OWNER);
    let repo = cfg
        .get("repo")
        .and_then(|v| v.as_str())
        .filter(|v| !v.trim().is_empty())
        .unwrap_or(GITHUB_REPO);
    let proxy = cfg
        .get("proxy")
        .and_then(|v| v.as_str())
        .map(str::trim)
        .unwrap_or("");
    // 这三个字段在保存路径上**必须回落到当前已存的值**，不能用默认值：
    // 设置页改代理时只提交 owner/repo/proxy（前端那次调用不带自动更新字段），
    // 若这里用默认值覆盖，用户刚设好的「自动下载」会被一次保存代理悄悄关掉。
    let previous = load_github_config();
    let auto_check = cfg
        .get("auto_check")
        .and_then(|v| v.as_bool())
        .or_else(|| previous.get("auto_check").and_then(|v| v.as_bool()))
        .unwrap_or(true);
    let auto_download = cfg
        .get("auto_download")
        .and_then(|v| v.as_bool())
        .or_else(|| previous.get("auto_download").and_then(|v| v.as_bool()))
        .unwrap_or(false);
    let interval = if cfg.get("check_interval_minutes").is_some() {
        norm_interval_minutes(cfg.get("check_interval_minutes"))
    } else {
        norm_interval_minutes(previous.get("check_interval_minutes"))
    };
    let clean = json!({
        "owner": owner,
        "repo": repo,
        "proxy": proxy,
        "auto_check": auto_check,
        "check_interval_minutes": interval,
        "auto_download": auto_download,
    });
    std::fs::create_dir_all(store_dir())?;
    atomic_write(
        &github_config_file(),
        &serde_json::to_string_pretty(&clean).unwrap_or_default(),
    )
}

fn version_tuple(v: &str) -> Vec<i64> {
    v.trim_start_matches('v')
        .split('.')
        .filter_map(|x| x.parse::<i64>().ok())
        .collect()
}

/// 版本比较：a > b 返回 1，a < b 返回 -1，相等返回 0。
pub fn compare_versions(a: &str, b: &str) -> i64 {
    let ta = version_tuple(a);
    let tb = version_tuple(b);
    for i in 0..ta.len().max(tb.len()) {
        let x = ta.get(i).copied().unwrap_or(0);
        let y = tb.get(i).copied().unwrap_or(0);
        if x != y {
            return if x > y { 1 } else { -1 };
        }
    }
    0
}

/// updater manifest 候选 URL（按优先级）。
///
/// 1. 合并后的 `latest.json`（含各平台）；
/// 2. 当前系统的 `latest-<os>-<arch>.json`；
/// 3. 兼容旧 Windows 安装包：它们仍请求 `latest-macos-<arch>.json`。
pub fn updater_manifest_urls(owner: &str, repo: &str, os: &str, arch: &str) -> Vec<String> {
    let os_slug = match os {
        "macos" | "darwin" => "macos",
        other => other,
    };
    let mut urls = vec![format!(
        "https://github.com/{owner}/{repo}/releases/latest/download/latest.json"
    )];
    urls.push(format!(
        "https://github.com/{owner}/{repo}/releases/latest/download/latest-{os_slug}-{arch}.json"
    ));
    if os_slug != "macos" {
        urls.push(format!(
            "https://github.com/{owner}/{repo}/releases/latest/download/latest-macos-{arch}.json"
        ));
    }
    urls.dedup();
    urls
}

/// 主端点：拉取 updater manifest。成功返回解析后的 JSON（含 version / pub_date），
/// 失败返回可读错误信息。
async fn fetch_manifest_version(
    owner: &str,
    repo: &str,
    proxy: Option<&str>,
) -> Result<Value, String> {
    let mut headers = HashMap::new();
    headers.insert("Accept".to_string(), "application/json".to_string());
    headers.insert("User-Agent".to_string(), "wb-switch-plus".to_string());
    let mut last_err = "更新清单解析失败".to_string();
    for url in updater_manifest_urls(owner, repo, std::env::consts::OS, std::env::consts::ARCH) {
        let resp = http_request_with_proxy(&url, "GET", None, Some(&headers), proxy).await;
        let version = resp.get("version").and_then(|v| v.as_str()).unwrap_or("");
        if !version.trim().is_empty() {
            return Ok(resp);
        }
        last_err = resp
            .get("message")
            .and_then(|v| v.as_str())
            .unwrap_or("更新清单解析失败")
            .to_string();
    }
    Err(last_err)
}

/// 兜底端点：请求 `/releases/latest`，读 302 `Location` 头（形如
/// `.../releases/tag/v0.1.13`）解析 tag。不跟随重定向，避免拉到 HTML 页面。
/// 成功返回 tag，失败返回可读错误 + code（状态码或 -1）。
async fn fetch_latest_tag(
    owner: &str,
    repo: &str,
    proxy: Option<&str>,
) -> Result<String, (String, i64)> {
    let url = format!("https://github.com/{owner}/{repo}/releases/latest");
    let mut headers = HashMap::new();
    headers.insert("Accept".to_string(), "text/html".to_string());
    headers.insert("User-Agent".to_string(), "wb-switch-plus".to_string());
    let (status, resp_headers, body) =
        http_request_raw(&url, "GET", None, Some(&headers), proxy, false).await;

    if status == 0 {
        let msg = if body.trim().is_empty() {
            "网络请求失败".to_string()
        } else {
            body
        };
        return Err((msg, -1));
    }
    if status == 404 {
        // 无正式 release 或仓库不存在。
        return Err(("未找到可用的发布版本".to_string(), 404));
    }
    let location = resp_headers
        .iter()
        .find(|(k, _)| k.eq_ignore_ascii_case("location"))
        .map(|(_, v)| v.clone());
    let location = match location {
        Some(location) => location,
        None => {
            return Err((
                format!("无法获取发布页跳转地址（HTTP {status}）"),
                status as i64,
            ))
        }
    };
    let tag = location.rsplit('/').next().unwrap_or("").trim().to_string();
    if tag.is_empty() || !location.contains("/releases/tag/") {
        return Err(("无法解析发布版本标签".to_string(), -1));
    }
    Ok(tag)
}

/// 查询最新 Release，与本地版本对比。对照 server.py `update_check`。
///
/// `force=true` 绕过缓存强制刷新（设置页手动检查）；否则 6 小时内成功结果直接返回，
/// 不发网络请求。主端点 manifest 失败时自动走 302 兜底；两个端点都失败返回可读错误。
pub async fn update_check(proxy: Option<&str>, force: bool) -> Value {
    if !force {
        if let Some(cached) = CACHE.lock().unwrap().as_ref() {
            if now_secs() - cached.checked_at < CACHE_TTL_SECS {
                return cached.value.clone();
            }
        }
    }

    let cfg = load_github_config();
    let owner = cfg
        .get("owner")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    let repo = cfg
        .get("repo")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    let configured_proxy = cfg
        .get("proxy")
        .and_then(|v| v.as_str())
        .map(str::trim)
        .filter(|v| !v.is_empty());
    let proxy = proxy.or(configured_proxy);
    let release_url = format!("https://github.com/{owner}/{repo}/releases/latest");
    let current = APP_VERSION.to_string();

    // 主端点：updater manifest（release 资产下载，不计 GitHub API 配额）。
    if let Ok(manifest) = fetch_manifest_version(&owner, &repo, proxy).await {
        let version = manifest
            .get("version")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .trim();
        let latest = version.strip_prefix('v').unwrap_or(version).to_string();
        let tag = format!("v{latest}");
        let release_name = manifest
            .get("name")
            .and_then(|v| v.as_str())
            .unwrap_or(&tag)
            .to_string();
        let published_at = manifest
            .get("pub_date")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string());
        let value = json!({
            "ok": true,
            "current": current,
            "latest": latest,
            "latestTag": tag,
            "hasUpdate": compare_versions(&latest, &current) > 0,
            "releaseName": release_name,
            "releaseUrl": release_url,
            "publishedAt": published_at,
            "checkedAt": now_secs(),
        });
        *CACHE.lock().unwrap() = Some(CachedCheck {
            checked_at: now_secs(),
            value: value.clone(),
        });
        return value;
    }

    // 兜底端点：`/releases/latest` 的 302 Location 头解析 tag（仅 manifest 失败时）。
    match fetch_latest_tag(&owner, &repo, proxy).await {
        Ok(tag) => {
            let latest = tag.strip_prefix('v').unwrap_or(&tag).to_string();
            let value = json!({
                "ok": true,
                "current": current,
                "latest": latest,
                "latestTag": tag,
                "hasUpdate": compare_versions(&latest, &current) > 0,
                "releaseName": tag.clone(),
                "releaseUrl": release_url,
                "checkedAt": now_secs(),
            });
            *CACHE.lock().unwrap() = Some(CachedCheck {
                checked_at: now_secs(),
                value: value.clone(),
            });
            value
        }
        Err((msg, code)) => json!({
            "ok": false,
            "error": msg,
            "message": format!("{msg}（code={code}）"),
            "releaseUrl": release_url,
        }),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn updater_manifest_urls_macos_skips_duplicate_fallback() {
        let urls = updater_manifest_urls("Hyact", "workbuddy-switch-plus", "macos", "aarch64");
        assert_eq!(
            urls,
            vec![
                "https://github.com/Hyact/workbuddy-switch-plus/releases/latest/download/latest.json",
                "https://github.com/Hyact/workbuddy-switch-plus/releases/latest/download/latest-macos-aarch64.json",
            ]
        );
    }

    #[test]
    fn updater_manifest_urls_windows_keeps_macos_compat() {
        let urls = updater_manifest_urls("Hyact", "workbuddy-switch-plus", "windows", "x86_64");
        assert_eq!(
            urls,
            vec![
                "https://github.com/Hyact/workbuddy-switch-plus/releases/latest/download/latest.json",
                "https://github.com/Hyact/workbuddy-switch-plus/releases/latest/download/latest-windows-x86_64.json",
                "https://github.com/Hyact/workbuddy-switch-plus/releases/latest/download/latest-macos-x86_64.json",
            ]
        );
    }

    #[test]
    fn compare_versions_orders_semver() {
        assert_eq!(compare_versions("0.1.18", "0.1.18"), 0);
        assert_eq!(compare_versions("0.1.19", "0.1.18"), 1);
        assert_eq!(compare_versions("v0.1.17", "0.1.18"), -1);
    }

    #[test]
    fn interval_is_clamped_into_range() {
        assert_eq!(norm_interval_minutes(None), DEFAULT_INTERVAL_MINUTES);
        assert_eq!(norm_interval_minutes(Some(&json!(1))), MIN_INTERVAL_MINUTES);
        assert_eq!(
            norm_interval_minutes(Some(&json!(99999))),
            MAX_INTERVAL_MINUTES
        );
        assert_eq!(norm_interval_minutes(Some(&json!(60))), 60);
        // 字符串 / null 这类脏值退回默认值，而不是把应用卡在解析上
        assert_eq!(
            norm_interval_minutes(Some(&json!("abc"))),
            DEFAULT_INTERVAL_MINUTES
        );
        assert_eq!(
            norm_interval_minutes(Some(&Value::Null)),
            DEFAULT_INTERVAL_MINUTES
        );
    }
}
