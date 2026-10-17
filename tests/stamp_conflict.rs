use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

static TEMP_SEQUENCE: AtomicU64 = AtomicU64::new(0);

fn write(path: &Path, body: &str) {
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    fs::write(path, body).unwrap();
}

fn temp_root() -> PathBuf {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let sequence = TEMP_SEQUENCE.fetch_add(1, Ordering::Relaxed);
    std::env::temp_dir().join(format!(
        "cam-stamp-conflict-{}-{nonce}-{sequence}",
        std::process::id()
    ))
}

#[test]
fn same_card_name_uses_each_documents_session_id() {
    let root = temp_root();
    let home = root.join("home");
    let state = root.join("state");
    let repo = std::env::current_dir().unwrap();
    let support = home.join("Library/Application Support/Claude/claude-code-sessions");
    let card_name = "local_aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa.json";
    let first_id = "11111111-1111-4111-8111-111111111111";
    let second_id = "22222222-2222-4222-8222-222222222222";
    let first = support
        .join("aaaaaaaa-0000-4000-8000-000000000001/aaaaaaaa-0000-4000-8000-000000000002")
        .join(card_name);
    let second = support
        .join("bbbbbbbb-0000-4000-8000-000000000001/bbbbbbbb-0000-4000-8000-000000000002")
        .join(card_name);

    write(
        &first,
        &format!(
            r#"{{"cliSessionId":"{first_id}","lastActivityAt":1,"completedTurns":1,"title":"First"}}"#
        ),
    );
    write(
        &second,
        &format!(
            r#"{{"cliSessionId":"{second_id}","lastActivityAt":1,"completedTurns":1,"title":"Second"}}"#
        ),
    );
    let project = home.join(".claude/projects/project");
    write(
        &project.join(format!("{first_id}.jsonl")),
        "{\"type\":\"assistant\",\"timestamp\":\"2024-01-01T00:00:00.000Z\",\"message\":{\"content\":\"first\"}}\n",
    );
    write(
        &project.join(format!("{second_id}.jsonl")),
        "{\"type\":\"assistant\",\"timestamp\":\"2024-01-02T00:00:00.000Z\",\"message\":{\"content\":\"second\"}}\n",
    );

    let result = Command::new("python3")
        .arg(repo.join("claude-account-move"))
        .args(["stamp", "--apply", "--json", "--home"])
        .arg(&home)
        .arg("--state-dir")
        .arg(&state)
        .env("HOME", &home)
        .env("TZ", "UTC")
        .env(
            "CLAUDE_ACCOUNT_MOVE_PGREP",
            repo.join("tests/stub_pgrep_closed.sh"),
        )
        .output()
        .unwrap();

    assert!(
        result.status.success(),
        "stdout={} stderr={}",
        String::from_utf8_lossy(&result.stdout),
        String::from_utf8_lossy(&result.stderr)
    );
    let first_after = fs::read_to_string(&first).unwrap();
    let second_after = fs::read_to_string(&second).unwrap();
    assert!(first_after.contains(first_id));
    assert!(second_after.contains(second_id));
    assert!(first_after.contains("First · 01.01 00:00"));
    assert!(second_after.contains("Second · 02.01 00:00"));

    fs::remove_dir_all(&root).unwrap();
}
