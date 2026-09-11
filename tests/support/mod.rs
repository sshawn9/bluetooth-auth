use std::{
    fs,
    io::{BufRead, BufReader},
    path::PathBuf,
    process::{Child, Command, Stdio},
    time::{SystemTime, UNIX_EPOCH},
};

pub struct PrivateBus {
    pub address: String,
    pub directory: PathBuf,
    daemon: Option<Child>,
}

impl PrivateBus {
    pub fn start() -> Self {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let directory =
            std::env::temp_dir().join(format!("bt-auth-test-{}-{nonce}", std::process::id()));
        fs::create_dir(&directory).unwrap();
        let mut bus = Self {
            address: String::new(),
            directory,
            daemon: None,
        };
        let socket = bus.directory.join("bus.sock");
        let config = bus.directory.join("bus.conf");
        fs::write(
            &config,
            format!(
                "<busconfig><type>system</type><listen>unix:path={}</listen>\
                 <policy context=\"default\"><allow own=\"*\"/>\
                 <allow send_destination=\"*\"/><allow receive_sender=\"*\"/>\
                 </policy></busconfig>",
                socket.display()
            ),
        )
        .unwrap();
        bus.daemon = Some(
            Command::new("dbus-daemon")
                .arg("--nofork")
                .arg(format!("--config-file={}", config.display()))
                .arg("--print-address=1")
                .stdout(Stdio::piped())
                .stderr(Stdio::null())
                .spawn()
                .unwrap(),
        );
        let stdout = bus.daemon.as_mut().unwrap().stdout.take().unwrap();
        BufReader::new(stdout).read_line(&mut bus.address).unwrap();
        assert!(
            !bus.address.trim().is_empty(),
            "dbus-daemon printed no address"
        );
        bus.address = bus.address.trim().to_owned();
        bus
    }
}

impl Drop for PrivateBus {
    fn drop(&mut self) {
        if let Some(daemon) = &mut self.daemon {
            let _ = daemon.kill();
            let _ = daemon.wait();
        }
        let _ = fs::remove_dir_all(&self.directory);
    }
}
