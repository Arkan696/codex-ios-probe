#![recursion_limit = "256"]

use anyhow::{Context, Result, anyhow, ensure};
use codex_app_server::in_process::{self, InProcessClientHandle, InProcessStartArgs};
use codex_app_server_protocol::{ClientInfo, ClientRequest, FsReadFileParams, FsReadFileResponse, InitializeParams, RequestId, ThreadStartParams, ThreadStartResponse};
use codex_config::{LoaderOverrides, NoopThreadConfigLoader};
use codex_core::config::ConfigBuilder;
use codex_exec_server::EnvironmentManager;
use codex_feedback::CodexFeedback;
use codex_protocol::{ThreadId, protocol::SessionSource};
use std::ffi::{CStr, c_char};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;
use tokio::time::timeout;

const STACK: usize = 8 * 1024 * 1024;
const DEADLINE: Duration = Duration::from_secs(60);

async fn start(home: &Path) -> Result<InProcessClientHandle> {
    let loader_overrides = LoaderOverrides {
        ignore_project_config: true,
        ignore_user_config: true,
        ..Default::default()
    };
    let cli_overrides = vec![
        ("cli_auth_credentials_store".into(), toml::Value::from("ephemeral")),
        ("analytics.enabled".into(), false.into()),
        ("features.plugins".into(), false.into()),
    ];
    let config = ConfigBuilder::default()
        .codex_home(home.to_owned())
        .fallback_cwd(Some(home.to_owned()))
        .loader_overrides(loader_overrides.clone())
        .cli_overrides(cli_overrides.clone())
        .build().await.context("load probe config")?;
    let environment_manager = Arc::new(EnvironmentManager::from_codex_home(home, None, config.http_client_factory()).await.context("configure local filesystem")?);
    let args = InProcessStartArgs {
        arg0_paths: Default::default(),
        config: Arc::new(config),
        cli_overrides,
        loader_overrides,
        strict_config: false,
        cloud_config_bundle: Default::default(),
        embedded_network_policy: Default::default(),
        thread_config_loader: Arc::new(NoopThreadConfigLoader),
        feedback: CodexFeedback::new(),
        log_db: None,
        state_db: None,
        environment_manager,
        config_warnings: vec![],
        session_source: SessionSource::Cli,
        enable_codex_api_key_env: false,
        initialize: InitializeParams {
            client_info: ClientInfo {
                name: "ios-probe-bridge".into(),
                title: None,
                version: env!("CARGO_PKG_VERSION").into(),
            },
            capabilities: None,
        },
        channel_capacity: in_process::DEFAULT_IN_PROCESS_CHANNEL_CAPACITY,
    };
    Ok(Box::pin(in_process::start(args)).await.context("initialize embedded app-server")?)
}

async fn probe(home: &Path) -> Result<String> {
    let client = timeout(DEADLINE, start(home)).await.context("app-server startup timed out")??;
    let request = ClientRequest::ThreadStart {
        request_id: RequestId::Integer(1), // initialize uses 0
        params: ThreadStartParams {
            cwd: Some(home.display().to_string()),
            ephemeral: Some(true),
            ..Default::default()
        },
    };
    let result: Result<String> = async {
        let value = timeout(DEADLINE, client.request(request)).await.context("thread/start timed out")?
            .context("thread/start transport")?.map_err(|e| anyhow!("thread/start RPC {}: {}", e.code, e.message))?;
        let response: ThreadStartResponse = serde_json::from_value(value)?;
        let id = ThreadId::from_string(&response.thread.id)?;
        // Hard-coded relative name under the fresh test container; no caller-supplied path.
        let params = FsReadFileParams { path: home.join("challenge.bin").try_into()? };
        let request = ClientRequest::FsReadFile { request_id: RequestId::Integer(2), params };
        let value = timeout(DEADLINE, client.request(request)).await.context("fs/readFile timed out")?
            .context("fs/readFile transport")?.map_err(|e| anyhow!("fs/readFile RPC {}: {}", e.code, e.message))?;
        let read: FsReadFileResponse = serde_json::from_value(value)?;
        Ok(serde_json::json!({"uuid":id.to_string(),"base64":read.data_base64}).to_string())
    }.await;
    let shutdown = client.shutdown().await;
    let reply = result?;
    shutdown.context("shut down embedded app-server")?;
    Ok(reply)
}

fn run(home: PathBuf) -> Result<String> {
    ensure!(home.is_absolute(), "home must be an absolute directory path");
    std::fs::create_dir_all(&home).context("create probe home")?;
    // Do not nest block_on in the caller's runtime or poll Codex startup on an iOS UI stack.
    std::thread::Builder::new().name("codex-probe".into()).stack_size(STACK).spawn(move || {
        let rt = tokio::runtime::Builder::new_multi_thread().worker_threads(2)
            .thread_stack_size(STACK).enable_all().build().context("create Tokio runtime")?;
        let result = rt.block_on(Box::pin(probe(&home)));
        rt.shutdown_timeout(Duration::from_secs(2));
        result
    }).context("spawn probe worker")?.join().map_err(|_| anyhow!("probe worker panicked"))?
}

/// Blocks until thread/start and server shutdown complete. Returns 0 for a JSON reply, 1 for an error.
///
/// # Safety
/// `home` must be null or a readable NUL-terminated UTF-8 string. `out` must be null or
/// writable for `cap` bytes, exclusive of other accesses. Use a fresh app-writable home.
/// Use a 4096-byte output buffer. Diagnostics may be truncated to fit.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn codex_probe(home: *const c_char, out: *mut c_char, cap: usize) -> i32 {
    if out.is_null() || cap == 0 { return 1; }
    let result = std::panic::catch_unwind(|| -> Result<String> {
        ensure!(cap >= 4096, "need 4096 output bytes");
        ensure!(!home.is_null(), "home is null");
        let path = unsafe { CStr::from_ptr(home) }.to_str().context("home is not UTF-8")?;
        run(PathBuf::from(path))
    });
    let (status, text) = match result {
        Ok(Ok(reply)) if reply.len() < cap => (0, reply),
        Ok(Ok(_)) => (1, "reply exceeds buffer".into()),
        Ok(Err(error)) => (1, format!("{error:#}")),
        Err(_) => (1, "panic in codex_probe".to_owned()),
    };
    let len = text.floor_char_boundary(text.len().min(cap - 1));
    unsafe {
        std::ptr::copy_nonoverlapping(text.as_ptr(), out.cast::<u8>(), len);
        out.add(len).write(0);
    }
    status
}
