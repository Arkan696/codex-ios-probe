#![recursion_limit = "256"]

use anyhow::{Context, Result, anyhow, ensure};
use codex_app_server::in_process::{self, InProcessClientHandle, InProcessStartArgs};
use codex_app_server_protocol::{ClientInfo, ClientRequest, InitializeParams, RequestId, ThreadStartParams, ThreadStartResponse};
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
    let environment_manager = Arc::new(EnvironmentManager::without_environments(config.http_client_factory()));
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

async fn probe(home: &Path) -> Result<ThreadId> {
    let client = timeout(DEADLINE, start(home)).await.context("app-server startup timed out")??;
    let request = ClientRequest::ThreadStart {
        request_id: RequestId::Integer(1), // initialize uses 0
        params: ThreadStartParams {
            cwd: Some(home.display().to_string()),
            ephemeral: Some(true),
            ..Default::default()
        },
    };
    let reply = timeout(DEADLINE, client.request(request)).await;
    // Always attempt graceful shutdown, including on timeout or RPC error.
    let shutdown = client.shutdown().await;
    let value = reply.context("thread/start timed out")?.context("thread/start transport")?
        .map_err(|err| anyhow!("thread/start RPC {}: {}", err.code, err.message))?;
    let response: ThreadStartResponse = serde_json::from_value(value).context("decode thread/start response")?;
    let id = ThreadId::from_string(&response.thread.id).context("invalid thread/start UUID")?;
    shutdown.context("shut down embedded app-server")?;
    Ok(id)
}

fn run(home: PathBuf) -> Result<ThreadId> {
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

/// Blocks until thread/start and server shutdown complete. Returns 0 for a UUID, 1 for an error.
///
/// # Safety
/// `home` must be null or a readable NUL-terminated UTF-8 string. `out` must be null or
/// writable for `cap` bytes, exclusive of other accesses. Use a fresh app-writable home.
/// Success requires at least 37 output bytes. Diagnostics may be truncated to fit.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn codex_probe(home: *const c_char, out: *mut c_char, cap: usize) -> i32 {
    if out.is_null() || cap == 0 { return 1; }
    let result = std::panic::catch_unwind(|| -> Result<ThreadId> {
        ensure!(cap >= 37, "need 37 output bytes");
        ensure!(!home.is_null(), "home is null");
        let path = unsafe { CStr::from_ptr(home) }.to_str().context("home is not UTF-8")?;
        run(PathBuf::from(path))
    });
    let (status, text) = match result {
        Ok(Ok(id)) => (0, id.to_string()),
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
