// Oracle for 0.1.4: `inventory` and the clean public defaults.
// Black box through the CLI and the launcher; written by the judge, not by the
// implementer. Run from the repository root:
//   rustc --edition 2021 --test tests/inventory_oracle.rs -o .selftest/inventory_oracle
//   .selftest/inventory_oracle --test-threads 1
use std::collections::BTreeMap;
use std::fs;
use std::os::unix::fs::symlink;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

static SEQ: AtomicU64 = AtomicU64::new(0);

const CC: &str = "claude-code-sessions";
const LAM: &str = "local-agent-mode-sessions";
const ACC_A: &str = "aaaaaaaa-0000-4000-8000-000000000001";
const ORG_A: &str = "aaaaaaaa-0000-4000-8000-0000000000f1";
const ACC_B: &str = "bbbbbbbb-0000-4000-8000-000000000001";
const ORG_B: &str = "bbbbbbbb-0000-4000-8000-0000000000f1";
const ACC_C: &str = "cccccccc-0000-4000-8000-000000000001";
// 1790000000000 ms = 2026-09-21T14:13:20Z
const T_NEW: u64 = 1_790_000_000_000;
const T_OLD: u64 = 1_780_000_000_000;
const SECRET_TITLE: &str = "SECRET-TITLE-do-not-print";

// ---------- minimal JSON ----------
#[derive(Debug, Clone, PartialEq)]
enum J {
    Null,
    Bool(bool),
    Num(f64),
    Str(String),
    Arr(Vec<J>),
    Obj(BTreeMap<String, J>),
}

impl J {
    fn get(&self, k: &str) -> &J {
        match self {
            J::Obj(m) => m.get(k).unwrap_or_else(|| panic!("missing key {k} in {self:?}")),
            _ => panic!("not an object when reading {k}: {self:?}"),
        }
    }
    fn has(&self, k: &str) -> bool {
        matches!(self, J::Obj(m) if m.contains_key(k))
    }
    fn arr(&self) -> &Vec<J> {
        match self {
            J::Arr(v) => v,
            _ => panic!("not an array: {self:?}"),
        }
    }
    fn n(&self) -> i64 {
        match self {
            J::Num(x) => *x as i64,
            _ => panic!("not a number: {self:?}"),
        }
    }
    fn s(&self) -> &str {
        match self {
            J::Str(x) => x,
            _ => panic!("not a string: {self:?}"),
        }
    }
    fn b(&self) -> bool {
        match self {
            J::Bool(x) => *x,
            _ => panic!("not a bool: {self:?}"),
        }
    }
}

struct P<'a> {
    s: &'a [u8],
    i: usize,
}

impl<'a> P<'a> {
    fn ws(&mut self) {
        while self.i < self.s.len() && (self.s[self.i] as char).is_ascii_whitespace() {
            self.i += 1;
        }
    }
    fn lit(&mut self, w: &str) -> bool {
        if self.s[self.i..].starts_with(w.as_bytes()) {
            self.i += w.len();
            true
        } else {
            false
        }
    }
    fn val(&mut self) -> J {
        self.ws();
        let c = self.s[self.i];
        match c {
            b'{' => {
                self.i += 1;
                let mut m = BTreeMap::new();
                self.ws();
                if self.s[self.i] == b'}' {
                    self.i += 1;
                    return J::Obj(m);
                }
                loop {
                    self.ws();
                    let k = match self.val() {
                        J::Str(k) => k,
                        o => panic!("bad key {o:?}"),
                    };
                    self.ws();
                    assert_eq!(self.s[self.i], b':');
                    self.i += 1;
                    let v = self.val();
                    m.insert(k, v);
                    self.ws();
                    match self.s[self.i] {
                        b',' => self.i += 1,
                        b'}' => {
                            self.i += 1;
                            return J::Obj(m);
                        }
                        x => panic!("bad object char {}", x as char),
                    }
                }
            }
            b'[' => {
                self.i += 1;
                let mut v = Vec::new();
                self.ws();
                if self.s[self.i] == b']' {
                    self.i += 1;
                    return J::Arr(v);
                }
                loop {
                    v.push(self.val());
                    self.ws();
                    match self.s[self.i] {
                        b',' => self.i += 1,
                        b']' => {
                            self.i += 1;
                            return J::Arr(v);
                        }
                        x => panic!("bad array char {}", x as char),
                    }
                }
            }
            b'"' => {
                self.i += 1;
                let mut out = String::new();
                loop {
                    let c = self.s[self.i];
                    self.i += 1;
                    match c {
                        b'"' => return J::Str(out),
                        b'\\' => {
                            let e = self.s[self.i];
                            self.i += 1;
                            match e {
                                b'n' => out.push('\n'),
                                b't' => out.push('\t'),
                                b'r' => out.push('\r'),
                                b'b' => out.push('\u{8}'),
                                b'f' => out.push('\u{c}'),
                                b'u' => {
                                    let h = std::str::from_utf8(&self.s[self.i..self.i + 4]).unwrap();
                                    self.i += 4;
                                    let cp = u32::from_str_radix(h, 16).unwrap();
                                    out.push(char::from_u32(cp).unwrap_or('?'));
                                }
                                o => out.push(o as char),
                            }
                        }
                        _ => {
                            // copy one UTF-8 sequence
                            let start = self.i - 1;
                            let len = if c < 0x80 { 1 } else if c >> 5 == 0b110 { 2 } else if c >> 4 == 0b1110 { 3 } else { 4 };
                            self.i = start + len;
                            out.push_str(std::str::from_utf8(&self.s[start..self.i]).unwrap());
                        }
                    }
                }
            }
            b't' if self.lit("true") => J::Bool(true),
            b'f' if self.lit("false") => J::Bool(false),
            b'n' if self.lit("null") => J::Null,
            _ => {
                let st = self.i;
                while self.i < self.s.len() && b"+-0123456789.eE".contains(&self.s[self.i]) {
                    self.i += 1;
                }
                let t = std::str::from_utf8(&self.s[st..self.i]).unwrap();
                J::Num(t.parse().unwrap_or_else(|_| panic!("bad number {t:?}")))
            }
        }
    }
}

fn parse(text: &str) -> J {
    let mut p = P { s: text.trim().as_bytes(), i: 0 };
    let v = p.val();
    p.ws();
    assert_eq!(p.i, p.s.len(), "trailing data after JSON: {text}");
    v
}

// ---------- synthetic home ----------
struct Home {
    root: PathBuf,
    home: PathBuf,
    state: PathBuf,
}

impl Drop for Home {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.root);
    }
}

fn repo() -> PathBuf {
    std::env::current_dir().unwrap()
}

fn new_home() -> Home {
    let nonce = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos();
    let root = std::env::temp_dir().join(format!(
        "cam-inventory-{}-{nonce}-{}",
        std::process::id(),
        SEQ.fetch_add(1, Ordering::Relaxed)
    ));
    let home = root.join("home");
    fs::create_dir_all(&home).unwrap();
    let state = root.join("state-must-not-appear");
    Home { root, home, state }
}

impl Home {
    fn support(&self) -> PathBuf {
        self.home.join("Library/Application Support/Claude")
    }
    fn pair(&self, store: &str, acc: &str, org: &str) -> PathBuf {
        let d = self.support().join(store).join(acc).join(org);
        fs::create_dir_all(&d).unwrap();
        d
    }
    fn card(&self, dir: &Path, n: u32, cid: &str, act: u64) {
        let name = format!("local_{:08x}-0000-4000-8000-{:012x}.json", n, 9);
        let body = format!(
            "{{\"cliSessionId\":\"{cid}\",\"lastActivityAt\":{act},\"completedTurns\":3,\"title\":\"{SECRET_TITLE}\"}}"
        );
        fs::write(dir.join(name), body).unwrap();
    }
    fn tomb(&self, dir: &Path, id: &str) {
        fs::write(dir.join(format!("deleted_{id}")), "").unwrap();
    }
    fn transcript(&self, sid: &str) {
        let d = self.home.join(".claude/projects/proj");
        fs::create_dir_all(&d).unwrap();
        fs::write(
            d.join(format!("{sid}.jsonl")),
            "{\"type\":\"user\",\"timestamp\":\"2026-09-21T14:13:20.000Z\",\"message\":{\"content\":\"hello\"}}\n",
        )
        .unwrap();
    }
    fn accounts(&self, cli: Option<&str>, app: Option<&str>) {
        fs::create_dir_all(self.support()).unwrap();
        if let Some(c) = cli {
            fs::write(
                self.home.join(".claude.json"),
                format!("{{\"oauthAccount\":{{\"accountUuid\":\"{c}\",\"organizationUuid\":\"{ORG_A}\"}}}}"),
            )
            .unwrap();
        }
        if let Some(a) = app {
            fs::write(self.support().join("config.json"), format!("{{\"lastKnownAccountUuid\":\"{a}\"}}")).unwrap();
        }
    }
    fn run(&self, args: &[&str]) -> Output {
        Command::new("python3")
            .arg(repo().join("claude-account-move"))
            .args(args)
            .env("HOME", &self.home)
            .env("CLAUDE_ACCOUNT_MOVE_HOME", &self.state)
            .env("PYTHONDONTWRITEBYTECODE", "1")
            .env("CLAUDE_ACCOUNT_MOVE_PGREP", repo().join("tests/stub_pgrep_open.sh"))
            .output()
            .unwrap()
    }
    fn inv(&self) -> (i32, J, String) {
        let o = self.run(&["inventory", "--json"]);
        let out = String::from_utf8_lossy(&o.stdout).to_string();
        let err = String::from_utf8_lossy(&o.stderr).to_string();
        let code = o.status.code().unwrap_or(-1);
        assert!(!out.trim().is_empty(), "no stdout, rc {code}, stderr: {err}");
        (code, parse(&out), out)
    }
}

/// Standard scene: account A (app + CLI) with one card; old account B with
/// four cards (two with a transcript, one without, one with no session id),
/// one deletion mark; one transcript no card names.
fn scene() -> Home {
    let h = new_home();
    h.accounts(Some(ACC_A), Some(ACC_A));
    let a = h.pair(CC, ACC_A, ORG_A);
    h.card(&a, 1, "11111111-0000-4000-8000-000000000001", T_NEW);
    h.transcript("11111111-0000-4000-8000-000000000001");
    let b = h.pair(CC, ACC_B, ORG_B);
    h.card(&b, 2, "22222222-0000-4000-8000-000000000001", T_OLD);
    h.card(&b, 3, "22222222-0000-4000-8000-000000000002", T_OLD - 1000);
    h.card(&b, 4, "22222222-0000-4000-8000-000000000003", T_OLD - 2000);
    h.card(&b, 5, "", T_OLD - 3000);
    h.tomb(&b, "22222222-0000-4000-8000-000000000009");
    h.transcript("22222222-0000-4000-8000-000000000001");
    h.transcript("22222222-0000-4000-8000-000000000002");
    h.transcript("99999999-0000-4000-8000-000000000001");
    h
}

fn account<'a>(doc: &'a J, id: &str) -> &'a J {
    doc.get("accounts")
        .arr()
        .iter()
        .find(|a| a.get("account").s() == id)
        .unwrap_or_else(|| panic!("account {id} not listed"))
}

fn folders_of<'a>(doc: &'a J, acc: &J) -> Vec<&'a J> {
    let all = doc.get("folders").arr();
    acc.get("folders").arr().iter().map(|i| &all[i.n() as usize]).collect()
}

fn digest(dir: &Path, out: &mut Vec<String>) {
    let mut names: Vec<_> = match fs::read_dir(dir) {
        Ok(r) => r.map(|e| e.unwrap().path()).collect(),
        Err(_) => return,
    };
    names.sort();
    for p in names {
        let md = fs::symlink_metadata(&p).unwrap();
        if md.file_type().is_symlink() {
            out.push(format!("L {} -> {}", p.display(), fs::read_link(&p).unwrap().display()));
        } else if md.is_dir() {
            out.push(format!("D {}", p.display()));
            digest(&p, out);
        } else {
            out.push(format!("F {} {:?} {:?}", p.display(), md.len(), fs::read(&p).unwrap()));
        }
    }
}

// ---------- inventory ----------
#[test]
fn counts_per_folder_and_account() {
    let h = scene();
    let (code, doc, _) = h.inv();
    assert_eq!(code, 0);
    assert_eq!(doc.get("command").s(), "inventory");
    assert_eq!(doc.get("exit").n(), 0);
    assert!(doc.get("read_complete").b());
    assert_eq!(doc.get("app_account").s(), ACC_A);
    assert_eq!(doc.get("cli_account").s(), ACC_A);

    let a = account(&doc, ACC_A);
    assert!(a.get("app").b() && a.get("cli").b());
    assert_eq!(a.get("cards").n(), 1);
    assert_eq!(a.get("with_transcript").n(), 1);

    let b = account(&doc, ACC_B);
    assert!(!b.get("app").b() && !b.get("cli").b());
    assert_eq!(b.get("cards").n(), 4);
    assert_eq!(b.get("with_transcript").n(), 2);
    assert!(b.get("last_activity").s().starts_with("2026-05-28T"), "{:?}", b.get("last_activity"));

    let fb = folders_of(&doc, b);
    assert_eq!(fb.len(), 1);
    let f = fb[0];
    assert_eq!(f.get("store").s(), CC);
    assert_eq!(f.get("cards").n(), 4);
    assert_eq!(f.get("deleted").n(), 1);
    assert_eq!(f.get("with_transcript").n(), 2);
    assert_eq!(f.get("without_transcript").n(), 1);
    assert_eq!(f.get("no_session_id").n(), 1);
    assert_eq!(f.get("scheduled_tasks").n(), 0);
    for f in doc.get("folders").arr() {
        assert_eq!(
            f.get("with_transcript").n() + f.get("without_transcript").n() + f.get("no_session_id").n(),
            f.get("cards").n()
        );
    }

    let t = doc.get("transcripts");
    assert_eq!(t.get("total").n(), 4);
    assert_eq!(t.get("with_card").n(), 3);
    assert_eq!(t.get("without_card").n(), 1);
}

#[test]
fn last_activity_is_newest_card_in_utc() {
    let h = scene();
    let (_, doc, _) = h.inv();
    let a = account(&doc, ACC_A).get("last_activity").s().to_string();
    assert!(a.starts_with("2026-09-21T14:13:20"), "{a}");
    assert!(a.ends_with('Z') || a.ends_with("+00:00"), "{a}");
}

#[test]
fn app_account_comes_first_then_newest() {
    let h = scene();
    // C is the app account and has no folder at all
    fs::write(h.support().join("config.json"), format!("{{\"lastKnownAccountUuid\":\"{ACC_C}\"}}")).unwrap();
    let (code, doc, _) = h.inv();
    assert_eq!(code, 0);
    let ids: Vec<&str> = doc.get("accounts").arr().iter().map(|a| a.get("account").s()).collect();
    assert_eq!(ids, vec![ACC_C, ACC_A, ACC_B]);
    let c = account(&doc, ACC_C);
    assert!(c.get("app").b());
    assert!(!c.get("cli").b());
    assert_eq!(c.get("cards").n(), 0);
    assert!(c.get("folders").arr().is_empty());
    assert_eq!(*c.get("last_activity"), J::Null);
}

#[test]
fn linked_folder_counted_once_and_shared() {
    let h = new_home();
    h.accounts(Some(ACC_A), Some(ACC_A));
    let a = h.pair(CC, ACC_A, ORG_A);
    h.card(&a, 1, "11111111-0000-4000-8000-000000000001", T_NEW);
    h.card(&a, 2, "11111111-0000-4000-8000-000000000002", T_NEW);
    let bdir = h.support().join(CC).join(ACC_B);
    fs::create_dir_all(&bdir).unwrap();
    symlink(&a, bdir.join(ORG_B)).unwrap();
    let (code, doc, _) = h.inv();
    assert_eq!(code, 0);
    let cc: Vec<&J> = doc.get("folders").arr().iter().filter(|f| f.get("store").s() == CC).collect();
    assert_eq!(cc.len(), 1, "one physical folder");
    assert_eq!(cc[0].get("cards").n(), 2);
    let kinds: Vec<(&str, &str)> = cc[0]
        .get("aliases")
        .arr()
        .iter()
        .map(|x| (x.get("account").s(), x.get("kind").s()))
        .collect();
    assert!(kinds.contains(&(ACC_A, "dir")), "{kinds:?}");
    assert!(kinds.contains(&(ACC_B, "link")), "{kinds:?}");
    assert_eq!(account(&doc, ACC_A).get("cards").n(), 2);
    assert_eq!(account(&doc, ACC_B).get("cards").n(), 2);
    assert_eq!(account(&doc, ACC_A).get("folders"), account(&doc, ACC_B).get("folders"));
}

#[test]
fn both_stores_are_listed_separately() {
    let h = scene();
    let l = h.pair(LAM, ACC_B, ORG_B);
    h.card(&l, 7, "77777777-0000-4000-8000-000000000001", T_OLD);
    let (code, doc, _) = h.inv();
    assert_eq!(code, 0);
    let b = account(&doc, ACC_B);
    let stores: Vec<&str> = folders_of(&doc, b).iter().map(|f| f.get("store").s()).collect();
    assert!(stores.contains(&CC) && stores.contains(&LAM), "{stores:?}");
    assert_eq!(b.get("cards").n(), 5);
    assert!(b.get("with_transcript").n() <= b.get("cards").n());
}

#[test]
fn scheduled_tasks_are_counted() {
    let h = scene();
    let b = h.support().join(CC).join(ACC_B).join(ORG_B);
    fs::write(
        b.join("scheduled-tasks.json"),
        "{\"scheduledTasks\":[{\"id\":\"t1\",\"createdAt\":1790000000000},{\"id\":\"t2\",\"createdAt\":1790000000000}]}",
    )
    .unwrap();
    let (_, doc, _) = h.inv();
    let f = folders_of(&doc, account(&doc, ACC_B))[0];
    assert_eq!(f.get("scheduled_tasks").n(), 2);
}

#[test]
fn writes_nothing_anywhere() {
    let h = scene();
    let mut before = Vec::new();
    digest(&h.root, &mut before);
    let (code, _, _) = h.inv();
    assert_eq!(code, 0);
    let o = h.run(&["inventory"]);
    assert_eq!(o.status.code(), Some(0));
    let mut after = Vec::new();
    digest(&h.root, &mut after);
    assert_eq!(before, after, "inventory changed files");
    assert!(!h.state.exists(), "state directory was created");
}

#[test]
fn titles_are_never_printed() {
    let h = scene();
    let (_, _, raw) = h.inv();
    assert!(!raw.contains(SECRET_TITLE));
    let o = h.run(&["inventory"]);
    let text = String::from_utf8_lossy(&o.stdout);
    assert_eq!(o.status.code(), Some(0));
    assert!(!text.contains(SECRET_TITLE));
    assert!(text.contains(ACC_A) && text.contains(ACC_B), "{text}");
}

#[test]
fn unreadable_card_is_exit_3_with_report() {
    let h = scene();
    let b = h.support().join(CC).join(ACC_B).join(ORG_B);
    fs::write(b.join("local_deadbeef-0000-4000-8000-000000000009.json"), "{not json").unwrap();
    let (code, doc, _) = h.inv();
    assert_eq!(code, 3);
    assert_eq!(doc.get("exit").n(), 3);
    assert!(!doc.get("read_complete").b());
    assert!(!doc.get("errors").arr().is_empty());
    assert!(doc.get("errors").arr().len() <= 20);
    assert_eq!(account(&doc, ACC_A).get("cards").n(), 1);
}

// Round 1 review (Codex, 4edb07b), finding 1: an unreadable transcript folder
// must not read as "no transcripts".
#[test]
fn unreadable_transcript_folder_is_exit_3() {
    let h = scene();
    let proj = h.home.join(".claude/projects/proj");
    Command::new("chmod").arg("000").arg(&proj).status().unwrap();
    let (code, doc, _) = h.inv();
    Command::new("chmod").arg("755").arg(&proj).status().unwrap();
    assert_eq!(code, 3);
    assert!(!doc.get("read_complete").b());
    assert!(!doc.get("errors").arr().is_empty());
}

// Round 2 review (Codex, 37ec069): a folder that can be listed but not
// searched (mode 0444) must not drop its transcripts silently.
#[test]
fn listable_but_unsearchable_folder_is_exit_3() {
    let h = scene();
    let proj = h.home.join(".claude/projects/proj");
    Command::new("chmod").arg("444").arg(&proj).status().unwrap();
    let (code, doc, _) = h.inv();
    Command::new("chmod").arg("755").arg(&proj).status().unwrap();
    assert_eq!(code, 3);
    assert!(!doc.get("read_complete").b());
    assert!(!doc.get("errors").arr().is_empty());
}

// Finding 2: only an existing regular file (or a link to one) is a transcript.
#[test]
fn dangling_link_or_directory_is_not_a_transcript() {
    let h = scene();
    let proj = h.home.join(".claude/projects/proj");
    // card 4 of B names ...0003, which has no transcript: give it a dangling link
    symlink(h.root.join("nowhere.jsonl"), proj.join("22222222-0000-4000-8000-000000000003.jsonl")).unwrap();
    // a directory with a transcript-like name, named by no card
    fs::create_dir_all(proj.join("88888888-0000-4000-8000-000000000001.jsonl")).unwrap();
    // a valid link to a real file still counts: card of A gets a second project
    let other = h.home.join(".claude/projects/other");
    fs::create_dir_all(&other).unwrap();
    symlink(
        proj.join("11111111-0000-4000-8000-000000000001.jsonl"),
        other.join("11111111-0000-4000-8000-000000000001.jsonl"),
    )
    .unwrap();
    let (code, doc, _) = h.inv();
    assert_eq!(code, 0);
    let f = folders_of(&doc, account(&doc, ACC_B))[0];
    assert_eq!(f.get("with_transcript").n(), 2);
    assert_eq!(f.get("without_transcript").n(), 1);
    assert_eq!(doc.get("transcripts").get("total").n(), 4);
    assert_eq!(account(&doc, ACC_A).get("with_transcript").n(), 1);
}

// Finding 3: one absurd timestamp is an incomplete read, not a crash.
#[test]
fn out_of_range_timestamp_is_exit_3_not_a_crash() {
    let h = scene();
    let b = h.support().join(CC).join(ACC_B).join(ORG_B);
    h.card(&b, 6, "22222222-0000-4000-8000-000000000006", 253_402_300_800_000);
    let (code, doc, _) = h.inv();
    assert_eq!(code, 3);
    assert!(!doc.get("read_complete").b());
    assert_eq!(account(&doc, ACC_B).get("cards").n(), 5);
    assert!(account(&doc, ACC_B).get("last_activity").s().starts_with("2026-05-28T"));
    assert_eq!(account(&doc, ACC_A).get("cards").n(), 1);
    let o = h.run(&["inventory"]);
    assert_eq!(o.status.code(), Some(3), "{}", String::from_utf8_lossy(&o.stderr));
}

// Finding 4: the documented plist install keeps a path with &, # and spaces.
#[test]
fn documented_plist_install_survives_odd_paths() {
    for f in ["README.md", "README.ru.md"] {
        let t = fs::read_to_string(repo().join(f)).unwrap();
        assert!(t.contains("plutil -replace ProgramArguments.2"), "{f}");
        assert!(t.contains("mkdir -p ~/Library/LaunchAgents"), "{f}");
    }
    let h = new_home();
    let odd = h.root.join("A&B #1 x");
    fs::create_dir_all(odd.join("deploy")).unwrap();
    fs::copy(repo().join("deploy/heal-active.plist"), odd.join("deploy/heal-active.plist")).unwrap();
    // the README's steps, in the README's order
    let script = r#"set -e
mkdir -p ~/Library/LaunchAgents
P=~/Library/LaunchAgents/io.github.mrskill.claude-account-move.heal-active.plist
cp deploy/heal-active.plist "$P"
plutil -replace ProgramArguments.2 -string "exec \"$PWD/claude-account-move\" heal-active --from-log --apply >> \"\$HOME/Library/Logs/claude-account-move-heal-active.log\" 2>&1" "$P"
plutil -lint "$P" >/dev/null
plutil -extract ProgramArguments.2 raw -o - "$P"
"#;
    let o = Command::new("bash").arg("-c").arg(script).current_dir(&odd).env("PWD", &odd).env("HOME", &h.home).output().unwrap();
    assert_eq!(o.status.code(), Some(0), "{}", String::from_utf8_lossy(&o.stderr));
    let got = String::from_utf8_lossy(&o.stdout).trim().to_string();
    assert_eq!(
        got,
        format!(
            "exec \"{}/claude-account-move\" heal-active --from-log --apply >> \"$HOME/Library/Logs/claude-account-move-heal-active.log\" 2>&1",
            odd.display()
        )
    );
}

#[test]
fn nothing_to_inventory_is_exit_4() {
    let h = new_home();
    let o = h.run(&["inventory", "--json"]);
    assert_eq!(o.status.code(), Some(4), "{}", String::from_utf8_lossy(&o.stderr));
}

#[test]
fn cli_only_transcripts_still_inventory() {
    let h = new_home();
    h.transcript("99999999-0000-4000-8000-000000000001");
    let (code, doc, _) = h.inv();
    assert_eq!(code, 0);
    assert_eq!(doc.get("transcripts").get("total").n(), 1);
    assert_eq!(doc.get("transcripts").get("without_card").n(), 1);
    assert!(doc.get("folders").arr().is_empty());
    assert_eq!(*doc.get("app_account"), J::Null);
    assert!(doc.has("accounts"));
}

#[test]
fn listed_in_help() {
    let h = new_home();
    let o = h.run(&["--help"]);
    assert!(String::from_utf8_lossy(&o.stdout).contains("inventory"));
}

// ---------- launcher and deploy ----------
struct Stage {
    root: PathBuf,
    real: PathBuf,
    home: PathBuf,
}

impl Drop for Stage {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.root);
    }
}

fn stage() -> Stage {
    let h = new_home();
    let root = h.root.clone();
    std::mem::forget(h);
    let real = root.join("real");
    fs::create_dir_all(&real).unwrap();
    fs::copy(repo().join("Claude Move.command"), real.join("Claude Move.command")).unwrap();
    let stub = real.join("claude-account-move");
    fs::write(&stub, "#!/bin/sh\necho \"ARGS=$*\"\nexit 0\n").unwrap();
    Command::new("chmod").arg("755").arg(&stub).status().unwrap();
    Stage { home: root.join("home"), root, real }
}

fn launch(s: &Stage, env_home: Option<&str>) -> Output {
    let mut c = Command::new("bash");
    c.arg(s.real.join("Claude Move.command")).env_remove("CLAUDE_ACCOUNT_MOVE_HOME").env("HOME", &s.home);
    if let Some(v) = env_home {
        c.env("CLAUDE_ACCOUNT_MOVE_HOME", v);
    }
    c.current_dir(&s.root);
    c.output().unwrap()
}

fn args_line(o: &Output) -> String {
    String::from_utf8_lossy(&o.stdout)
        .lines()
        .find(|l| l.starts_with("ARGS="))
        .unwrap_or_else(|| panic!("no ARGS line: {}", String::from_utf8_lossy(&o.stderr)))
        .to_string()
}

#[test]
fn launcher_default_is_the_cli_default() {
    let s = stage();
    let o = launch(&s, None);
    assert_eq!(o.status.code(), Some(0));
    assert_eq!(
        args_line(&o),
        format!("ARGS=auto --state-dir {}/.claude-account-move --pause", s.home.display())
    );
}

#[test]
fn launcher_reads_local_env_without_running_it() {
    let s = stage();
    let marker = s.root.join("pwned");
    fs::write(
        s.real.join("local.env"),
        format!(
            "# owner state dir\n\n$(touch {m})\ntouch {m}\nCLAUDE_ACCOUNT_MOVE_HOME=\"$HOME/my state\"\n",
            m = marker.display()
        ),
    )
    .unwrap();
    let o = launch(&s, None);
    assert_eq!(o.status.code(), Some(0));
    assert!(!marker.exists(), "local.env was executed");
    assert_eq!(args_line(&o), format!("ARGS=auto --state-dir {}/my state --pause", s.home.display()));
    assert!(!String::from_utf8_lossy(&o.stderr).trim().is_empty(), "ignored lines must warn");
}

#[test]
fn launcher_local_env_tilde_and_single_quotes() {
    let s = stage();
    fs::write(s.real.join("local.env"), "CLAUDE_ACCOUNT_MOVE_HOME='~/st'\n").unwrap();
    let o = launch(&s, None);
    assert_eq!(args_line(&o), format!("ARGS=auto --state-dir {}/st --pause", s.home.display()));
}

#[test]
fn launcher_environment_wins_over_local_env() {
    let s = stage();
    fs::write(s.real.join("local.env"), "CLAUDE_ACCOUNT_MOVE_HOME=/from/file\n").unwrap();
    let o = launch(&s, Some("/from/env"));
    assert_eq!(args_line(&o), "ARGS=auto --state-dir /from/env --pause");
}

#[test]
fn local_env_is_git_ignored() {
    let o = Command::new("git").args(["check-ignore", "-q", "local.env"]).current_dir(repo()).output().unwrap();
    assert_eq!(o.status.code(), Some(0), "local.env must be in .gitignore");
}

#[test]
fn plist_is_a_neutral_template() {
    assert!(!repo().join("deploy/com.neurovision.heal-active.plist").exists());
    let t = fs::read_to_string(repo().join("deploy/heal-active.plist")).unwrap();
    assert!(t.contains("<string>io.github.mrskill.claude-account-move.heal-active</string>"));
    assert!(t.contains("__REPO__/claude-account-move"));
    assert!(t.contains("heal-active --from-log --apply"));
    assert!(t.contains("<integer>60</integer>"));
    assert!(t.contains("Library/Logs/claude-account-move-heal-active.log"));
}

#[test]
fn no_owner_paths_or_names_in_tracked_files() {
    let o = Command::new("git")
        .args(["grep", "-n", "-I", "-i", "-E", "MEGAsync|neurovision|nv\\.global|hooks/state", "--", ".", ":!CHANGELOG.md", ":!tests/inventory_oracle.rs"])
        .current_dir(repo())
        .output()
        .unwrap();
    let hits = String::from_utf8_lossy(&o.stdout);
    assert!(hits.trim().is_empty(), "owner-specific text left:\n{hits}");
}

#[test]
fn readme_documents_inventory_and_plist_install() {
    for f in ["README.md", "README.ru.md"] {
        let t = fs::read_to_string(repo().join(f)).unwrap();
        assert!(t.contains("inventory"), "{f}");
        assert!(t.contains("__REPO__"), "{f}: sed install of the plist");
        assert!(t.contains("local.env"), "{f}");
    }
}
